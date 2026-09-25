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
from pathlib import Path
from typing import Optional, Any, Callable

# pyrefly: ignore [missing-import]
from PySide6.QtCore import QObject, Signal, QTimer

from main.core.file_utils import (
    ensure_file_writable,
    get_ai_review_state_file,
    AIEditBackupStore,
)

def build_ai_line_diff(before_content: str, after_content: str) -> dict:
    """Build compact Monaco decoration ranges for one external AI edit."""
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
        opcodes = [("replace", prefix, old_tail, prefix, new_tail)]
    else:
        opcodes = difflib.SequenceMatcher(
            None, before_lines, after_lines, autojunk=False
        ).get_opcodes()

    changes = []
    added = removed = modified = 0
    first_line = None
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        old_count, new_count = i2 - i1, j2 - j1
        start = max(1, j1 + 1)
        end = max(start, j2)
        if tag == "insert":
            kind = "added"
            added += new_count
        elif tag == "delete":
            kind = "removed"
            removed += old_count
            end = start
        else:
            kind = "modified"
            shared = min(old_count, new_count)
            modified += max(1, shared)
            added += max(0, new_count - old_count)
            removed += max(0, old_count - new_count)
        changes.append({"type": kind, "startLine": start, "endLine": end})
        first_line = start if first_line is None else min(first_line, start)

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
            os.replace(temp_path, target)
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
            os.replace(temp_path, target)
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
            "version": 2,
            "generation": (
                self._ai_review_generation if generation is None else int(generation)
            ),
            "revision": self._ai_review_revision,
            "reviews": list(self._pending_ai_edits.values()),
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
                })
            except Exception:
                continue

        if loaded_candidates:
            loaded_candidates.sort(key=lambda x: x["generation"], reverse=True)
            best = loaded_candidates[0]
            self._pending_ai_edits = best["reviews"]
            self._ai_review_revision = best["revision"]
            self._ai_review_generation = best["generation"]

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
            self._pending_ai_edits[key] = payload
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
    Monitors .ai_edit_signal and file modification times to detect
    AI code applications in real time and trigger Monaco's review banner.
    """
    ai_edit_detected = Signal(str, str, str, bool, bool)  # (path, before, after, before_exists, after_exists)

    SKETCH_EXTS = {".ino", ".cpp", ".c", ".h", ".hpp"}

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
        self._baseline_mtimes: dict[str, float] = {}
        self._pending_settle: dict[str, dict] = {}
        self._last_signal_mtime: float = 0.0
        self._lock = threading.Lock()

        # Qt Timer: runs every 250ms on Qt main thread, completely non-blocking
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._poll_step)

        if self.project_dir and self.project_dir.is_dir():
            self._rebuild_baseline()
            self._timer.start()

    def bind_project(self, project_dir: str | Path) -> None:
        """Switch watcher target when project folder changes."""
        resolved = Path(project_dir).resolve(strict=False) if project_dir else None
        with self._lock:
            self.project_dir = resolved
            self._baseline_contents.clear()
            self._baseline_mtimes.clear()
            self._pending_settle.clear()
            self._last_signal_mtime = 0.0

            if self.project_dir and self.project_dir.is_dir():
                self._rebuild_baseline()
                if not self._timer.isActive():
                    self._timer.start()
            else:
                self._timer.stop()

    def _is_sketch_file(self, path: Path) -> bool:
        if not path.is_file():
            return False
        if path.name.startswith("."):
            return False
        return path.suffix.lower() in self.SKETCH_EXTS

    def _rebuild_baseline(self) -> None:
        if not self.project_dir or not self.project_dir.is_dir():
            return
        try:
            for p in self.project_dir.iterdir():
                if self._is_sketch_file(p):
                    key = AIReviewManager._path_key(p)
                    try:
                        self._baseline_mtimes[key] = p.stat().st_mtime
                        self._baseline_contents[key] = p.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass
        except Exception:
            pass

    def note_user_save(self, path: str | Path, content: Optional[str] = None) -> None:
        """Record manual user saves so they are never flagged as external AI edits."""
        target = Path(path)
        key = AIReviewManager._path_key(target)
        with self._lock:
            self._pending_settle.pop(key, None)
            if target.is_file():
                try:
                    self._baseline_mtimes[key] = target.stat().st_mtime
                    self._baseline_contents[key] = (
                        content if content is not None
                        else target.read_text(encoding="utf-8", errors="replace")
                    )
                except Exception:
                    pass

    def _poll_step(self) -> None:
        if not self.project_dir or not self.project_dir.is_dir():
            return

        now = time.time()
        with self._lock:
            # 1. Check wake-up signal from OpenCode AI
            signal_file = self.project_dir / ".ai_edit_signal"
            if signal_file.exists():
                try:
                    sig_m = signal_file.stat().st_mtime
                    if sig_m > self._last_signal_mtime:
                        self._last_signal_mtime = sig_m
                    signal_file.unlink(missing_ok=True)
                except Exception:
                    pass

            # 2. Check current files in project directory
            try:
                current_files = {
                    AIReviewManager._path_key(p): p
                    for p in self.project_dir.iterdir()
                    if self._is_sketch_file(p)
                }
            except Exception:
                return

            # Check for modified or new files
            for key, path in current_files.items():
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue

                old_mtime = self._baseline_mtimes.get(key)
                if old_mtime is None:
                    # New file created
                    if key not in self._pending_settle:
                        self._pending_settle[key] = {
                            "path": str(path),
                            "before": "",
                            "before_exists": False,
                            "first_seen": now,
                            "last_mtime": mtime,
                        }
                    else:
                        self._pending_settle[key]["last_mtime"] = mtime
                elif abs(mtime - old_mtime) > 0.001:
                    # File modified
                    if key not in self._pending_settle:
                        self._pending_settle[key] = {
                            "path": str(path),
                            "before": self._baseline_contents.get(key, ""),
                            "before_exists": True,
                            "first_seen": now,
                            "last_mtime": mtime,
                        }
                    else:
                        self._pending_settle[key]["last_mtime"] = mtime

            # 3. Process settled edits (debounce: mtime stable for >= 250ms)
            to_emit = []
            for key, pending in list(self._pending_settle.items()):
                if now - pending["first_seen"] >= 0.25:
                    p = Path(pending["path"])
                    if p.is_file():
                        try:
                            after_content = p.read_text(encoding="utf-8", errors="replace")
                            mtime = p.stat().st_mtime
                        except Exception:
                            continue

                        before_content = pending["before"]
                        before_exists = pending["before_exists"]
                        if before_content != after_content or not before_exists:
                            to_emit.append((str(p), before_content, after_content, before_exists, True))
                            self._baseline_contents[key] = after_content
                            self._baseline_mtimes[key] = mtime
                    del self._pending_settle[key]

        # 4. Trigger review queue and UI signals
        for fp, before_c, after_c, b_exists, a_exists in to_emit:
            res = self.review_manager.queue_ai_edit_snapshot(
                fp, before_c, after_c, b_exists, a_exists
            )
            if res and res != "cancelled":
                self.ai_edit_detected.emit(fp, before_c, after_c, b_exists, a_exists)
