#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import time
import threading
import hashlib
from typing import TYPE_CHECKING
from pathlib import Path
import tkinter as tk


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

class SyntaxCheckerMixin(_Base):
    """Mixin providing SyntaxCheckerMixin capabilities for MCUUploadGUI."""
    def _start_periodic_syntax_check(self):
        """Runs periodically to check for errors and update the UI in real-time."""
        # Main-thread periodic check relaxed: background thread handles project syntax
        self.root.after(10000, self._start_periodic_syntax_check)

    def _syntax_check_tab_index(self) -> int:
        """Resolve the Syntax Check tab index in the bottom notebook."""
        cached = getattr(self, "_syntax_check_tab_index_cache", None)
        if cached is not None:
            return cached
        try:
            frame = getattr(self, "_syntax_check_frame", None)
            if frame is not None and self.bottom_notebook.winfo_exists():
                idx = self.bottom_notebook.index(frame)
                self._syntax_check_tab_index_cache = idx
                return idx
        except Exception:
            pass
        try:
            for i, tab_id in enumerate(self.bottom_notebook.tabs()):
                if "Syntax Check" in self.bottom_notebook.tab(tab_id, "text"):
                    self._syntax_check_tab_index_cache = i
                    return i
        except Exception:
            pass
        return 4

    def _syntax_check_is_selected(self) -> bool:
        """Return whether the Syntax Check tab is currently selected in the bottom notebook."""
        try:
            if not getattr(self, "monitors_pane_visible", True):
                return False
            current = self.bottom_notebook.select()
            return bool(current) and self.bottom_notebook.index(current) == self._syntax_check_tab_index()
        except Exception:
            return False

    def _start_background_syntax_thread(self):
        """Launch a permanent background thread that periodically checks syntax
        for all open editor files, regardless of editor mode.
        Stops itself if the window is destroyed or the thread is cancelled."""
        if getattr(self, "_syntax_bg_active", False):
            return  # already running
        self._syntax_bg_active = True

        def _syntax_bg_loop():
            while getattr(self, "_syntax_bg_active", False):
                try:
                    time.sleep(3.0)
                    if not getattr(self, "_syntax_bg_active", False):
                        break
                except Exception:
                    break

                # Skip if busy compiling or no project loaded
                if (getattr(self, "is_busy", False)
                        or getattr(self, "_compile_background_lock", threading.Lock()).is_set()
                        or not getattr(self, "sketch_dir_path", None)):
                    continue

                # Check editor is loaded
                if getattr(self, "editor_mode", "default") == "monaco":
                    if not getattr(self, "_editor_content_loaded", False):
                        continue

                # Run background check directly on worker thread
                self._run_bg_syntax_check()

        bg_thread = threading.Thread(target=_syntax_bg_loop, daemon=True, name="SyntaxBgLoop")
        bg_thread.start()
        self._syntax_bg_thread = bg_thread

        # Also keep the periodic check for inline highlighting in default mode
        self.root.after(4000, self._start_periodic_syntax_check)

    def _run_bg_syntax_check(self):
        """Lightweight multi-threaded background syntax check — analyzes project files
        in parallel across all CPU cores, utilizing memory caching freely."""
        if (getattr(self, "is_busy", False)
                or getattr(self, "_compile_background_lock", threading.Lock()).is_set()
                or not getattr(self, "sketch_dir_path", None)):
            return

        def _worker():
            try:
                sketch_dir = getattr(self, "sketch_dir_path", None)
                if not sketch_dir or not sketch_dir.exists():
                    return

                # Check if any files were modified since last scan
                current_mtimes = {}
                project_files = []
                for ext in ["*.ino", "*.cpp", "*.h", "*.hpp"]:
                    for f in sketch_dir.glob(ext):
                        try:
                            st = f.stat()
                            current_mtimes[str(f)] = (st.st_mtime_ns, st.st_size)
                            project_files.append(f)
                        except Exception:
                            pass

                last_mtimes = getattr(self, "_last_syntax_mtimes", None)
                if last_mtimes is not None and last_mtimes == current_mtimes:
                    return
                self._last_syntax_mtimes = current_mtimes

                from src.syntax_checker import analyze_files_parallel, extract_project_functions
                defined_funcs = extract_project_functions(sketch_dir)
                all_errors = analyze_files_parallel(project_files, defined_funcs)
                all_errors.sort(key=lambda x: (x.get("file", ""), x.get("line", 0)))
                self._post_ui(lambda all_errors=all_errors: self._update_syntax_check_ui(all_errors))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="BgSyntaxCheck").start()


    def _get_project_defined_functions(self) -> set[str]:
        if not hasattr(self, "_cached_project_funcs") or self._cached_project_funcs is None:
            try:
                from src.syntax_checker import extract_project_functions
                self._cached_project_funcs = extract_project_functions(self.sketch_dir_path)
            except Exception:
                self._cached_project_funcs = set()
        return self._cached_project_funcs

    def _run_realtime_syntax_check(self, text_widget: tk.Text, file_path: Path):
        if self._compile_background_lock.is_set():
            return
        code = text_widget.get("1.0", tk.END)
        text_widget.tag_remove("syntax_error", "1.0", tk.END)
        text_widget.tag_remove("syntax_warning", "1.0", tk.END)
        
        defined_funcs = self._get_project_defined_functions()
        try:
            from src.syntax_checker import analyze_cpp_syntax
            errors = analyze_cpp_syntax(code, file_path, defined_funcs)
        except Exception:
            return
            
        for err in errors:
            line = err["line"]
            col = err["col"]
            tag = "syntax_error" if err["severity"] == "error" else "syntax_warning"
            start_pos = f"{line}.{max(0, col - 1)}"
            end_pos = f"{line}.end"
            text_widget.tag_add(tag, start_pos, end_pos)

    def _is_project_unsaved(self) -> bool:
        mode = getattr(self, "editor_mode", "default")
        if mode == "monaco":
            if hasattr(self, "editor_api") and hasattr(self.editor_api, "modified_files"):
                if any(is_modified for is_modified in self.editor_api.modified_files.values()):
                    return True
        else:
            tab_data = getattr(self, "editor_tab_data", None)
            if tab_data:
                if any(d.get("modified") for d in tab_data.values()):
                    return True
        return False

    def _run_manual_syntax_check(self):
        """UI-thread entry point for the syntax check.

        The old implementation parsed every open buffer AND globbed/re-read
        the whole project inline on the Tk thread — plus a 150 ms
        ``root.update()`` busy-wait that re-entered pending timers. All of
        that now happens in three cheap phases:

          1. UI thread : trigger save-all, snapshot open buffers (fast),
             compute a change signature and bail out if nothing changed.
          2. Worker    : extract project functions + run analyze_cpp_syntax
             over snapshots and on-disk files (CPU/IO heavy).
          3. UI thread : apply inline error tags + refresh the result tree.
        """
        if getattr(self, "_syntax_analysis_inflight", False):
            # A pass is already running; coalesce instead of stacking work.
            self._syntax_analysis_rerun = True
            return

        # Trigger save all if unsaved changes exist. No busy-wait here: saves
        # are small local writes, and phase 1 reads live widget buffers, so
        # there is nothing to wait for.
        if self._is_project_unsaved():
            if hasattr(self, "_save_all_editor_files") and callable(self._save_all_editor_files):
                try:
                    self._save_all_editor_files()
                except Exception:
                    pass

        self._cached_project_funcs = None

        tab_snapshots = []   # [(Path, code)]
        checked_files = set()
        if hasattr(self, "editor_tab_data") and self.editor_tab_data:
            for frame, data in self.editor_tab_data.items():
                try:
                    if not frame.winfo_exists():
                        continue
                except Exception:
                    continue
                file_path = data["path"]
                if file_path.suffix in (".ino", ".cpp", ".h"):
                    try:
                        tab_snapshots.append((file_path, data["text"].get("1.0", tk.END)))
                        checked_files.add(file_path.resolve())
                    except Exception:
                        pass

        sketch_dir = getattr(self, "sketch_dir_path", None)

        # Change signature: buffer contents + project file metadata. When
        # nothing changed since the last completed pass (the common idle case
        # for the 3 s periodic timer) skip the worker entirely.
        signature = None
        try:
            disk_state = []
            if sketch_dir and sketch_dir.exists():
                for ext in ("*.ino", "*.cpp", "*.h"):
                    for fp in sketch_dir.glob(ext):
                        try:
                            st = fp.stat()
                            disk_state.append((str(fp), st.st_mtime_ns, st.st_size))
                        except OSError:
                            pass
            digest = hashlib.sha256()
            for fpath, code in tab_snapshots:
                digest.update(str(fpath).encode("utf-8", "replace"))
                digest.update(code.encode("utf-8", "replace"))
            signature = (
                digest.hexdigest(),
                tuple(sorted(disk_state)),
            )
            if signature == getattr(self, "_last_syntax_signature", None):
                return
        except Exception:
            signature = None

        self._syntax_analysis_inflight = True
        self._last_syntax_signature = signature

        def _worker():
            all_errors = []
            per_file_errors = {}
            defined_funcs = set()
            try:
                from src.syntax_checker import analyze_cpp_syntax, extract_project_functions
            except Exception:
                # Mirror the historical behaviour: no checker available,
                # leave existing UI state untouched.
                self._post_ui(lambda: self._finish_manual_syntax_check({}, [], quiet=True))
                return
            try:
                if sketch_dir and sketch_dir.exists():
                    try:
                        defined_funcs = extract_project_functions(sketch_dir)
                    except Exception:
                        defined_funcs = set()
                for fpath, code in tab_snapshots:
                    try:
                        errs = analyze_cpp_syntax(code, fpath, defined_funcs)
                    except Exception:
                        errs = []
                    per_file_errors[str(fpath)] = errs
                    all_errors.extend(errs)
                if sketch_dir and sketch_dir.exists():
                    disk_files = []
                    for ext in ["*.ino", "*.cpp", "*.h", "*.hpp"]:
                        for file_path in sketch_dir.glob(ext):
                            try:
                                if file_path.resolve() not in checked_files:
                                    disk_files.append(file_path)
                            except Exception:
                                pass
                    if disk_files:
                        from src.syntax_checker import analyze_files_parallel
                        all_errors.extend(analyze_files_parallel(disk_files, defined_funcs))
            finally:
                self._post_ui(lambda: self._finish_manual_syntax_check(per_file_errors, all_errors))

        threading.Thread(target=_worker, daemon=True, name="ManualSyntaxCheck").start()

    def _finish_manual_syntax_check(self, per_file_errors, all_errors, quiet=False):
        """UI-thread completion: paint inline tags and refresh the tree."""
        self._syntax_analysis_inflight = False
        rerun = getattr(self, "_syntax_analysis_rerun", False)
        self._syntax_analysis_rerun = False

        if quiet:
            return

        if hasattr(self, "editor_tab_data") and self.editor_tab_data:
            for frame, data in list(self.editor_tab_data.items()):
                try:
                    if not frame.winfo_exists():
                        continue
                except Exception:
                    continue
                errs = per_file_errors.get(str(data["path"]))
                if errs is None:
                    continue
                try:
                    txt = data["text"]
                    txt.tag_remove("syntax_error", "1.0", tk.END)
                    txt.tag_remove("syntax_warning", "1.0", tk.END)
                    for err in errs:
                        line = err["line"]
                        col = err["col"]
                        tag = "syntax_error" if err["severity"] == "error" else "syntax_warning"
                        txt.tag_add(tag, f"{line}.{max(0, col - 1)}", f"{line}.end")
                except Exception:
                    pass

        self._update_syntax_check_ui(all_errors)

        if rerun:
            self.root.after(50, self._run_manual_syntax_check)

    def _update_syntax_check_ui(self, errors):
        """Atomic UI update for syntax results on Tk thread."""
        if threading.get_ident() != getattr(self, "_tk_thread_id", None):
            self._post_ui(lambda e=list(errors or ()): self._update_syntax_check_ui(e))
            return

        if not hasattr(self, "syntax_tree") or not self.syntax_tree:
            return
        try:
            if not self.syntax_tree.winfo_exists():
                return
        except Exception:
            return

        try:
            children = self.syntax_tree.get_children()
            if children:
                self.syntax_tree.delete(*children)
        except Exception:
            pass

        result_errors = list(errors or ())
        err_count = sum(1 for e in result_errors if e.get("severity") == "error")
        warn_count = sum(1 for e in result_errors if e.get("severity") == "warning")

        lbl = getattr(self, "lbl_syntax_status", None)
        if lbl is not None:
            try:
                if not result_errors:
                    lbl.configure(text="🔍 SYNTAX CHECK: ✔ Clean (no issues)", fg=Theme.GREEN)
                else:
                    status_text = f"🔍 SYNTAX CHECK: {err_count} Error(s), {warn_count} Warning(s)"
                    if len(result_errors) > 200:
                        status_text += " — showing first 200"
                    fg_color = Theme.RED if err_count > 0 else Theme.ORANGE
                    lbl.configure(text=status_text, fg=fg_color)
            except Exception:
                pass

        for err in result_errors[:200]:
            tag = "error" if err.get("severity") == "error" else "warning"
            try:
                self.syntax_tree.insert(
                    "", tk.END,
                    values=(err.get("file", ""), err.get("line", ""), str(err.get("severity", "")).upper(), err.get("message", "")),
                    tags=(tag,)
                )
            except Exception:
                pass

    def _on_syntax_tree_double_click(self, event):
        if not hasattr(self, "syntax_tree") or not self.syntax_tree:
            return
        item = self.syntax_tree.selection()
        if not item:
            return
        values = self.syntax_tree.item(item, "values")
        if not values:
            return
            
        file_name, line_str, severity, desc = values
        try:
            line_no = int(line_str)
        except ValueError:
            return
            
        if hasattr(self, "editor_tab_data") and self.editor_tab_data:
            for frame, data in self.editor_tab_data.items():
                if data["path"].name == file_name:
                    if hasattr(self, "editor_notebook") and self.editor_notebook:
                        self.editor_notebook.select(frame)
                        txt = data["text"]
                        txt.focus_set()
                        txt.mark_set(tk.INSERT, f"{line_no}.0")
                        txt.see(f"{line_no}.0")
                        
                        txt.tag_remove("active_line", "1.0", tk.END)
                        txt.tag_add("active_line", f"{line_no}.0", f"{line_no}.end")
                        break

    def _trigger_save(self):
        if hasattr(self, "_save_current_editor_file") and callable(self._save_current_editor_file):
            self._save_current_editor_file()
        self.root.after(200, self._run_manual_syntax_check)

    def _trigger_save_all(self):
        if hasattr(self, "_save_all_editor_files") and callable(self._save_all_editor_files):
            self._save_all_editor_files()
        self.root.after(200, self._run_manual_syntax_check)

    def _schedule_syntax_ui_update(self):
        if hasattr(self, "_syntax_ui_after_id") and self._syntax_ui_after_id:
            try:
                self.root.after_cancel(self._syntax_ui_after_id)
            except Exception:
                pass
        self._syntax_ui_after_id = self.root.after(500, self._run_manual_syntax_check)

    def _backup_default_editor_state(self):
        saved_state = []
        if hasattr(self, "editor_tab_data") and self.editor_tab_data:
            for frame, data in list(self.editor_tab_data.items()):
                try:
                    if frame.winfo_exists() and data["text"].winfo_exists():
                        saved_state.append({
                            "path": data["path"],
                            "content": data["text"].get("1.0", tk.END),
                            "original": data["original"],
                            "modified": data["modified"],
                            "cursor": data["text"].index(tk.INSERT),
                            "scroll": data["text"].yview()
                        })
                except Exception:
                    pass
        self._default_editor_state_backup = saved_state

