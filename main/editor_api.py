#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import os
import time
import json
import re
import shutil
import tempfile
import subprocess
import threading
import queue
import ctypes
import traceback
import hashlib
from collections import deque
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, font as tkfont


from main.core.constants import *
from main.core.theme import *
from main.core.config import *
from main.core.file_utils import *
from main.core.toolchain import *
from main.core.board_catalog import *
from main.core.board_compat import *
from main.widgets import *

EDITOR_WINDOW_TITLE = "MCU Flasher — Embedded Code Editor (Closing this window will attach back to the MAIN window)"

# Windows-only: lets us reparent the editor's native window into the
# Tkinter frame via the Win32 API. Import is best-effort — if pywin32
# isn't installed, the app still runs fine, it just falls back to the
# old "Open Editor Window" popup behavior instead of true embedding.
win32gui = None
win32con = None
win32process = None
_wm_set_embedded = 0
if sys.platform == "win32":
    try:
        import win32gui
        import win32con
        import win32process
        import ctypes
        _wm_set_embedded = ctypes.windll.user32.RegisterWindowMessageW("MCU_Flash_Set_Embedded")
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("naph.mcuflasher.gui.v3")
    except Exception:
        pass

# Global event to signal when the user cancels project selection at startup
project_cancelled = threading.Event()


def _list_own_toplevel_hwnds() -> set:
    """Enumerate all top-level window handles belonging to this process.
    Used to spot the pywebview editor window by "what showed up" rather
    than by title, since some backends silently rewrite the window title
    to match the loaded page's <title> tag."""
    if win32gui is None or win32process is None:
        return set()
    my_pid = os.getpid()
    hwnds = []

    def _cb(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == my_pid:
                hwnds.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return set(hwnds)

class MonacoAutosaveWorker:
    """Single-timer Monaco autosave debounce.

    The previous implementation submitted one sleeping thread-pool job for
    every keystroke. On a low-end device those jobs accumulated and could keep
    autosave minutes behind the editor. One cancellable timer gives the same
    debounce semantics with at most one background thread.
    """
    def __init__(self, gui):
        self.gui = gui
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def start(self):
        return

    def stop(self):
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def update_state(self):
        """Update thread pool worker state based on current configuration."""
        if getattr(self.gui, "autosave_enabled", False):
            self.start()
        else:
            self.stop()

    def notify_edit(self):
        if not getattr(self.gui, "autosave_enabled", False):
            self.stop()
            return
        delay_ms = max(200, int(getattr(self.gui, "autosave_delay_ms", 1500)))
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(delay_ms / 1000.0, self._request_save)
            self._timer.daemon = True
            self._timer.start()

    def _request_save(self):
        with self._lock:
            self._timer = None
        def _do_save():
            if not getattr(self.gui, "autosave_enabled", False):
                return
            if hasattr(self.gui, "editor_api") and self.gui.editor_api:
                dirty = any(self.gui.editor_api.modified_files.values())
                if dirty:
                    if hasattr(self.gui, "_save_all_editor_files") and callable(self.gui._save_all_editor_files):
                        self.gui._save_all_editor_files()

        if hasattr(self.gui, "root") and self.gui.root:
            try:
                self.gui._post_ui(_do_save)
            except Exception:
                pass

def build_ai_line_diff(before_content: str, after_content: str) -> dict:
    """Build compact Monaco decoration ranges for one external AI edit."""
    import difflib

    before_lines = str(before_content or "").splitlines()
    after_lines = str(after_content or "").splitlines()
    if before_content == after_content:
        return {"changes": [], "added": 0, "removed": 0, "modified": 0, "firstLine": 1}

    # SequenceMatcher gives accurate VS Code-like hunks for normal sketches.
    # For very large generated files, bound the work to the changed middle
    # region so one AI edit can never freeze the editor bridge.
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

class EditorApi:
    def __init__(self, gui):
        self._gui = gui
        self.active_file_path = None
        self.modified_files = {} # path -> is_modified
        self._pending_ai_edits = {}
        self._pending_ai_lock = threading.Lock()
        # Accepted/rejected AI decisions are reversible for the current editor
        # session. Keep a bounded, multi-level undo/redo history just like an
        # IDE edit stack, without weakening the durable pending-review journal.
        self._ai_decision_history = []
        self._ai_decision_redo = []
        self._ai_decision_history_limit = 50
        project_dir = getattr(gui, "sketch_dir_path", None)
        try:
            self._ai_backup_store = (
                AIEditBackupStore(project_dir) if project_dir else None
            )
            if self._ai_backup_store:
                self._ai_backup_store.current_project = str(project_dir)
        except Exception as exc:
            print(f"[MCU Flasher] Project-local AI edit backup session could not start: {exc}")
            self._ai_backup_store = None
        self._ai_review_revision = 0
        self._ai_review_generation = 0
        self._ai_review_journal_error = ""
        self._ai_review_journal_recovery_required = False
        configured_state_path = getattr(gui, "ai_review_state_path", None)
        self._ai_review_state_path_is_configured = bool(configured_state_path)
        if configured_state_path:
            self._ai_review_state_path = Path(configured_state_path)
        elif project_dir:
            try:
                self._ai_review_state_path = get_ai_review_state_file(project_dir)
            except Exception as exc:
                # A temporarily unavailable/removable project drive must not
                # prevent the editor from opening. AI history remains in RAM;
                # persistent review/backup creation is retried on project bind.
                print(f"[MCU Flasher] Project-local AI review journal unavailable: {exc}")
                self._ai_review_state_path = None
        else:
            self._ai_review_state_path = None
        self._load_pending_ai_edits()

    @staticmethod
    def _path_key(path):
        try:
            return os.path.normcase(str(Path(path or "").resolve(strict=False)))
        except (OSError, ValueError):
            return os.path.normcase(os.path.abspath(str(path or "")))

    def bind_project(self, project_dir):
        """Switch the review journal and backup session with the project."""
        new_review_state_path = self._ai_review_state_path
        if not self._ai_review_state_path_is_configured:
            try:
                new_review_state_path = get_ai_review_state_file(project_dir)
            except Exception as exc:
                print(f"[MCU Flasher] Project-local AI review journal unavailable: {exc}")
                new_review_state_path = None

        with self._pending_ai_lock:
            if self._ai_review_journal_recovery_required:
                raise OSError(
                    self._ai_review_journal_error
                    or "The current project's AI review journal needs manual recovery."
                )
            try:
                self._commit_pending_ai_edits_locked()
            except Exception as exc:
                self._ai_review_journal_error = (
                    "The current project's AI review journal could not be saved "
                    f"before switching projects: {exc}"
                )
                raise OSError(self._ai_review_journal_error) from exc
            self._pending_ai_edits = {}
            self._ai_decision_history = []
            self._ai_decision_redo = []
            old_backup_store = self._ai_backup_store
            self._ai_backup_store = None
            self._ai_review_revision = 0
            self._ai_review_generation = 0
            self._ai_review_journal_error = ""
            self._ai_review_journal_recovery_required = False
            if not self._ai_review_state_path_is_configured:
                self._ai_review_state_path = new_review_state_path
            self.modified_files.clear()
            self.active_file_path = None
            self._load_pending_ai_edits_locked()

        # Finish the previous project's asynchronous backup session only after
        # its review journal has committed, then start a fresh dated session in
        # the newly selected sketch folder.  Switching back later creates the
        # next sessionN directory, exactly like a new app session for that project.
        if old_backup_store:
            try:
                old_backup_store.shutdown(timeout=1.0)
            except Exception as exc:
                print(f"[MCU Flasher] Could not close previous AI backup session: {exc}")
        try:
            self._ai_backup_store = AIEditBackupStore(project_dir)
            self._ai_backup_store.current_project = str(project_dir or "")
        except Exception as exc:
            print(f"[MCU Flasher] Project-local AI edit backup session could not start: {exc}")
            self._ai_backup_store = None

    def _path_is_in_project(self, path):
        project_dir = getattr(self._gui, "sketch_dir_path", None)
        if not project_dir or not path:
            return False
        try:
            project_root = Path(project_dir).resolve(strict=False)
            candidate = Path(path).resolve(strict=False)
            return os.path.commonpath(
                [os.path.normcase(str(project_root)), os.path.normcase(str(candidate))]
            ) == os.path.normcase(str(project_root))
        except (OSError, ValueError):
            return False

    @staticmethod
    def _read_text_exact(path):
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as stream:
            return stream.read()

    @staticmethod
    def _write_text_atomic(path, content):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_file_writable(target)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.ai-review-",
            suffix=".tmp",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(
                file_descriptor, "w", encoding="utf-8", errors="strict", newline=""
            ) as stream:
                stream.write(str(content))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    def _next_ai_review_revision_locked(self):
        self._ai_review_revision += 1
        return str(self._ai_review_revision)

    def _ai_review_state_data_locked(self, generation=None):
        return {
            "version": 2,
            "generation": (
                self._ai_review_generation if generation is None else int(generation)
            ),
            "revision": self._ai_review_revision,
            "reviews": list(self._pending_ai_edits.values()),
        }

    def _ai_review_backup_path(self):
        state_path = self._ai_review_state_path
        return state_path.with_suffix(state_path.suffix + ".bak") if state_path else None

    @staticmethod
    def _write_ai_review_journal_atomic(path, data):
        """Atomically replace one journal replica with fully synced JSON."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_file_writable(target)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(
                file_descriptor, "w", encoding="utf-8", errors="strict", newline=""
            ) as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    def _persist_pending_ai_edits_replicated_locked(self):
        """Commit the next journal generation to the recovery replica first.

        The backup is the commit point.  If replacing the primary is interrupted,
        startup can select the newer backup by generation without losing a review.
        Empty review sets are written as tombstones instead of deleting the files,
        so an older replica can never resurrect a resolved review.
        """
        state_path = self._ai_review_state_path
        if self._ai_review_journal_recovery_required:
            raise OSError(
                self._ai_review_journal_error
                or "The AI review journal needs manual recovery."
            )
        if not state_path:
            if self._pending_ai_edits:
                raise OSError("No AI review journal path is configured.")
            return
        backup_path = self._ai_review_backup_path()
        if not backup_path:
            raise OSError("No AI review recovery journal path is configured.")

        generation = max(
            self._ai_review_generation + 1,
            self._ai_review_revision,
        )
        data = self._ai_review_state_data_locked(generation)

        # Backup-first ordering guarantees that a committed generation is never
        # represented only by the more fragile primary replica.
        self._write_ai_review_journal_atomic(backup_path, data)
        self._ai_review_generation = generation
        try:
            self._write_ai_review_journal_atomic(state_path, data)
        except Exception as exc:
            # The newest generation is already durable in the backup.  Treat the
            # commit as successful; startup will prefer it and repair naturally
            # on the next journal write.
            print(f"[MCU Flasher] AI review primary replica is stale: {exc}")
        self._ai_review_journal_error = ""
        self._ai_review_journal_recovery_required = False

    def _persist_pending_ai_edits_fallback_locked(self):
        """Independent retry entry point used after a failed journal commit."""
        self._persist_pending_ai_edits_replicated_locked()

    def _persist_pending_ai_edits_locked(self):
        self._persist_pending_ai_edits_replicated_locked()

    def _commit_pending_ai_edits_locked(self):
        """Commit through either entry point or raise without hiding failure."""
        try:
            self._persist_pending_ai_edits_locked()
        except Exception as primary_exc:
            try:
                self._persist_pending_ai_edits_fallback_locked()
            except Exception as fallback_exc:
                self._ai_review_journal_error = (
                    "AI review journal commit failed: "
                    f"{primary_exc}; retry failed: {fallback_exc}"
                )
                raise OSError(self._ai_review_journal_error) from fallback_exc

    def _load_pending_ai_edits(self):
        with self._pending_ai_lock:
            self._load_pending_ai_edits_locked()

    def _load_pending_ai_edits_locked(self):
        state_path = self._ai_review_state_path
        if not state_path:
            self._pending_ai_edits = {}
            self._ai_review_revision = 0
            self._ai_review_generation = 0
            self._ai_review_journal_error = ""
            self._ai_review_journal_recovery_required = False
            return
        candidates = [state_path]
        backup_path = self._ai_review_backup_path()
        if backup_path:
            candidates.append(backup_path)
        existing_candidates = [candidate for candidate in candidates if candidate.exists()]
        if not existing_candidates:
            self._pending_ai_edits = {}
            self._ai_review_revision = 0
            self._ai_review_generation = 0
            self._ai_review_journal_error = ""
            self._ai_review_journal_recovery_required = False
            return

        load_errors = []
        failed_candidates = set()
        loaded_candidates = []
        for candidate in existing_candidates:
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("journal root must be an object")
                version = int(data.get("version", 1) or 1)
                if version not in (1, 2):
                    raise ValueError(f"unsupported journal version {version}")
                reviews = data.get("reviews", [])
                if not isinstance(reviews, list):
                    raise ValueError("journal reviews must be a list")
                revision = int(data.get("revision", 0) or 0)
                if revision < 0:
                    raise ValueError("journal revision cannot be negative")
                if version >= 2:
                    generation = int(data.get("generation", -1))
                    if generation < 0:
                        raise ValueError("journal generation is missing or negative")
                else:
                    # Version 1 used revision as its only monotonic value.  It is
                    # safe while the primary exists, but never authoritative as
                    # a backup-only recovery because old backups were one write
                    # behind.
                    generation = revision

                loaded = {}
                normalized_reviews = []
                for raw_value in reviews:
                    if not isinstance(raw_value, dict):
                        raise ValueError("journal contains a non-object review")
                    raw = dict(raw_value)
                    if not self._path_is_in_project(raw.get("path")):
                        raise ValueError("journal contains a review outside the active project")
                    before_content = str(raw.get("beforeContent", ""))
                    after_content = str(raw.get("content", ""))
                    raw["beforeContent"] = before_content
                    raw["content"] = after_content
                    raw["beforeExists"] = bool(raw.get("beforeExists", True))
                    raw["afterExists"] = bool(raw.get("afterExists", True))
                    raw["diff"] = build_ai_line_diff(before_content, after_content)
                    raw["revision"] = str(raw.get("revision", "0"))
                    key = self._path_key(raw["path"])
                    if key in loaded:
                        raise ValueError("journal contains duplicate review paths")
                    loaded[key] = raw
                    normalized_reviews.append(raw)

                effective_revision = max(
                    revision,
                    max((int(item.get("revision", 0) or 0) for item in loaded.values()), default=0),
                )
                signature = json.dumps(
                    {
                        "revision": effective_revision,
                        "reviews": normalized_reviews,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                loaded_candidates.append({
                    "path": candidate,
                    "version": version,
                    "generation": generation,
                    "revision": effective_revision,
                    "reviews": loaded,
                    "signature": signature,
                })
            except Exception as exc:
                load_errors.append(f"{candidate.name}: {exc}")
                failed_candidates.add(candidate)

        primary = next(
            (item for item in loaded_candidates if item["path"] == state_path), None
        )
        backup = next(
            (item for item in loaded_candidates if item["path"] == backup_path), None
        )

        # A version-1 backup may be the deliberately stale copy left behind by
        # the previous writer, including after the last review was resolved.
        # Never resurrect it when its primary is absent or corrupt.
        if not primary and backup and backup["version"] < 2:
            load_errors.append(
                f"{backup_path.name}: legacy backup cannot prove it is the newest generation"
            )
            loaded_candidates = []

        # In v2 the backup is written first and is the commit point.  If it is
        # present but unreadable, a valid primary may still be an older
        # generation from an interrupted replacement, so selecting it could
        # silently omit the newest review.
        if primary and primary["version"] >= 2 and backup_path in failed_candidates:
            load_errors.append(
                f"{backup_path.name}: corrupt commit replica makes the primary ambiguous"
            )
            loaded_candidates = []

        if loaded_candidates:
            # A valid legacy primary remains authoritative until the first v2
            # write migrates it.  Version 2 replicas are selected strictly by
            # committed generation, regardless of filename.
            if primary and primary["version"] < 2:
                newest = [primary]
            else:
                newest_generation = max(
                    item["generation"] for item in loaded_candidates
                )
                newest = [
                    item for item in loaded_candidates
                    if item["generation"] == newest_generation
                ]
            signatures = {item["signature"] for item in newest}
            if len(signatures) != 1:
                load_errors.append(
                    "journal replicas disagree within the newest committed generation"
                )
                loaded_candidates = []
            else:
                selected = next(
                    (item for item in newest if item["path"] == state_path), newest[0]
                )
                self._pending_ai_edits = selected["reviews"]
                self._ai_review_revision = selected["revision"]
                self._ai_review_generation = selected["generation"]
                self._ai_review_journal_error = ""
                self._ai_review_journal_recovery_required = False
                if selected["path"] == backup_path:
                    print("[MCU Flasher] Recovered newest AI review journal from backup.")
                return

        # Fail closed: malformed or ambiguous journals may contain the only
        # exact Reject copy.  Compile/Upload remains blocked until recovery.
        self._pending_ai_edits = {}
        self._ai_review_revision = 0
        self._ai_review_generation = 0
        self._ai_review_journal_error = "; ".join(load_errors) or (
            "No trustworthy AI review journal generation could be loaded."
        )
        self._ai_review_journal_recovery_required = True

    def _snapshot_for_key_locked(self, key):
        payload = self._pending_ai_edits.get(key)
        if not payload:
            return {}
        keys = list(self._pending_ai_edits)
        snapshot = dict(payload)
        snapshot["pendingCount"] = len(keys)
        snapshot["reviewIndex"] = keys.index(key) + 1
        snapshot["fileName"] = Path(snapshot["path"]).name
        return snapshot

    def queue_ai_edit_snapshot(
        self,
        path,
        before_content,
        after_content,
        before_exists=True,
        after_exists=True,
    ):
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
                # A resolved edit on the same file must create a new backup
                # record (edit2.txt, edit3.txt, ...), while updates to one
                # still-pending proposal retain their existing review ID.
                review_id = f"{key}:{time.time_ns()}"
            if existing and original_exists == after_exists and original_content == after_content:
                self._pending_ai_edits.pop(key, None)
                self._next_ai_review_revision_locked()
                try:
                    self._commit_pending_ai_edits_locked()
                except Exception as exc:
                    print(f"[MCU Flasher] Could not persist AI review cancellation: {exc}")
                    # The old journal still says this review is pending.  Keep
                    # memory consistent with that durable state and fail closed.
                    self._pending_ai_edits[key] = existing
                    return False
                if self._ai_backup_store:
                    try:
                        self._ai_backup_store.mark_cancelled(existing)
                    except Exception as exc:
                        print(f"[MCU Flasher] Could not queue cancelled AI backup: {exc}")
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
            # A new AI write branches the edit timeline, so redo entries from
            # an earlier undone decision are no longer valid.
            self._ai_decision_redo.clear()
            try:
                self._commit_pending_ai_edits_locked()
            except Exception as exc:
                # Retain the exact Reject copy in memory and let the journal
                # error block Compile/Upload until storage becomes writable.
                print(f"[MCU Flasher] Could not persist pending AI review: {exc}")
            if self._ai_backup_store:
                try:
                    payload["project"] = str(getattr(self._gui, "sketch_dir_path", "") or "")
                    payload["backupFile"] = self._ai_backup_store.record_edit(
                        payload, status="pending"
                    )
                except Exception as exc:
                    print(f"[MCU Flasher] Could not queue AI edit backup: {exc}")
        return True

    def consume_ai_edit_snapshot(self, path):
        """Compatibility name retained for JavaScript; this is now a safe peek.

        A review is removed only after an explicit Accept or Reject response.
        """
        with self._pending_ai_lock:
            return self._snapshot_for_key_locked(self._path_key(path))

    def get_ai_edit_reviews(self):
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

    def has_pending_ai_edit(self, path):
        with self._pending_ai_lock:
            return self._path_key(path) in self._pending_ai_edits

    def has_any_pending_ai_edits(self):
        with self._pending_ai_lock:
            return bool(self._pending_ai_edits) or bool(self._ai_review_journal_error)

    def get_ai_review_journal_error(self):
        with self._pending_ai_lock:
            return self._ai_review_journal_error

    def _resolve_ai_edit(self, path, revision, accept):
        key = self._path_key(path)
        with self._pending_ai_lock:
            payload = self._pending_ai_edits.get(key)
            if not payload:
                return {"success": False, "error": "This AI review is no longer pending."}
            if str(revision or "") != str(payload.get("revision", "")):
                return {
                    "success": False,
                    "conflict": True,
                    "error": "The AI edit changed while it was being reviewed. The latest version has been reopened.",
                    "snapshot": self._snapshot_for_key_locked(key),
                }
            if not self._path_is_in_project(payload.get("path")):
                return {"success": False, "error": "The reviewed file is outside the active project."}

            target = Path(payload["path"])
            actual_exists = target.is_file()
            actual_content = self._read_text_exact(target) if actual_exists else ""
            if (actual_exists != bool(payload.get("afterExists", True))
                    or actual_content != str(payload.get("content", ""))):
                # Adopt the current disk state as a new proposal revision. The
                # user gets to inspect it before deciding again, while Reject
                # still targets the first pre-AI original.
                payload["afterExists"] = actual_exists
                payload["content"] = actual_content
                payload["revision"] = self._next_ai_review_revision_locked()
                payload["diff"] = build_ai_line_diff(
                    payload.get("beforeContent", ""), actual_content
                )
                try:
                    self._commit_pending_ai_edits_locked()
                except Exception as exc:
                    print(f"[MCU Flasher] Could not persist refreshed AI review: {exc}")
                if self._ai_backup_store:
                    try:
                        payload["project"] = str(getattr(self._gui, "sketch_dir_path", "") or "")
                        payload["backupFile"] = self._ai_backup_store.record_edit(
                            payload, status="pending-refreshed"
                        )
                    except Exception as exc:
                        print(f"[MCU Flasher] Could not refresh AI edit backup: {exc}")
                return {
                    "success": False,
                    "conflict": True,
                    "error": "The file changed during review. The latest version is now shown; review it again.",
                    "snapshot": self._snapshot_for_key_locked(key),
                }

            try:
                if not accept:
                    if bool(payload.get("beforeExists", True)):
                        self._write_text_atomic(target, payload.get("beforeContent", ""))
                    elif target.exists():
                        ensure_file_writable(target)
                        target.unlink()
            except Exception as exc:
                return {
                    "success": False,
                    "error": f"Could not restore the original file: {exc}",
                }

            resolved_payload = dict(payload)
            final_exists = (
                bool(resolved_payload.get("afterExists", True))
                if accept else bool(resolved_payload.get("beforeExists", True))
            )
            final_content = (
                str(resolved_payload.get("content", ""))
                if accept else str(resolved_payload.get("beforeContent", ""))
            )
            controller = getattr(self._gui, "ai_controller", None)
            if controller:
                # Rebaseline before potentially slow journal fsync work so the
                # watcher cannot report Reject's restoration as a new AI edit.
                controller.note_local_save(
                    resolved_payload["path"], final_content if final_exists else None
                )
            decision_entry = {
                "decisionId": f"{self._next_ai_review_revision_locked()}:{key}",
                "sourceReviewId": resolved_payload.get("reviewId", key),
                "reviewId": resolved_payload.get("reviewId", key),
                "project": str(getattr(self._gui, "sketch_dir_path", "") or ""),
                "path": resolved_payload["path"],
                "fileName": Path(resolved_payload["path"]).name,
                "action": "accepted" if accept else "rejected",
                "originalBeforeExists": bool(resolved_payload.get("beforeExists", True)),
                "originalAfterExists": bool(resolved_payload.get("afterExists", True)),
                "originalBeforeContent": str(resolved_payload.get("beforeContent", "")),
                "originalAfterContent": str(resolved_payload.get("content", "")),
                # State currently applied after the decision.
                "appliedExists": final_exists,
                "appliedContent": final_content,
                # State that Undo should restore. Accept -> original;
                # Reject -> the AI-proposed version that was rejected.
                "undoExists": (
                    bool(resolved_payload.get("beforeExists", True))
                    if accept else bool(resolved_payload.get("afterExists", True))
                ),
                "undoContent": (
                    str(resolved_payload.get("beforeContent", ""))
                    if accept else str(resolved_payload.get("content", ""))
                ),
            }
            pending_before_resolution = dict(self._pending_ai_edits)
            self._pending_ai_edits.pop(key, None)
            try:
                self._commit_pending_ai_edits_locked()
            except Exception as exc:
                # Do not report a successful decision while durable storage
                # still contains the old review.  Keeping it pending makes a
                # later retry/restart honest and the journal error blocks build
                # actions until the state can be committed safely.
                self._pending_ai_edits = pending_before_resolution
                return {
                    "success": False,
                    "journalError": True,
                    "error": (
                        "The AI review decision could not be saved and remains "
                        f"pending: {exc}"
                    ),
                    "snapshot": self._snapshot_for_key_locked(key),
                }
            if self._ai_backup_store:
                try:
                    resolved_payload["project"] = decision_entry["project"]
                    decision_entry["backupFile"] = self._ai_backup_store.record_edit(
                        resolved_payload,
                        status=decision_entry["action"],
                        decision_entry=decision_entry,
                    )
                except Exception as exc:
                    print(f"[MCU Flasher] Could not queue resolved AI backup: {exc}")
            self._ai_decision_history.append(decision_entry)
            limit = max(1, int(getattr(self, "_ai_decision_history_limit", 50)))
            if len(self._ai_decision_history) > limit:
                del self._ai_decision_history[:-limit]
            self._ai_decision_redo.clear()
            next_path = next(iter(self._pending_ai_edits.values()), {}).get("path", "")
            pending_count = len(self._pending_ai_edits)

        self.modified_files[resolved_payload["path"]] = False

        def _notify_resolution():
            if hasattr(self._gui, "_update_skip_compile_state"):
                self._gui._update_skip_compile_state()
            if hasattr(self._gui, "_update_editor_info"):
                self._gui._update_editor_info()
            if hasattr(self._gui, "_append_notif"):
                verb = "accepted" if accept else "rejected"
                name = Path(resolved_payload["path"]).name
                self._gui._append_notif(
                    f"  AI edit {verb}: {name}",
                    "success" if accept else "warning",
                    category="system",
                    title=f"AI edit {verb}",
                )

        if self._gui:
            try:
                self._gui._post_ui(_notify_resolution)
            except Exception:
                pass
        result = {
            "success": True,
            "action": "accepted" if accept else "rejected",
            "path": resolved_payload["path"],
            "beforeExists": bool(resolved_payload.get("beforeExists", True)),
            "afterExists": bool(resolved_payload.get("afterExists", True)),
            "nextPath": next_path,
            "pendingCount": pending_count,
            "undoAvailable": True,
        }
        return result

    def _ai_history_summary_locked(self):
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
        backup_state = (
            self._ai_backup_store.get_memory_state()
            if self._ai_backup_store else {}
        )
        return {
            "canUndo": bool(undo_entry),
            "canRedo": bool(redo_entry),
            "undoDepth": len(self._ai_decision_history),
            "redoDepth": len(self._ai_decision_redo),
            "undo": _summary(undo_entry),
            "redo": _summary(redo_entry),
            "backup": backup_state,
        }

    def get_ai_history_state(self):
        with self._pending_ai_lock:
            return self._ai_history_summary_locked()

    def _apply_ai_history_decision(self, direction: str, force=False):
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
            if not self._path_is_in_project(path):
                return {
                    "success": False,
                    "error": "The AI history file is outside the active project.",
                    "history": self._ai_history_summary_locked(),
                }

            target = Path(path)
            expected_exists = bool(entry.get("appliedExists" if undoing else "undoExists", True))
            expected_content = str(entry.get("appliedContent" if undoing else "undoContent", ""))
            target_exists = bool(entry.get("undoExists" if undoing else "appliedExists", True))
            target_content = str(entry.get("undoContent" if undoing else "appliedContent", ""))

            actual_exists = target.is_file()
            actual_content = self._read_text_exact(target) if actual_exists else ""
            if not force and (actual_exists != expected_exists or actual_content != expected_content):
                return {
                    "success": False,
                    "conflict": True,
                    "error": (
                        "The file changed after this AI decision. Undoing now would "
                        "replace newer edits." if undoing else
                        "The file changed after AI Undo. Redoing now would replace newer edits."
                    ),
                    "path": path,
                    "history": self._ai_history_summary_locked(),
                }

            try:
                if target_exists:
                    self._write_text_atomic(target, target_content)
                elif target.exists():
                    ensure_file_writable(target)
                    target.unlink()
            except Exception as exc:
                return {
                    "success": False,
                    "error": f"Could not {'undo' if undoing else 'redo'} the AI decision: {exc}",
                    "history": self._ai_history_summary_locked(),
                }

            controller = getattr(self._gui, "ai_controller", None)
            if controller:
                controller.note_local_save(path, target_content if target_exists else None)

            source.pop()
            destination.append(entry)
            limit = max(1, int(getattr(self, "_ai_decision_history_limit", 50)))
            if len(destination) > limit:
                del destination[:-limit]
            if self._ai_backup_store:
                try:
                    self._ai_backup_store.record_history_action(
                        entry,
                        "undo" if undoing else "redo",
                        target_exists,
                        target_content,
                    )
                except Exception as exc:
                    print(f"[MCU Flasher] Could not queue AI history backup: {exc}")
            history_state = self._ai_history_summary_locked()

        self.modified_files[path] = False

        def _notify_history_change():
            if hasattr(self._gui, "_update_skip_compile_state"):
                self._gui._update_skip_compile_state()
            if hasattr(self._gui, "_update_editor_info"):
                self._gui._update_editor_info()
            if hasattr(self._gui, "_append_notif"):
                verb = "Undid" if undoing else "Redid"
                action = str(entry.get("action", "AI edit")).rstrip("d")
                self._gui._append_notif(
                    f"  {verb} AI {action}: {entry.get('fileName') or Path(path).name}",
                    "info",
                    category="system",
                    title=f"{verb} AI decision",
                )

        if self._gui:
            try:
                self._gui._post_ui(_notify_history_change)
            except Exception:
                pass

        return {
            "success": True,
            "direction": "undo" if undoing else "redo",
            "action": entry.get("action", "edited"),
            "path": path,
            "fileName": entry.get("fileName") or Path(path).name,
            "exists": target_exists,
            "workspaceShapeChanged": expected_exists != target_exists,
            "history": history_state,
        }

    def undo_ai_edit_decision(self, force=False):
        return self._apply_ai_history_decision("undo", force=bool(force))

    def redo_ai_edit_decision(self, force=False):
        return self._apply_ai_history_decision("redo", force=bool(force))

    def shutdown_ai_edit_backup(self):
        store = getattr(self, "_ai_backup_store", None)
        if store:
            try:
                store.shutdown(timeout=1.5)
                return {"success": True, **store.get_memory_state()}
            except Exception as exc:
                return {"success": False, "error": str(exc)}
        return {"success": True}

    def accept_ai_edit(self, path, revision):
        return self._resolve_ai_edit(path, revision, accept=True)

    def reject_ai_edit(self, path, revision):
        return self._resolve_ai_edit(path, revision, accept=False)

    def on_editor_content_change(self):
        if self._gui and hasattr(self._gui, "_monaco_autosave_worker"):
            self._gui._monaco_autosave_worker.notify_edit()

    def get_theme_mode(self):
        return get_theme_mode()

    def get_project_files(self):
        project_dir = getattr(self._gui, "sketch_dir_path", None)
        if not project_dir:
            return []
        sketch_dir = Path(project_dir)
        if not sketch_dir.exists():
            return []
        supported_suffixes = {".ino", ".cpp", ".c", ".h", ".hpp", ".txt"}
        files = get_sketch_files_fast(sketch_dir, supported_suffixes)
        # A project file must have one editor tab even if a network share,
        # junction, or overlapping directory refresh reports the same path
        # more than once. Keep the first filesystem entry and compare paths
        # case-insensitively on Windows.
        unique_files = []
        seen_paths = set()
        for file_path in files:
            key = os.path.normcase(os.path.abspath(str(file_path)))
            if key in seen_paths:
                continue
            seen_paths.add(key)
            unique_files.append(file_path)
        files = unique_files
        files.sort(key=lambda item: str(item.relative_to(sketch_dir)).lower())

        order_file = get_project_build_cache_root(sketch_dir, create=False) / ".mcu_flash_tab_order.json"
        if order_file.exists():
            try:
                import json
                saved_order = json.loads(order_file.read_text(encoding="utf-8"))
                file_map = {}
                for f in files:
                    try:
                        rel = str(f.relative_to(sketch_dir))
                    except Exception:
                        rel = str(f)
                    file_map.setdefault(os.path.normcase(rel), f)
                ordered_files = []
                ordered_keys = set()
                for name in saved_order:
                    key = os.path.normcase(str(name))
                    if key in file_map and key not in ordered_keys:
                        ordered_files.append(file_map[key])
                        ordered_keys.add(key)
                        file_map.pop(key, None)
                ordered_files.extend(file_map.values())
                files = ordered_files
            except Exception:
                pass

        return [{"name": f.name, "path": str(f)} for f in files]

    def save_tab_order(self, paths):
        if not self._gui or not self._gui.sketch_dir_path:
            return {"success": False}
        order_file = get_project_build_cache_root(self._gui.sketch_dir_path) / ".mcu_flash_tab_order.json"
        try:
            import json
            normalized_paths = []
            seen_paths = set()
            for p in paths:
                try:
                    path_obj = Path(p)
                    if path_obj.is_absolute():
                        value = str(path_obj.relative_to(self._gui.sketch_dir_path))
                    else:
                        value = str(p)
                except Exception:
                    value = str(p)
                key = os.path.normcase(value)
                if key not in seen_paths:
                    seen_paths.add(key)
                    normalized_paths.append(value)
            ensure_file_writable(order_file)
            order_file.write_text(json.dumps(normalized_paths, indent=2), encoding="utf-8")
            hide_hidden_attribute(order_file)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def read_file(self, path):
        if not self._path_is_in_project(path):
            return {"error": "File is outside the active project."}
        try:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
                content = f.read()
            return {"content": content}
        except Exception as e:
            return {"error": str(e)}

    def save_file(self, path, content):
        if not self._path_is_in_project(path):
            return {"success": False, "error": "File is outside the active project."}
        if self.has_pending_ai_edit(path):
            return {
                "success": False,
                "reviewPending": True,
                "error": "Accept or reject the pending AI edit before saving this file.",
            }
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            if self._gui and getattr(self._gui, "ai_controller", None):
                self._gui.ai_controller.note_local_save(path, content)
            # Trigger skip compile check in Tkinter GUI (thread-safe after call)
            if self._gui:
                self._gui._post_ui(self._gui._update_skip_compile_state)
                self._gui._post_ui(self._gui._update_editor_info)
            return {"success": True}
        except Exception as e:
            return {"error": str(e), "success": False}

    def mark_modified(self, path, is_modified):
        self.modified_files[path] = is_modified
        if is_modified and self._gui and hasattr(self._gui, "_monaco_autosave_worker"):
            self._gui._monaco_autosave_worker.notify_edit()
        if self._gui:
            self._gui._post_ui(self._gui._update_skip_compile_state)

    def set_active_file(self, path):
        self.active_file_path = path
        if self._gui:
            self._gui._post_ui(self._gui._update_editor_info)

    def realtime_check_syntax(self, file_path, content):
        if not self._gui:
            return "[]"
        try:
            from src.syntax_checker import analyze_cpp_syntax
            from pathlib import Path
            import json
            
            p = Path(file_path)
            defined_funcs = self._gui._get_project_defined_functions()
            errors = analyze_cpp_syntax(content, p, defined_funcs)
            
            # Write errors to the project-cache JSON so external readers (like
            # QScintilla) stay in sync without recreating root metadata.
            if self._gui.sketch_dir_path:
                err_file = get_project_temp_file(self._gui.sketch_dir_path, ".mcu_flash_syntax_errors.json")
                try:
                    ensure_file_writable(err_file)
                    err_file.write_text(json.dumps(errors, indent=2), encoding="utf-8")
                    hide_hidden_attribute(err_file)
                except Exception:
                    pass
            
            # Update the bottom panel through the Tk owner's dispatch queue.
            self._gui._post_ui(lambda: self._gui._update_syntax_check_ui(errors))
            
            # Return JSON string of errors to JavaScript
            return json.dumps(errors)
        except Exception as e:
            import json
            return json.dumps([{"severity": "error", "message": f"Syntax checker error: {e}", "line": 1, "col": 1}])

    def run_action(self, action):
        """Run a main-GUI action requested by the detached Monaco toolbar."""
        if not self._gui:
            return {"success": False, "error": "GUI is not available"}
        actions = {
            "compile": self._gui._do_compile,
            "upload": self._gui._do_upload,
            "stop": self._gui._do_stop,
            "clean": self._gui._do_clean,
            "save": self._gui._trigger_save,
            "save_all": self._gui._trigger_save_all,
            "reload": self._gui._reload_current_editor_file,
            "modify": self._gui._open_modify_files_dialog,
        }
        command = actions.get(str(action))
        if command is None:
            return {"success": False, "error": "Unknown action"}
        self._gui._post_ui(command)
        return {"success": True}


__all__ = [
    "EDITOR_WINDOW_TITLE",
    "EditorApi",
    "MonacoAutosaveWorker",
    "_list_own_toplevel_hwnds",
    "_wm_set_embedded",
    "build_ai_line_diff",
    "project_cancelled",
    "win32con",
    "win32gui",
    "win32process"
]
