#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import os
import json
import re
import threading
from typing import TYPE_CHECKING
from pathlib import Path
import tkinter as tk
from tkinter import messagebox


from main.core.constants import *
from main.core.theme import *
from main.core.config import *
from main.core.file_utils import *
from main.core.toolchain import *
from main.core.board_catalog import *
from main.core.board_compat import *
from main.widgets import *
from main.dialogs import *
from main.editor_api import *

if TYPE_CHECKING:
    from main.mcu_flash_gui import MCUUploadGUI
    _Base = MCUUploadGUI
else:
    _Base = object

class CleanBuildMixin(_Base):
    """Mixin providing CleanBuildMixin capabilities for MCUUploadGUI."""
    def _capture_stale_clean_path(self, report_text: str, stash: list):
        """Extract the backtick-quoted path from a PlatformIO 'Please manually
        remove the file `...`' report and remember it for automatic retry."""
        if report_text is None:
            return
        _m = re.search(r"`([^`]+)`", report_text)
        if _m:
            _p = _m.group(1).strip()
            if _p and _p not in stash:
                stash.append(_p)

    def _auto_clean_stale_build_paths(self, reported_paths: list, env_name: str,
                                      build_ok: bool, build_root: Path | None = None):
        """Retry deletion of build paths PlatformIO could not remove itself
        (WinError 145 — file locked by antivirus/indexer/another handle).
        Must be called AFTER the PlatformIO process has fully exited so the
        locks have a chance to clear. Fully automated — the user no longer
        needs to manually delete anything.

        Safe-guards: when the build succeeded, the CURRENT env's fresh build
        output is never deleted; only stale debris from the aborted clean is
        removed. ``build_root`` overrides the selected board's cache build root
        for projects that build elsewhere (e.g. the bundled soft_reset
        project)."""
        if not reported_paths:
            return
        if build_root is None:
            build_root = self._board_build_root()
        try:
            safe_root = Path(build_root).resolve(strict=False)
        except Exception:
            safe_root = Path(build_root)
        active_env = safe_root / env_name

        for stale_text in reported_paths:
            try:
                stale_path = Path(stale_text).resolve(strict=False)
                stale_path.relative_to(safe_root)
            except Exception:
                self._append(
                    f"  ⚠ Ignored unsafe stale-cache path outside the selected board workspace: {stale_text}",
                    "warning",
                )
                continue
            if not stale_path.exists():
                continue

            if not build_ok:
                # A failed run may be an ordinary compiler/linker or upload
                # error.  Do not destroy valid partial objects before the
                # caller classifies the actual diagnostics.
                self._append(
                    "  ℹ Preserved selected-board incremental state after the failed run; stale cleanup is deferred unless cache corruption is confirmed.",
                    "dim",
                )
                continue

            # A successful run proves that the active environment now contains
            # valid fresh output.  Never delete it or an ancestor that contains
            # it just because PlatformIO printed a delayed cleanup warning.
            if build_ok:
                try:
                    active_env.relative_to(stale_path)
                    contains_active_env = True
                except ValueError:
                    contains_active_env = False
                if stale_path == active_env or contains_active_env:
                    self._append(
                        "  ℹ Preserved the successful selected-board build despite a stale cleanup report.",
                        "dim",
                    )
                    continue

            # A failed checksum cleanup can leave an incompatible directory,
            # but the board-specific root guarantees this repair cannot touch
            # any other board.  Ordinary compiler errors do not reach here
            # unless PlatformIO explicitly identified a stale path.
            if robust_rmtree(stale_path):
                self._append(
                    f"  ✔ Repaired stale selected-board build path: {stale_path}",
                    "success",
                )
            else:
                self._append(
                    f"  ⚠ Selected-board cache path is still locked; it will be retried next run: {stale_path}",
                    "warning",
                )

    def _clean_targets(self) -> list[tuple[Path, str]]:
        """Authoritative targets for manual Clean and its availability check."""
        sketch = self.sketch_dir_path
        remote_root = self._remote_workspace_root(sketch)
        targets = [
            (get_project_build_cache_root(sketch, create=False), "MCU Flasher project build cache"),
            (sketch / ".pio", "all cached board workspaces"),
            (sketch / "src", "generated build sources"),
            (sketch / "platformio.ini", "generated PlatformIO configuration"),
            (sketch / "build_artifacts", "board-specific binary archives"),
            (sketch / ".build_artifacts", "legacy binary archives"),
            (sketch / "compiled_builds", "legacy compiled binaries"),
            (sketch / ".mcu_gui_cache.json", "legacy compile metadata"),
            (get_project_build_cache_root(sketch, create=False) / ".mcu_gui_cache.json", "compile metadata"),
            (get_project_build_cache_root(sketch, create=False) / ".mcu_flash_syntax_errors.json", "syntax metadata"),
            (sketch / ".mcu_flash_syntax_errors.json", "legacy syntax metadata"),
            (sketch / ".mcu_gui_compat_cache.json", "legacy compatible-devices metadata"),
            (sketch / ".mcu_flash_tab_order.json", "legacy editor tab order"),
            (sketch / ".mcu_ai_edits", "legacy AI edit backups"),
            (sketch / "MCU-FLASHER-SRC", "legacy generated source cache"),
            (sketch / ".ai_edit_signal", "generated editor signal"),
            # Reset builds are app-level, exact-board caches.  Manual Clean is
            # intentionally the one operation that clears them all.
            (SCRIPT_DIR / "soft_reset_project" / "boards", "Soft/Hard Reset board caches"),
            (SCRIPT_DIR / "soft_reset_project_uno" / "boards", "Arduino reset board caches"),
            (SCRIPT_DIR / "soft_reset_project" / ".pio", "legacy ESP reset cache"),
            (SCRIPT_DIR / "soft_reset_project_uno" / ".pio", "legacy Arduino reset cache"),
            (SCRIPT_DIR / ".pio_cache", "legacy app-wide SCons cache"),
            (Path.home() / ".mcu_flash_gui" / "ai-reviews", "external AI review transcripts"),
            (Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / ".mcuflasher-app" / ".tmp", "external temporary app cache"),
        ]
        if remote_root is not None:
            targets.append((remote_root, "remote project local build workspace"))
        return targets

    def _perform_clean(self) -> tuple[list[str], list[str]]:
        """Core clean execution: delete all build artifacts, temporary directories, and generated configs, leaving only sketch source files."""
        removed: list[str] = []
        errors:  list[str] = []

        seen: set[str] = set()
        for target, label in self._clean_targets():
            target_key = os.path.normcase(os.path.abspath(str(target)))
            if target_key in seen:
                continue
            seen.add(target_key)
            if not target.exists():
                continue
            try:
                if robust_rmtree(target):
                    removed.append(label)
                else:
                    errors.append(f"{label}: path remained locked")
            except Exception as exc:
                errors.append(f"{label}: {exc}")

        # Reset every compile-state field so the next build always prepares a
        # fresh PlatformIO environment.  Keep the legacy single-slot hash in
        # sync with the per-board cache; leaving it populated makes a cleaned
        # project appear compiled to older cache readers and chained actions.
        self._compile_cache_hash = None
        self._last_compiled_board = None
        self._compile_cache_by_board = {}
        self._build_config_hash_by_board = {}
        self._build_metadata_by_board = {}
        self._just_created_envs = set()
        try:
            self._set_symbol_cache_compiled_state(False)
        except Exception:
            pass
        return removed, errors

    def _perform_clean_current_board(self) -> tuple[list[str], list[str]]:
        """Repair only the selected board workspace, leaving all siblings and
        shared toolchain/framework packages untouched.

        This is reserved for an explicitly classified cache-integrity failure;
        normal compiler/linker errors retain their partial incremental state.
        """
        sketch = self.sketch_dir_path
        removed: list[str] = []
        errors: list[str] = []

        # One exact workspace contains this board's build, libdeps and
        # checksum state.  Other board workspaces are siblings and untouched.
        targets = [self._board_workspace(sketch)]
        for target in targets:
            if not target.exists():
                continue
            try:
                if robust_rmtree(target):
                    removed.append(f"boards/{target.name}")
                else:
                    errors.append(f"boards/{target.name}: path remained locked")
            except Exception as exc:
                errors.append(f"{target.parent.name}/{target.name}: {exc}")

        # This board no longer has a valid cached hash once its build is wiped.
        board_name = self.board_var.get()
        if board_name:
            cache_key = self._board_cache_key(board_name)
            self._compile_cache_by_board.pop(cache_key, None)
            self._compile_cache_by_board.pop(board_name, None)  # legacy cache key
            self._build_config_hash_by_board.pop(cache_key, None)
            self._build_config_hash_by_board.pop(board_name, None)
            self._build_metadata_by_board.pop(cache_key, None)
            self._build_metadata_by_board.pop(board_name, None)
        return removed, errors

    # Targets the *running app itself* recreates within moments (temp files,
    # AI review transcripts). They are still swept by a real Clean, but they
    # must not count towards availability — otherwise Clean re-enables right
    # after finishing and the user can "clean" an already-clean project
    # forever.
    _CLEAN_VOLATILE_LABELS = frozenset({
        "external AI review transcripts",
        "external temporary app cache",
    })

    def _has_cleanable_targets(self) -> bool:
        """Return True if any build artifacts or generated files exist that Clean would remove.

        Volatile app-owned paths (see _CLEAN_VOLATILE_LABELS) are excluded so
        the button reflects real project artifacts, not transient temp churn."""
        return any(
            target.exists()
            for target, label in self._clean_targets()
            if label not in self._CLEAN_VOLATILE_LABELS
        )

    def _show_toast(self, message: str, duration_ms: int = 2500):
        """Show a floating borderless toast notification centered over the main window that auto-dismisses."""
        try:
            toast = tk.Toplevel(self.root)
            toast.overrideredirect(True)
            toast.configure(bg=Theme.BG_DARK)
            toast.attributes("-topmost", True)

            # Semi-transparent on Windows
            try:
                toast.attributes("-alpha", 0.92)
            except Exception:
                pass

            # Build content
            outer = tk.Frame(toast, bg=Theme.CYAN, padx=1, pady=1)
            outer.pack(fill=tk.BOTH, expand=True)
            inner = tk.Frame(outer, bg=Theme.BG_DARK, padx=16, pady=10)
            inner.pack(fill=tk.BOTH, expand=True)

            tk.Label(
                inner, text=message,
                font=self.font_label, fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARK,
                justify=tk.CENTER, wraplength=350,
            ).pack()

            # Position: center over the main window
            toast.update_idletasks()
            tw = toast.winfo_reqwidth()
            th = toast.winfo_reqheight()
            rx = self.root.winfo_rootx()
            ry = self.root.winfo_rooty()
            rw = self.root.winfo_width()
            rh = self.root.winfo_height()
            x = rx + (rw - tw) // 2
            y = ry + (rh - th) // 2
            toast.geometry(f"+{max(0, x)}+{max(0, y)}")

            # Auto-dismiss
            def _fade_out(alpha=0.92):
                try:
                    if alpha <= 0.1:
                        toast.destroy()
                        return
                    toast.attributes("-alpha", alpha)
                    toast.after(40, lambda: _fade_out(alpha - 0.08))
                except Exception:
                    pass

            toast.after(duration_ms, _fade_out)
            # Allow click-to-dismiss
            toast.bind("<Button-1>", lambda e: toast.destroy())
        except Exception:
            pass

    def _update_clean_button_state(self):
        """Enable or disable the Clean button based on whether there are cleanable targets."""
        try:
            is_compact = getattr(self, "_action_compact_mode", False)
            if self._has_cleanable_targets():
                self.btn_clean.configure(state=tk.NORMAL, text="Clean" if is_compact else "🧹 Clean")
            else:
                self.btn_clean.configure(state=tk.DISABLED, text="Clean" if is_compact else "🧹 Clean")
        except Exception:
            pass

    def _do_clean(self, on_complete=None, confirmed: bool = False):
        """Delete all generated/cached files, keeping only source files.

        Removes:
          • .mcu_flasher_build_cache/ — staged sources, board builds, metadata, and generated configuration
          • reset caches   — exact-board Hard/Soft Reset builds

        Keeps all source/user files and the shared framework/toolchain packages.
        Safe to call at any time (not during a busy operation).
        """
        if self.is_busy:
            self._set_status("Busy — stop the current operation first", Theme.RED)
            return

        # askyesno() runs a nested event loop: while the dialog is open,
        # self.is_busy is still False and buttons are still enabled, so a
        # second click would queue a second concurrent Clean. Guard the whole
        # confirm window with an explicit latch.
        if getattr(self, "_clean_confirm_pending", False):
            return

        # Fast-path: nothing to clean — show toast and disable button
        if not self._has_cleanable_targets():
            self._show_toast("🧹  Project is already clean\nNothing to remove.")
            self._update_clean_button_state()
            return

        self._clean_confirm_pending = True
        try:
            if not confirmed:
                proceed = messagebox.askyesno(
                    "Clear All Board Build Caches?",
                    "Clean will remove generated project configuration and ALL cached "
                    "builds for every board used with this sketch.\n\n"
                    "The app-wide Hard/Soft Reset board caches shared by all sketches "
                    "and windows, plus legacy compiled artifacts, will also be cleared. "
                    "The next Compile, Upload, Hard "
                    "Reset, or Soft Reset for those boards may need a first-time rebuild.\n\n"
                    "Your .ino/.cpp/.c/.h source files, other user files, and shared "
                    "PlatformIO frameworks/toolchains will NOT be removed.\n\n"
                    "Continue with Clean?",
                    icon="warning",
                    parent=self.root,
                )
                if not proceed:
                    self._set_status("Clean cancelled — caches preserved", Theme.YELLOW)
                    return
        finally:
            self._clean_confirm_pending = False

        self._clean_retry_in_progress = bool(on_complete)
        self.is_busy = True
        self._stop_requested = False
        self._op_session_id += 1
        self._set_buttons_state(True, operation="clean")
        self._set_status("Cleaning build cache...", Theme.GREEN)
        self._append("")
        self._append("  🧹 CLEANING PROJECT...", "header")

        def _bg_clean():
            reset_cache_lock = _try_acquire_reset_cache_lock()
            if reset_cache_lock is None:
                def _locked():
                    self._append(
                        "  ⚠ Clean cancelled: another window is using the Hard/Soft Reset cache.",
                        "warning",
                    )
                    self._set_status("Clean blocked — reset cache is in use", Theme.YELLOW)
                    self.is_busy = False
                    self._set_buttons_state(False)
                self.root.after(0, _locked)
                return
            try:
                removed, errors = self._perform_clean()

                def _done():
                    if removed:
                        self._append(f"  ✔ Removed: {', '.join(removed)}", "success")
                    else:
                        self._append("  Nothing to remove — project already clean.", "info")
                    if errors:
                        for e in errors:
                            self._append(f"  ⚠ Could not remove {e}", "warning")
                    self._append("  Ready. Compile or Upload to rebuild from scratch.", "dim")
                    self._set_status("Project cleaned — ready to rebuild", Theme.GREEN)
                    
                    # Uncheck and disable "Skip recompile"
                    self.skip_compile_var.set(False)
                    try:
                        self.cb_skip_compile.configure(state=tk.DISABLED)
                    except Exception:
                        pass
                    
                    # Reflect the post-clean target state immediately, in
                    # both the chained and standalone paths — after a full
                    # clean there is normally nothing left, so the button
                    # must go DISABLED instead of waiting for the next op.
                    self._update_clean_button_state()

                    # Re-sync hardware state and recreate AGENTS.md immediately so AI Assistant has fresh cache
                    try:
                        if hasattr(self, "sketch_dir_path") and self.sketch_dir_path:
                            ensure_hidden_read_first_md(self.sketch_dir_path)
                        if hasattr(self, "_sync_project_hardware_state"):
                            self._sync_project_hardware_state()
                    except Exception:
                        pass

                    # Return to the normal idle state before invoking a
                    # chained action.  In particular, _do_compile() must not
                    # inherit the clean operation marker or stale disabled
                    # button state.  Queue the callback after the idle
                    # transition so its scheduled UI update runs first.
                    self.is_busy = False
                    self._set_buttons_state(False)
                    if on_complete:
                        self.root.after(0, on_complete)

                self.root.after(0, _done)
            except Exception as exc:
                def _error(exc=exc):
                    self._append(f"  ✖ Internal error during clean: {exc}", "error")
                    self._set_status("Clean FAILED", Theme.RED)
                    self.is_busy = False
                    self._set_buttons_state(False)
                self.root.after(0, _error)
            finally:
                _release_reset_cache_lock(reset_cache_lock)

        threading.Thread(target=_bg_clean, daemon=True).start()

    def _do_clean_then_compile(self, confirmed: bool = False):
        """Clean the build cache and immediately start a fresh compile."""
        self._do_clean(on_complete=self._do_compile, confirmed=confirmed)

    def _block_action_for_pending_ai_review(self, action_name):
        editor_api = getattr(self, "editor_api", None)
        if not editor_api:
            return False
        journal_error = (
            editor_api.get_ai_review_journal_error()
            if hasattr(editor_api, "get_ai_review_journal_error") else ""
        )
        if journal_error:
            self._append_notif(
                f"  {action_name} paused: the AI review journal needs recovery.",
                "error",
                category="system",
                title="AI review journal error",
            )
            return True
        controller = getattr(self, "ai_controller", None)
        if controller and hasattr(controller, "collect_unreported_edits"):
            try:
                for edit in controller.collect_unreported_edits():
                    path, before, after, before_exists, after_exists = edit
                    queue_result = editor_api.queue_ai_edit_snapshot(
                        path, before, after, before_exists, after_exists
                    )
                    if queue_result == "cancelled" and getattr(self, "editor_window", None):
                        self.editor_window.evaluate_js(
                            "onAiReviewCancelled("
                            f"{json.dumps(str(path))}, {json.dumps(bool(after_exists))})"
                        )
                    if queue_result and queue_result != "cancelled" and getattr(self, "editor_window", None):
                        self.editor_window.evaluate_js(
                            f"reloadActiveFileWithDiff({json.dumps(str(path))})"
                        )
            except Exception as exc:
                self._append_notif(
                    f"  {action_name} paused: AI edit verification failed ({exc}).",
                    "warning",
                    category="system",
                    title="AI approval check failed",
                )
                return True
        if not editor_api.has_any_pending_ai_edits():
            return False
        reviews = editor_api.get_ai_edit_reviews()
        count = len(reviews)
        noun = "edit" if count == 1 else "edits"
        self._append_notif(
            f"  {action_name} paused: review {count} pending AI {noun} first.",
            "warning",
            category="system",
            title="AI approval required",
        )
        if reviews and getattr(self, "editor_window", None):
            try:
                review_path = reviews[0].get("path", "")
                self.editor_window.evaluate_js(
                    f"reloadActiveFileWithDiff({json.dumps(review_path)})"
                )
            except Exception:
                pass
        return True

