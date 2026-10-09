#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.core.ai_review — AI Edit Review Manager & Filesystem Watcher.

Faithfully implements the AI Accept/Decline review architecture from
the reference codebase, optimized for PySide6 Qt:
  • Calculates accurate line-level diffs (added/modified/removed hunks)
    for Monaco editor decorations.
  • Maintains atomic, two-replica persistent review journals (.state/pending_reviews.json
    and .bak) and integrates with AIEditBackupStore for session history.
  • Provides a lightweight, debounced file watcher that detects OpenCode AI edits
    via .ai_edit_signal and sketch source file modifications.
  • Emits Qt signals to trigger Monaco's floating Accept/Decline review banner.
  • Distinguishes manual user editor saves from external AI edits.
"""
from __future__ import annotations

import os
import sys
import time
import json
import difflib
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any, Callable

# pyrefly: ignore [missing-import]
from PySide6.QtCore import QObject, Signal, QTimer, QFileSystemWatcher, Qt

from main.core.file_utils import (
    ensure_file_writable,
    get_ai_review_state_file,
    AIEditBackupStore,
    retry_transient_file_operation,
)

def build_ai_line_diff(before_content: str, after_content: str) -> dict:
    """Build compact ranges in the after-model, with separate deletion anchors.

    Removed text has no range in that model. Its red marker anchors to the next
    surviving line (or the last line at EOF), without recoloring that line's
    added/modified text. The original range remains available for its tooltip.
    """
    before_lines = str(before_content or "").splitlines()
    after_lines = str(after_content or "").splitlines()
    if before_content == after_content:
        return {"changes": [], "added": 0, "removed": 0, "modified": 0, "firstLine": 1}

    # SequenceMatcher gives accurate VS Code-like hunks for normal sketches.
    # For very large generated files, bound the work to the changed middle
    # region so diff calculation never freezes the GUI.
    if len(before_lines) + len(after_lines) > 8000:
        prefix = 0
        limit = min(len(before_lines), len(after_lines))
        while prefix < limit and before_lines[prefix] == after_lines[prefix]:
            prefix += 1
        old_tail, new_tail = len(before_lines), len(after_lines)
        while old_tail > prefix and new_tail > prefix and before_lines[old_tail - 1] == after_lines[new_tail - 1]:
            old_tail -= 1
            new_tail -= 1
        if old_tail == prefix:
            tag = "insert"
        elif new_tail == prefix:
            tag = "delete"
        else:
            tag = "replace"
        opcodes = [(tag, prefix, old_tail, prefix, new_tail)]
    else:
        opcodes = difflib.SequenceMatcher(
            None, before_lines, after_lines, autojunk=False
        ).get_opcodes()

    changes = []
    added = removed = modified = 0
    first_line = None
    line_count = max(1, len(after_lines))

    def append_change(kind, start, end, **metadata):
        nonlocal first_line
        start = min(line_count, max(1, start))
        end = min(line_count, max(start, end))
        changes.append({"type": kind, "startLine": start, "endLine": end, **metadata})
        first_line = start if first_line is None else min(first_line, start)

    def append_removed(anchor, before_start, before_end):
        append_change(
            "removed", anchor, anchor, anchorOnly=True,
            removedCount=before_end - before_start + 1,
            beforeStartLine=before_start, beforeEndLine=before_end,
        )

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        old_count, new_count = i2 - i1, j2 - j1
        if not old_count and not new_count:
            continue
        if tag == "insert":
            added += new_count
            append_change("added", j1 + 1, j2)
        elif tag == "delete":
            removed += old_count
            append_removed(j1 + 1, i1 + 1, i2)
        else:
            shared = min(old_count, new_count)
            modified += shared
            if shared:
                append_change("modified", j1 + 1, j1 + shared)
            if new_count > shared:
                added += new_count - shared
                append_change("added", j1 + shared + 1, j2)
            if old_count > shared:
                removed += old_count - shared
                append_removed(j1 + shared + 1, i1 + shared + 1, i2)

    return {
        "changes": changes,
        "added": added,
        "removed": removed,
        "modified": modified,
        "firstLine": first_line or 1,
    }


class AIReviewManager:
    """
    Manages pending AI review proposals, multi-file review queues,
    persistent journal replicas, and multi-level Undo/Redo stacks.
    """

    def __init__(self, project_dir: Optional[str | Path] = None):
        self.project_dir: Optional[Path] = (
            Path(project_dir).resolve(strict=False) if project_dir else None
        )
        self._pending_ai_edits: dict[str, dict] = {}
        self._pending_ai_lock = threading.RLock()
        self._ai_decision_history: list[dict] = []
        self._ai_decision_redo: list[dict] = []
        self._ai_decision_history_limit: int = 50
        self._ai_backup_store: Optional[AIEditBackupStore] = None
        self._ai_review_revision: int = 0
        self._ai_review_generation: int = 0
        self._ai_review_journal_error: str = ""
        self._ai_review_journal_recovery_required: bool = False
        self._ai_review_state_path: Optional[Path] = None
        self._ai_changes: list[dict] = []
        self._change_group = ""
        self._change_group_time = 0.0
        self.history_changed_callback = None

        if self.project_dir:
            self.bind_project(self.project_dir)

    @staticmethod
    def _path_key(path: str | Path) -> str:
        try:
            return os.path.normcase(str(Path(path or "").resolve(strict=False)))
        except (OSError, ValueError):
            return os.path.normcase(os.path.abspath(str(path or "")))

    def _path_is_in_project(self, path: str | Path) -> bool:
        if not self.project_dir or not path:
            return False
        try:
            p_root = Path(self.project_dir).resolve(strict=False)
            candidate = Path(path).resolve(strict=False)
            return os.path.commonpath(
                [os.path.normcase(str(p_root)), os.path.normcase(str(candidate))]
            ) == os.path.normcase(str(p_root))
        except (OSError, ValueError):
            return False

    @staticmethod
    def _read_text_exact(path: str | Path) -> str:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as stream:
            return stream.read()

    @staticmethod
    def _write_text_atomic(path: str | Path, content: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_file_writable(target)
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{target.name}.ai-review-",
            suffix=".tmp",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="strict", newline="") as stream:
                stream.write(str(content))
                stream.flush()
                os.fsync(stream.fileno())
            ensure_file_writable(target)
            retry_transient_file_operation(lambda: os.replace(temp_path, target))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def bind_project(self, project_dir: str | Path) -> None:
        """Switch review journal and backup session when sketch project changes."""
        resolved = Path(project_dir).resolve(strict=False) if project_dir else None
        if not resolved or not resolved.is_dir():
            return

        with self._pending_ai_lock:
            try:
                if self._pending_ai_edits:
                    self._commit_pending_ai_edits_locked()
            except Exception:
                pass

            self.project_dir = resolved
            self._pending_ai_edits = {}
            self._ai_decision_history = []
            self._ai_decision_redo = []
            self._ai_changes = []
            self._change_group = ""
            self._change_group_time = 0.0
            self._ai_review_revision = 0
            self._ai_review_generation = 0
            self._ai_review_journal_error = ""
            self._ai_review_journal_recovery_required = False

            old_backup = self._ai_backup_store
            self._ai_backup_store = None

            try:
                self._ai_review_state_path = get_ai_review_state_file(self.project_dir)
            except Exception:
                self._ai_review_state_path = None

            self._load_pending_ai_edits_locked()

        if old_backup:
            try:
                old_backup.shutdown(timeout=0.8)
            except Exception:
                pass

        try:
            self._ai_backup_store = AIEditBackupStore(self.project_dir)
            self._ai_backup_store.current_project = str(self.project_dir)
        except Exception as exc:
            print(f"[MCU Flasher] AI edit backup session could not start: {exc}")
            self._ai_backup_store = None

    def _ai_review_backup_path(self) -> Optional[Path]:
        sp = self._ai_review_state_path
        return sp.with_suffix(sp.suffix + ".bak") if sp else None

    @staticmethod
    def _write_journal_atomic(path: Path, data: dict) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_file_writable(target)
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="strict", newline="") as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            ensure_file_writable(target)
            retry_transient_file_operation(lambda: os.replace(temp_path, target))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _next_ai_review_revision_locked(self) -> str:
        self._ai_review_revision += 1
        return str(self._ai_review_revision)

    def _ai_review_state_data_locked(self, generation: Optional[int] = None) -> dict:
        return {
            "version": 3,
            "generation": (
                self._ai_review_generation if generation is None else int(generation)
            ),
            "revision": self._ai_review_revision,
            "reviews": list(self._pending_ai_edits.values()),
            "changes": self._ai_changes,
        }

    def _commit_pending_ai_edits_locked(self) -> None:
        state_path = self._ai_review_state_path
        if not state_path:
            return
        backup_path = self._ai_review_backup_path()
        if not backup_path:
            return

        generation = max(
            self._ai_review_generation + 1,
            self._ai_review_revision,
        )
        data = self._ai_review_state_data_locked(generation)

        # Backup-first ordering guarantees atomic durable persistence
        try:
            self._write_journal_atomic(backup_path, data)
            self._ai_review_generation = generation
            self._write_journal_atomic(state_path, data)
            self._ai_review_journal_error = ""
            self._ai_review_journal_recovery_required = False
        except Exception as exc:
            self._ai_review_journal_error = str(exc)

    def _load_pending_ai_edits_locked(self) -> None:
        state_path = self._ai_review_state_path
        if not state_path or not state_path.parent.is_dir():
            return
        backup_path = self._ai_review_backup_path()
        candidates = [c for c in (state_path, backup_path) if c and c.exists()]
        if not candidates:
            return

        loaded_candidates = []
        for candidate in candidates:
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                version = int(data.get("version", 1) or 1)
                reviews = data.get("reviews", [])
                if not isinstance(reviews, list):
                    continue
                revision = int(data.get("revision", 0) or 0)
                generation = int(data.get("generation", revision))

                loaded = {}
                for raw in reviews:
                    if not isinstance(raw, dict):
                        continue
                    p = raw.get("path")
                    if not self._path_is_in_project(p):
                        continue
                    before_c = str(raw.get("beforeContent", ""))
                    after_c = str(raw.get("content", ""))
                    raw["beforeContent"] = before_c
                    raw["content"] = after_c
                    raw["beforeExists"] = bool(raw.get("beforeExists", True))
                    raw["afterExists"] = bool(raw.get("afterExists", True))
                    raw["diff"] = build_ai_line_diff(before_c, after_c)
                    raw["revision"] = str(raw.get("revision", "0"))
                    loaded[self._path_key(p)] = raw

                loaded_candidates.append({
                    "path": candidate,
                    "generation": generation,
                    "revision": revision,
                    "reviews": loaded,
                    "changes": [item for item in data.get("changes", [])
                                if isinstance(item, dict) and self._path_is_in_project(item.get("path"))]
                                if isinstance(data.get("changes", []), list) else [],
                })
            except Exception:
                continue

        if loaded_candidates:
            loaded_candidates.sort(key=lambda x: x["generation"], reverse=True)
            best = loaded_candidates[0]
            self._pending_ai_edits = best["reviews"]
            self._ai_review_revision = best["revision"]
            self._ai_review_generation = best["generation"]
            self._ai_changes = best["changes"]
            self._bound_changes_locked()

    def _bound_changes_locked(self) -> None:
        self._ai_changes = self._ai_changes[-200:]
        total = 0
        for index in range(len(self._ai_changes) - 1, -1, -1):
            item = self._ai_changes[index]
            for field in ("beforeContent", "content"):
                value = str(item.get(field, ""))
                if len(value) > 60000:
                    item["previewTruncated"] = True
                item[field] = value[:60000]
            total += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            if total > 4 * 1024 * 1024:
                del self._ai_changes[:index + 1]
                break

    def _history_changed(self) -> None:
        if callable(self.history_changed_callback):
            try:
                self.history_changed_callback()
            except RuntimeError:
                pass

    def get_ai_changes(self) -> list[dict]:
        with self._pending_ai_lock:
            return [dict(item) for item in self._ai_changes]

    def delete_ai_changes(self, change_id: str = "") -> dict:
        """Remove display history only; pending reviews and recovery remain intact."""
        with self._pending_ai_lock:
            original = self._ai_changes
            self._ai_changes = [item for item in original if item.get("id") != change_id] if change_id else []
            self._commit_pending_ai_edits_locked()
            if self._ai_review_journal_error:
                error = self._ai_review_journal_error
                self._ai_changes = original
                # Repair a successfully written first replica after a failed
                # second write; report the original failure either way.
                self._commit_pending_ai_edits_locked()
                return {"success": False, "error": error}
        self._history_changed()
        return {"success": True}

    def _record_change_locked(self, payload: dict, before: str, context: Optional[dict]) -> None:
        now = time.monotonic()
        context = context or {}
        group = str(context.get("id", ""))[:160]
        title = str(context.get("prompt", ""))[:500]
        prompt_source = "cli" if context.get("source") == "cli" else ""
        if not group:
            if not self._change_group or now - self._change_group_time > 8:
                self._change_group = f"external:{time.time_ns()}"
            group = self._change_group
            title = "External assistant changes (prompt unavailable)"
            prompt_source = "external"
        self._change_group_time = now
        record_id = f"{group}:{self._path_key(payload['path'])}"
        existing = next((item for item in self._ai_changes if item.get("id") == record_id), None)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record = dict(payload, id=record_id, groupId=group, prompt=title or "Assistant changes",
                      promptSource=prompt_source or (existing or {}).get("promptSource", ""),
                      timestamp=existing.get("timestamp", stamp) if existing else stamp,
                      updatedAt=stamp, status="pending", beforeContent=existing.get("beforeContent", before) if existing else before)
        if existing:
            self._ai_changes.remove(existing)
        self._ai_changes.append(record)
        self._bound_changes_locked()

    def _snapshot_for_key_locked(self, key: str) -> dict:
        payload = self._pending_ai_edits.get(key)
        if not payload:
            return {}
        keys = list(self._pending_ai_edits.keys())
        snapshot = dict(payload)
        snapshot["pendingCount"] = len(keys)
        snapshot["reviewIndex"] = keys.index(key) + 1
        snapshot["fileName"] = Path(snapshot["path"]).name
        return snapshot

    def queue_ai_edit_snapshot(
        self,
        path: str | Path,
        before_content: str,
        after_content: str,
        before_exists: bool = True,
        after_exists: bool = True,
        *,
        expected_project: Optional[Path] = None,
        expected_valid: Optional[Callable[[], bool]] = None,
        prompt_context: Optional[dict] = None,
    ) -> bool | str:
        """Queue a detected external AI edit for user review in Monaco."""
        if not path or not self._path_is_in_project(path):
            return False
        before_content = str(before_content or "")
        after_content = str(after_content or "")
        before_exists = bool(before_exists)
        after_exists = bool(after_exists)
        if before_exists == after_exists and before_content == after_content:
            return False

        resolved_path = str(Path(path).resolve(strict=False))
        key = self._path_key(resolved_path)

        with self._pending_ai_lock:
            # A slow filesystem scan may finish after the workspace switched.
            # Recheck under the same lock that bind_project uses, before any
            # journal mutation; the earlier path check alone can race a switch.
            if expected_project is not None and self.project_dir != expected_project:
                return False
            if expected_valid is not None and not expected_valid():
                return False
            if not self._path_is_in_project(resolved_path):
                return False
            existing = self._pending_ai_edits.get(key)
            if existing:
                original_content = str(existing.get("beforeContent", ""))
                original_exists = bool(existing.get("beforeExists", True))
                review_id = str(existing.get("reviewId") or key)
            else:
                original_content = before_content
                original_exists = before_exists
                review_id = f"{key}:{time.time_ns()}"

            # If user or AI reverted back to original, clear review
            if existing and original_exists == after_exists and original_content == after_content:
                self._pending_ai_edits.pop(key, None)
                for item in self._ai_changes:
                    if item.get("reviewId") == existing.get("reviewId"):
                        item["status"] = "cancelled"
                self._next_ai_review_revision_locked()
                try:
                    self._commit_pending_ai_edits_locked()
                except Exception:
                    pass
                if self._ai_backup_store:
                    try:
                        self._ai_backup_store.mark_cancelled(existing)
                    except Exception:
                        pass
                self._history_changed()
                return "cancelled"

            payload = {
                "reviewId": review_id,
                "revision": self._next_ai_review_revision_locked(),
                "path": resolved_path,
                "beforeContent": original_content,
                "content": after_content,
                "beforeExists": original_exists,
                "afterExists": after_exists,
                "diff": build_ai_line_diff(original_content, after_content),
            }
            # Diff calculation can take time for a large source. If a manual
            # save or project switch won meanwhile, do not start journal I/O.
            if expected_valid is not None and not expected_valid():
                return False
            self._pending_ai_edits[key] = payload
            self._record_change_locked(payload, before_content, prompt_context)
            self._ai_decision_redo.clear()
            try:
                self._commit_pending_ai_edits_locked()
            except Exception:
                pass

            if self._ai_backup_store:
                try:
                    payload["project"] = str(self.project_dir or "")
                    payload["backupFile"] = self._ai_backup_store.record_edit(
                        payload, status="pending"
                    )
                except Exception:
                    pass
        self._history_changed()
        return True

    def consume_ai_edit_snapshot(self, path: str) -> dict:
        """Fetch active review snapshot for Monaco editor."""
        with self._pending_ai_lock:
            return self._snapshot_for_key_locked(self._path_key(path))

    def get_ai_edit_reviews(self) -> list[dict]:
        """Return all pending reviews for JS navigation & status counters."""
        with self._pending_ai_lock:
            result = []
            for key in self._pending_ai_edits:
                snapshot = self._snapshot_for_key_locked(key)
                result.append({
                    "reviewId": snapshot.get("reviewId"),
                    "revision": snapshot.get("revision"),
                    "path": snapshot.get("path"),
                    "fileName": snapshot.get("fileName"),
                    "beforeExists": snapshot.get("beforeExists"),
                    "afterExists": snapshot.get("afterExists"),
                    "diff": snapshot.get("diff"),
                    "pendingCount": snapshot.get("pendingCount"),
                    "reviewIndex": snapshot.get("reviewIndex"),
                })
            return result

    def has_pending_ai_edit(self, path: str) -> bool:
        with self._pending_ai_lock:
            return self._path_key(path) in self._pending_ai_edits

    def has_any_pending_ai_edits(self) -> bool:
        with self._pending_ai_lock:
            return bool(self._pending_ai_edits)

    def get_ai_review_journal_error(self) -> str:
        with self._pending_ai_lock:
            return self._ai_review_journal_error

    def _resolve_ai_edit(self, path: str, revision: str, accept: bool) -> dict:
        key = self._path_key(path)
        with self._pending_ai_lock:
            payload = self._pending_ai_edits.get(key)
            if not payload:
                return {"success": False, "error": "This AI review is no longer pending."}

            if str(revision or "") and str(payload.get("revision", "")) != str(revision):
                return {
                    "success": False,
                    "conflict": True,
                    "error": "The AI edit changed while it was being reviewed. The latest version has been reopened.",
                    "snapshot": self._snapshot_for_key_locked(key),
                }

            target = Path(payload["path"])
            if not accept:
                try:
                    if bool(payload.get("beforeExists", True)):
                        self._write_text_atomic(target, payload.get("beforeContent", ""))
                    elif target.exists():
                        ensure_file_writable(target)
                        target.unlink(missing_ok=True)
                except Exception as exc:
                    return {
                        "success": False,
                        "error": f"Could not restore the original file: {exc}",
                    }

            resolved = dict(payload)
            final_exists = (
                bool(resolved.get("afterExists", True))
                if accept else bool(resolved.get("beforeExists", True))
            )
            final_content = (
                str(resolved.get("content", ""))
                if accept else str(resolved.get("beforeContent", ""))
            )

            decision_entry = {
                "decisionId": f"{self._next_ai_review_revision_locked()}:{key}",
                "reviewId": resolved.get("reviewId", key),
                "project": str(self.project_dir or ""),
                "path": resolved["path"],
                "fileName": Path(resolved["path"]).name,
                "action": "accepted" if accept else "rejected",
                "appliedExists": final_exists,
                "appliedContent": final_content,
                "undoExists": (
                    bool(resolved.get("beforeExists", True))
                    if accept else bool(resolved.get("afterExists", True))
                ),
                "undoContent": (
                    str(resolved.get("beforeContent", ""))
                    if accept else str(resolved.get("content", ""))
                ),
            }

            self._pending_ai_edits.pop(key, None)
            for item in self._ai_changes:
                if item.get("reviewId") == resolved.get("reviewId"):
                    item["status"] = "accepted" if accept else "rejected"
            try:
                self._commit_pending_ai_edits_locked()
            except Exception:
                pass

            if self._ai_backup_store:
                try:
                    resolved["project"] = decision_entry["project"]
                    decision_entry["backupFile"] = self._ai_backup_store.record_edit(
                        resolved,
                        status=decision_entry["action"],
                        decision_entry=decision_entry,
                    )
                except Exception:
                    pass

            self._ai_decision_history.append(decision_entry)
            if len(self._ai_decision_history) > self._ai_decision_history_limit:
                del self._ai_decision_history[:-self._ai_decision_history_limit]
            self._ai_decision_redo.clear()

            next_path = next(iter(self._pending_ai_edits.values()), {}).get("path", "")
            pending_count = len(self._pending_ai_edits)

        self._history_changed()
        return {
            "success": True,
            "action": "accepted" if accept else "rejected",
            "path": resolved["path"],
            "appliedContent": final_content,
            "beforeExists": bool(resolved.get("beforeExists", True)),
            "afterExists": bool(resolved.get("afterExists", True)),
            "nextPath": next_path,
            "pendingCount": pending_count,
            "undoAvailable": True,
        }

    def accept_ai_edit(self, path: str, revision: str = "") -> dict:
        return self._resolve_ai_edit(path, revision, accept=True)

    def reject_ai_edit(self, path: str, revision: str = "") -> dict:
        return self._resolve_ai_edit(path, revision, accept=False)

    def _ai_history_summary_locked(self) -> dict:
        def _summary(entry):
            if not entry:
                return None
            return {
                "decisionId": entry.get("decisionId", ""),
                "path": entry.get("path", ""),
                "fileName": entry.get("fileName") or Path(entry.get("path", "")).name,
                "action": entry.get("action", "edited"),
            }

        undo_entry = self._ai_decision_history[-1] if self._ai_decision_history else None
        redo_entry = self._ai_decision_redo[-1] if self._ai_decision_redo else None
        return {
            "canUndo": bool(undo_entry),
            "canRedo": bool(redo_entry),
            "undoDepth": len(self._ai_decision_history),
            "redoDepth": len(self._ai_decision_redo),
            "undo": _summary(undo_entry),
            "redo": _summary(redo_entry),
        }

    def get_ai_history_state(self) -> dict:
        with self._pending_ai_lock:
            return self._ai_history_summary_locked()

    def _apply_ai_history_decision(self, direction: str, force: bool = False) -> dict:
        undoing = str(direction).lower() == "undo"
        with self._pending_ai_lock:
            if self._pending_ai_edits:
                return {
                    "success": False,
                    "error": "Accept or reject the pending AI review before using AI Undo/Redo.",
                    "history": self._ai_history_summary_locked(),
                }
            source = self._ai_decision_history if undoing else self._ai_decision_redo
            destination = self._ai_decision_redo if undoing else self._ai_decision_history
            if not source:
                return {
                    "success": False,
                    "error": "There is no AI decision to undo." if undoing else "There is no AI decision to redo.",
                    "history": self._ai_history_summary_locked(),
                }

            entry = source[-1]
            path = entry.get("path", "")
            target = Path(path)
            target_exists = bool(entry.get("undoExists" if undoing else "appliedExists", True))
            target_content = str(entry.get("undoContent" if undoing else "appliedContent", ""))

            try:
                if target_exists:
                    self._write_text_atomic(target, target_content)
                elif target.exists():
                    ensure_file_writable(target)
                    target.unlink(missing_ok=True)
            except Exception as exc:
                return {
                    "success": False,
                    "error": f"Could not {'undo' if undoing else 'redo'} the AI decision: {exc}",
                    "history": self._ai_history_summary_locked(),
                }

            source.pop()
            destination.append(entry)
            if len(destination) > self._ai_decision_history_limit:
                del destination[:-self._ai_decision_history_limit]

            return {
                "success": True,
                "direction": "undo" if undoing else "redo",
                "action": entry.get("action", "edited"),
                "path": path,
                "fileName": entry.get("fileName") or Path(path).name,
                "appliedContent": target_content,
                "exists": target_exists,
                "history": self._ai_history_summary_locked(),
            }

    def undo_ai_edit_decision(self, force: bool = False) -> dict:
        return self._apply_ai_history_decision("undo", force=bool(force))

    def redo_ai_edit_decision(self, force: bool = False) -> dict:
        return self._apply_ai_history_decision("redo", force=bool(force))

    def shutdown(self) -> None:
        if self._ai_backup_store:
            try:
                self._ai_backup_store.shutdown(timeout=1.0)
            except Exception:
                pass


class AIEditWatcher(QObject):
    """
    Lightweight, high-frequency sketch file & signal watcher.
    Filesystem events wake one background scan; a quiet fallback catches
    unsupported watchers without repeatedly seeking through a USB/HDD project
    on the GUI thread. Scan completions belong to the captured project only.
    """
    ai_edit_detected = Signal(str, str, str, bool, bool)  # (path, before, after, before_exists, after_exists)
    _scan_finished = Signal(object)

    SKETCH_EXTS = {".ino", ".cpp", ".c", ".h", ".hpp", ".txt"}

    def __init__(
        self,
        project_dir: Optional[str | Path],
        review_manager: AIReviewManager,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.project_dir: Optional[Path] = (
            Path(project_dir).resolve(strict=False) if project_dir else None
        )
        self.review_manager = review_manager
        self._baseline_contents: dict[str, str] = {}
        self._baseline_mtimes: dict[str, tuple] = {}
        self._pending_settle: dict[str, dict] = {}
        self._last_signal_mtime: float = 0.0
        self._lock = threading.Lock()
        self._generation = 0
        self._scan_running = False
        self._scan_pending = False
        self._baseline_ready = False
        self._force_read = False
        self._save_versions: dict[str, int] = {}
        self._closed = False
        self._user_operations: set[str] = set()

        # File events are immediate. The fallback is deliberately less frequent
        # even on high-core PCs: faster CPUs do not improve mechanical seeks.
        self._idle_interval = 5000
        self._settle_interval = 250
        self._timer = QTimer(self)
        self._timer.setInterval(self._idle_interval)
        self._timer.timeout.connect(self._poll_step)
        self._wake_timer = QTimer(self)
        self._wake_timer.setSingleShot(True)
        self._wake_timer.setInterval(100)
        self._wake_timer.timeout.connect(self._poll_step)
        self._scan_finished.connect(self._finish_scan, Qt.ConnectionType.QueuedConnection)

        # OS-level filesystem watcher (Windows ReadDirectoryChangesW): zero-overhead event notification
        self._fs_watcher = QFileSystemWatcher(self)
        self._fs_watcher.directoryChanged.connect(self._on_fs_changed)
        self._fs_watcher.fileChanged.connect(self._on_fs_changed)

        if self.project_dir:
            try:
                self._fs_watcher.addPath(str(self.project_dir))
            except Exception:
                pass
            self._timer.start()
            self._poll_step()

    def _on_fs_changed(self, path: str) -> None:
        """Coalesce event bursts, including replacements with coarse timestamps."""
        with self._lock:
            self._force_read = True
        self._wake_timer.start()

    def bind_project(self, project_dir: str | Path) -> None:
        """Switch watcher target when project folder changes."""
        resolved = Path(project_dir).resolve(strict=False) if project_dir else None
        with self._lock:
            self.project_dir = resolved
            self._baseline_contents.clear()
            self._baseline_mtimes.clear()
            self._pending_settle.clear()
            self._last_signal_mtime = 0.0
            self._generation += 1
            self._baseline_ready = False
            self._force_read = False
            self._save_versions.clear()
            self._scan_pending = False

            old_paths = self._fs_watcher.directories() + self._fs_watcher.files()
            if old_paths:
                self._fs_watcher.removePaths(old_paths)

            if self.project_dir:
                try:
                    self._fs_watcher.addPath(str(self.project_dir))
                except Exception:
                    pass
                if not self._timer.isActive():
                    self._timer.start()
            else:
                self._timer.stop()
        self._wake_timer.stop()
        if self.project_dir:
            self._poll_step()

    def _is_sketch_file(self, path: Path) -> bool:
        if not path.is_file():
            return False
        if path.name.startswith("."):
            return False
        return path.suffix.lower() in self.SKETCH_EXTS

    def _rebuild_baseline(self) -> None:
        """Schedule, rather than synchronously reading every source at startup."""
        with self._lock:
            self._baseline_ready = False
        self._poll_step()

    @staticmethod
    def _signature(stat) -> tuple:
        return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)

    def note_user_save(self, path: str | Path, content: Optional[str] = None) -> None:
        """Record manual user saves so they are never flagged as external AI edits."""
        target = Path(path)
        key = AIReviewManager._path_key(target)
        try:
            stat = target.stat()
            saved_content = content if content is not None else AIReviewManager._read_text_exact(target)
        except FileNotFoundError:
            stat, saved_content = None, ""
        except OSError:
            return
        with self._lock:
            self._save_versions[key] = self._save_versions.get(key, 0) + 1
            self._pending_settle.pop(key, None)
            if stat is None:
                self._baseline_mtimes.pop(key, None)
                self._baseline_contents.pop(key, None)
            else:
                self._baseline_mtimes[key] = self._signature(stat)
                self._baseline_contents[key] = saved_content

    @contextmanager
    def user_file_operation(self, *paths):
        """Invalidate scans before a manual add/rename/delete can touch disk."""
        keys = {AIReviewManager._path_key(path) for path in paths}
        # Match the scan's manager -> watcher lock order.
        with self.review_manager._pending_ai_lock:
            if any(key in self.review_manager._pending_ai_edits for key in keys):
                raise ValueError("Review pending AI changes before modifying this file.")
            with self._lock:
                self._user_operations.update(keys)
                for key in keys:
                    self._save_versions[key] = self._save_versions.get(key, 0) + 1
                    self._pending_settle.pop(key, None)
        try:
            yield
        finally:
            for path in paths:
                self.note_user_save(path)
            with self._lock:
                self._user_operations.difference_update(keys)

    def _poll_step(self) -> None:
        """Dispatch one scan, retaining only one latest pending request."""
        with self._lock:
            if self._closed or not self.project_dir:
                return
            if self._scan_running:
                self._scan_pending = True
                return
            self._scan_running = True
            request = (self._generation, self.project_dir, self._baseline_ready,
                       dict(self._baseline_contents), dict(self._baseline_mtimes),
                       {key: dict(value) for key, value in self._pending_settle.items()},
                       dict(self._save_versions), self._force_read)
            self._force_read = False

        def scan():
            try:
                result = self._scan_project(request)
            except Exception as exc:
                result = {"generation": request[0], "error": str(exc), "events": []}
            with self._lock:
                if self._closed:
                    return
            try:
                # The parent window can delete the QObject after the closed
                # check while a slow scan finishes. Do not report that normal
                # teardown as an unhandled worker failure. Live stale-project
                # completions still reach _finish_scan to release the slot.
                self._scan_finished.emit(result)
            except RuntimeError:
                pass

        try:
            threading.Thread(target=scan, name="MCU_SketchWatcher", daemon=True).start()
        except Exception as exc:
            self._finish_scan({"generation": request[0], "error": str(exc), "events": []})

    def _scan_project(self, request) -> dict:
        generation, project, ready, contents, signatures, pending, versions, force = request
        now = time.monotonic()
        signal_file = project / ".ai_edit_signal"
        prompt_context = None
        try:
            context_file = project / ".mcu_flasher_build_cache" / "ai_prompt.json"
            if context_file.stat().st_size <= 8192:
                candidate = json.loads(context_file.read_text(encoding="utf-8"))
                if isinstance(candidate, dict) and 0 <= time.time() - float(candidate.get("time", 0)) < 3600:
                    prompt_context = candidate
        except (OSError, ValueError, TypeError):
            pass
        try:
            signal_file.stat()
            force = True
            signal_file.unlink(missing_ok=True)
        except OSError:
            pass
        current = {}
        with os.scandir(project) as entries:
            for entry in entries:
                path = Path(entry.path)
                # Filter names before metadata access: user asset directories or
                # thousands of unrelated files cost no per-entry stat/resolve.
                if (path.name.startswith(".") or path.suffix.lower() not in self.SKETCH_EXTS
                        or not entry.is_file(follow_symlinks=False)):
                    continue
                key = (AIReviewManager._path_key(path) if entry.is_symlink()
                       else os.path.normcase(os.path.abspath(entry.path)))
                try:
                    stat = entry.stat()
                    # Windows directory snapshots can omit file identity and
                    # report different creation metadata than a handle stat.
                    # Use the same reliable identity for scan and read checks.
                    if not stat.st_ino:
                        stat = path.stat()
                    current[key] = (path, self._signature(stat))
                except OSError:
                    continue
        for key, (path, signature) in current.items():
            if not ready:
                try:
                    contents[key] = AIReviewManager._read_text_exact(path)
                    signatures[key] = signature
                except OSError:
                    pass
                continue
            previous = signatures.get(key)
            if previous != signature or force:
                item = pending.setdefault(key, {
                    "path": str(path), "before": contents.get(key, ""),
                    "before_exists": previous is not None,
                    "last_change_time": now, "last_mtime": signature,
                })
                if item["last_mtime"] != signature:
                    item["last_mtime"] = signature
                    item["last_change_time"] = now
        if ready:
            for key in set(signatures) - set(current):
                item = pending.setdefault(key, {
                    "path": key, "before": contents.get(key, ""), "before_exists": True,
                    "last_change_time": now, "last_mtime": None,
                })
                if item["last_mtime"] is not None:
                    item["last_mtime"] = None
                    item["last_change_time"] = now
        pending = {key: value for key, value in pending.items() if key in current or key in signatures}
        events = []
        for key, item in list(pending.items()):
            if now - item["last_change_time"] < 0.35:
                continue
            if key not in current:
                pending.pop(key, None)
                contents.pop(key, None)
                signatures.pop(key, None)
                events.append((key, item["path"], item["before"], "", True, False))
                continue
            path, signature = current[key]
            try:
                after = retry_transient_file_operation(
                    lambda: AIReviewManager._read_text_exact(path), attempts=3, delay=0.05)
                # Do not combine bytes from an in-flight replacement with its
                # previous signature. Wait for another settled pass instead.
                if signature != self._signature(path.stat()):
                    item["last_change_time"] = time.monotonic()
                    continue
            except OSError:
                continue
            before, exists = item["before"], item["before_exists"]
            signatures[key], contents[key] = signature, after
            pending.pop(key, None)
            if before != after or not exists:
                events.append((key, str(path), before, after, exists, True))

        published = []
        with self._lock:
            if self._closed or generation != self._generation:
                return {"generation": generation, "events": []}
            # Manual saves while a slow scan was reading win for that file.
            for key in set(current) | set(self._baseline_mtimes):
                if versions.get(key, 0) != self._save_versions.get(key, 0):
                    continue
                if key in contents:
                    self._baseline_contents[key] = contents[key]
                    self._baseline_mtimes[key] = signatures[key]
                elif key not in signatures:
                    self._baseline_contents.pop(key, None)
                    self._baseline_mtimes.pop(key, None)
                if key in pending:
                    self._pending_settle[key] = pending[key]
                else:
                    self._pending_settle.pop(key, None)
            self._baseline_ready = True
            for key in list(self._pending_settle):
                if key not in current and key not in signatures:
                    self._pending_settle.pop(key, None)
            eligible = [event for event in events
                        if versions.get(event[0], 0) == self._save_versions.get(event[0], 0)
                        and event[0] not in self._user_operations]
        # Journal I/O is rare, but must also stay off the GUI thread. The
        # manager checks the captured project atomically with journal mutation.
        for key, *event in eligible:
            with self._lock:
                valid = (not self._closed and generation == self._generation
                         and versions.get(key, 0) == self._save_versions.get(key, 0)
                         and key not in self._user_operations)
            if not valid:
                continue
            def still_valid():
                with self._lock:
                    return (not self._closed and generation == self._generation
                            and versions.get(key, 0) == self._save_versions.get(key, 0)
                            and key not in self._user_operations)
            result = self.review_manager.queue_ai_edit_snapshot(
                *event, expected_project=project, expected_valid=still_valid,
                prompt_context=prompt_context)
            if result and result != "cancelled":
                published.append((key, *event))
        return {"generation": generation, "events": published,
                "files": [str(path) for path, _ in current.values()], "save_versions": versions}

    def _finish_scan(self, result) -> None:
        """Queued GUI completion: Qt watcher/timer changes never run in workers."""
        with self._lock:
            self._scan_running = False
            valid = not self._closed and result.get("generation") == self._generation
            again = self._scan_pending
            self._scan_pending = False
            settling = bool(self._pending_settle)
        if valid and not result.get("error"):
            # Watching actual sources matters on Linux: directory events alone
            # need not report modifications to existing files. Bound kernel slots.
            wanted = set(result.get("files", [])[:512])
            existing = set(self._fs_watcher.files())
            if existing - wanted:
                self._fs_watcher.removePaths(list(existing - wanted))
            if wanted - existing:
                self._fs_watcher.addPaths(list(wanted - existing))
            for key, *event in result.get("events", []):
                with self._lock:
                    saved_since_scan = (result.get("save_versions", {}).get(key, 0)
                                        != self._save_versions.get(key, 0))
                if not saved_since_scan:
                    self.ai_edit_detected.emit(*event)
            self._timer.setInterval(self._settle_interval if settling else self._idle_interval)
        if not self._closed and (again or not valid):
            self._wake_timer.start()

    def shutdown(self) -> None:
        self._timer.stop()
        self._wake_timer.stop()
        with self._lock:
            self._closed = True
            self._generation += 1
            self._scan_pending = False
