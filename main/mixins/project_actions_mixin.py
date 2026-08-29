#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import os
import subprocess
import threading
from typing import TYPE_CHECKING
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont


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

class ProjectActionsMixin(_Base):
    """Mixin providing ProjectActionsMixin capabilities for MCUUploadGUI."""
    def _open_sketch_in_explorer(self):
        """Open the current project folder in Windows File Explorer."""
        try:
            if self.sketch_dir_path and self.sketch_dir_path.is_dir():
                import os
                os.startfile(str(self.sketch_dir_path))
            else:
                from tkinter import messagebox
                messagebox.showwarning(
                    "Folder Not Found",
                    f"Project folder not found or missing:\n{self.sketch_dir_path}",
                    parent=self.root,
                )
        except Exception as e:
            from tkinter import messagebox
            messagebox.showerror(
                "Cannot Open Folder",
                f"Failed to open folder in Explorer:\n{e}",
                parent=self.root,
            )

    def _select_sketch_folder(self):
        """Right-click on project title / icon: directly open the file/sketch picker (showing files inside folders)."""
        if self.is_busy:
            return
        from tkinter import filedialog, messagebox
        init_dir = str(self.sketch_dir_path) if self.sketch_dir_path.exists() else str(Path.home())

        selected = filedialog.askopenfilename(
            initialdir=init_dir,
            title="Select Sketch / Project File (.ino, .cpp, .h, platformio.ini)",
            parent=self.root,
            filetypes=[
                ("Project Files & Sketches (*.ino, *.cpp, *.h)", "*.ino;*.cpp;*.h;*.hpp;platformio.ini;*.txt"),
                ("Arduino Sketches (*.ino)", "*.ino"),
                ("C/C++ Source & Headers (*.cpp, *.h)", "*.cpp;*.h;*.hpp"),
                ("All Files (*.*)", "*.*")
            ]
        )
        if selected:
            p = Path(selected)
            new_path = p.parent if p.is_file() else p
            if new_path.name.lower() == "src" and (new_path.parent / "platformio.ini").exists():
                new_path = new_path.parent
            if new_path.resolve() != self.sketch_dir_path.resolve():
                owner_pid = folder_lock_owner(new_path)
                if owner_pid is not None:
                    messagebox.showerror(
                        "Project In Use",
                        f"This project folder is already open in another MCU Flasher window "
                        f"(PID {owner_pid}):\n{new_path}\n\n"
                        "Close that window first, or choose a different project.",
                        parent=self.root
                    )
                    return

                if not _validate_and_scaffold_ino(self.root, new_path):
                    return

                self.sketch_dir_path = new_path
                config = load_gui_config()
                config["last_sketch_dir"] = str(self.sketch_dir_path)
                save_gui_config(config)
                self._on_folder_changed()
            else:
                messagebox.showinfo(
                    "Project Active",
                    "✔ This project is already currently active.",
                    parent=self.root
                )

    def _new_project(self):
        """Open the project selector dialog mid-session.
        On success, switches the active sketch folder to the chosen or
        freshly scaffolded project — same effect as startup project selection."""
        if self.is_busy:
            return
        dlg = ProjectSelectorDialog(self.root, str(self.sketch_dir_path))
        project_dir = dlg.run()
        if project_dir:
            if project_dir.resolve() != self.sketch_dir_path.resolve():
                # When another task is already live, let the user decide
                # whether this project replaces the current task or deserves
                # an explicitly independent window.  The latter gets its own
                # PID-scoped config/editor state and cannot be created by an
                # accidental VBS relaunch.
                data = _load_raw_config()
                alive = _get_alive_pid_create_times()
                other_ids = [pid for pid, inst in data.get("instances", {}).items()
                             if pid != _INSTANCE_ID and _instance_is_alive(pid, inst, alive)]
                if self.root.winfo_exists():
                    from tkinter import messagebox
                    run_here = messagebox.askyesno(
                        "Open Project",
                        f"Current task ID: {_INSTANCE_ID}"
                        + (f"\nOther running task(s): {', '.join(other_ids)}" if other_ids else "")
                        + "\n\n"
                        f"Do you want to open '{project_dir.name}' on another window?\n\n"
                        "Yes = open an independent window\nNo = use this window",
                        parent=self.root,
                    )
                    if run_here:
                        try:
                            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                            proc = subprocess.Popen(
                                [sys.executable, str(Path(__file__).resolve()), "--from-bootstrap",
                                 "--new-window", "--project", str(project_dir)],
                                cwd=str(SCRIPT_DIR), creationflags=flags,
                            )
                            messagebox.showinfo(
                                "Independent Task Started",
                                f"Project: {project_dir.name}\nTask ID: {proc.pid}\n\n"
                                "This window remains on its current project.",
                                parent=self.root,
                            )
                        except Exception as exc:
                            messagebox.showerror("Cannot Open New Window", str(exc), parent=self.root)
                        return
                self.sketch_dir_path = project_dir
                config = load_gui_config()
                config["last_sketch_dir"] = str(self.sketch_dir_path)
                save_gui_config(config)
                self._on_folder_changed()
            else:
                from tkinter import messagebox
                messagebox.showinfo(
                    "Project Active",
                    "✔ This project is already currently active.",
                    parent=self.root
                )

        self.is_busy = False
        self._set_buttons_busy(False)

    def _open_modify_files_dialog(self):
        """Open a tabbed modal for managing project source files:
          • Add    — create a new blank file (.ino / .h / .cpp / .txt only)
          • Rename — rename an existing project file
          • Delete — permanently remove an existing project file

        Renaming/deleting operates on whatever's currently on disk in the
        sketch folder (so it also covers .c files placed there manually).
        Any successful action refreshes the open editor tabs immediately.
        """
        if self.is_busy:
            return
        if not self.sketch_dir_path or not self.sketch_dir_path.exists():
            from tkinter import messagebox
            messagebox.showerror(
                "No Project Folder",
                "No active project folder to modify.",
                parent=self.root
            )
            return

        from tkinter import messagebox

        ALLOWED_NEW_EXTS = (".ino", ".h", ".cpp", ".txt")
        LISTABLE_GLOBS = ("*.ino", "*.h", "*.cpp", "*.c", "*.txt")
        INVALID_CHARS = '\\/:*?"<>|'

        def _list_project_files():
            names = []
            for pattern in LISTABLE_GLOBS:
                names.extend(f.name for f in self.sketch_dir_path.glob(pattern))
            return sorted(names)

        dlg = tk.Toplevel(self.root)
        dlg.title("Modify Project Files")
        dlg.configure(bg=Theme.BG_DARKEST)
        dlg.resizable(False, False)
        
        # Set window icon if available
        try:
            icon_path = SCRIPT_DIR / "src" / "assets" / "mcu_icon.ico"
            if not icon_path.exists():
                icon_path = SCRIPT_DIR / "src" / "mcu_icon.ico"
            if icon_path.exists():
                dlg.iconbitmap(str(icon_path))
        except Exception:
            pass
        center_toplevel(dlg, self.root, 460, 400)
        dlg.transient(self.root)
        dlg.grab_set()

        dlg_title_font = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        dlg_btn_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        dlg_action_font = tkfont.Font(family="Segoe UI", size=11, weight="bold")

        tk.Label(
            dlg, text="Modify Project Files", font=dlg_title_font,
            fg=Theme.CYAN, bg=Theme.BG_DARKEST
        ).pack(pady=(14, 10))

        # ── Sub-tab bar (Add / Rename / Delete) ─────────────────────────
        tab_bar = tk.Frame(dlg, bg=Theme.BG_DARKEST)
        tab_bar.pack(fill=tk.X, padx=20)

        body = tk.Frame(dlg, bg=Theme.BG_DARKEST)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(12, 0))

        err_lbl = tk.Label(
            dlg, text="", font=self.font_label, fg=Theme.RED, bg=Theme.BG_DARKEST,
            wraplength=400, justify=tk.CENTER
        )
        err_lbl.pack(pady=(8, 0))

        current_tab = ["add"]
        frames = {}
        tab_btns = {}

        def _clear_err():
            err_lbl.config(text="")

        # ── Add sub-panel ────────────────────────────────────────────────
        add_frame = tk.Frame(body, bg=Theme.BG_DARKEST)
        frames["add"] = add_frame

        tk.Label(
            add_frame,
            text="Enter a filename with one of these extensions:\n.ino   .h   .cpp   .txt",
            font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST,
            justify=tk.CENTER
        ).pack(pady=(10, 10))

        add_name_var = tk.StringVar(value="")
        add_entry = tk.Entry(
            add_frame, textvariable=add_name_var, font=self.font_mono,
            bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
            insertbackground=Theme.CYAN, borderwidth=0,
            highlightthickness=1, highlightcolor=Theme.CYAN_DIM,
            highlightbackground=Theme.BORDER, justify=tk.CENTER,
        )
        add_entry.pack(fill=tk.X, ipady=5)

        # ── Browse separator ──────────────────────────────────────────────
        sep_row = tk.Frame(add_frame, bg=Theme.BG_DARKEST)
        sep_row.pack(fill=tk.X, pady=(8, 0))
        tk.Frame(sep_row, bg=Theme.BORDER, height=1).pack(fill=tk.X)
        tk.Label(
            add_frame,
            text="— or —",
            font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST,
        ).pack(pady=(4, 0))

        tk.Button(
            add_frame,
            text="📂  Browse & Copy Existing File…",
            font=self.font_label,
            bg=Theme.BTN_CLEAR, fg=Theme.TEXT_BRIGHT,
            activebackground=Theme.BTN_CLEAR_H,
            activeforeground=Theme.TEXT_BRIGHT,
            relief=tk.FLAT, cursor="hand2", bd=0,
            command=lambda: _do_add_browse(),
        ).pack(pady=(4, 0), ipadx=6, ipady=4)

        def _do_add_browse():
            from tkinter import filedialog
            import shutil as _shutil
            src = filedialog.askopenfilename(
                title="Select a file to add to the project",
                filetypes=[("Sketch/Header/Text files", "*.ino *.cpp *.h *.txt"),
                           ("All files", "*.*")],
                parent=dlg,
            )
            if not src:
                return
            src_path = Path(src)
            if src_path.suffix.lower() not in ALLOWED_NEW_EXTS:
                err_lbl.config(text="Only .ino, .h, .cpp, .txt are allowed.")
                return
            target_name = src_path.name
            if src_path.stem.lower() == self.sketch_dir_path.name.lower():
                target_name = f"{self.sketch_dir_path.name}{src_path.suffix}"
            dest = self.sketch_dir_path / target_name
            if dest.resolve() == src_path.resolve():
                err_lbl.config(text="That file is already in the project folder.")
                return
            if dest.exists():
                overwrite = messagebox.askyesno(
                    "File Already Exists",
                    f"\"{target_name}\" already exists in the project folder.\n\n"
                    "Overwrite it with the selected file?",
                    parent=dlg,
                )
                if not overwrite:
                    return
            try:
                _shutil.copy2(str(src_path), str(dest))
            except Exception as e:
                err_lbl.config(text=f"Could not copy file: {e}")
                return
            self._append(f"  ➕ Added file to project (copied): {target_name}", "success")
            _refresh_editor()
            dlg.destroy()

        def _do_add():

            name = add_name_var.get().strip()
            if not name:
                err_lbl.config(text="Please enter a filename.")
                return
            if any(c in name for c in INVALID_CHARS) or name in (".", ".."):
                err_lbl.config(text="Filename contains invalid characters.")
                return

            suffix = Path(name).suffix.lower()
            if suffix not in ALLOWED_NEW_EXTS:
                err_lbl.config(text="Only .ino, .h, .cpp, .txt are allowed.")
                return
            if not Path(name).stem:
                err_lbl.config(text="Please enter a name before the extension.")
                return

            # Match folder casing if adding primary sketch or header
            if Path(name).stem.lower() == self.sketch_dir_path.name.lower():
                name = f"{self.sketch_dir_path.name}{Path(name).suffix}"

            dest = self.sketch_dir_path / name
            if dest.exists():
                overwrite = messagebox.askyesno(
                    "File Already Exists",
                    f"\"{name}\" already exists in the project folder.\n\n"
                    "Overwrite it with a blank file?",
                    parent=dlg
                )
                if not overwrite:
                    return

            try:
                dest.write_text("", encoding="utf-8")
            except Exception as e:
                err_lbl.config(text=f"Could not create file: {e}")
                return

            self._append(f"  ➕ Added file to project: {name}", "success")
            _refresh_editor()
            dlg.destroy()

        # ── Rename sub-panel ─────────────────────────────────────────────
        rename_frame = tk.Frame(body, bg=Theme.BG_DARKEST)
        frames["rename"] = rename_frame

        tk.Label(
            rename_frame, text="Select a file to rename:", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor=tk.W
        ).pack(fill=tk.X, pady=(10, 4))

        rename_select_var = tk.StringVar()
        rename_combo = ttk.Combobox(
            rename_frame, textvariable=rename_select_var, state="readonly",
            font=self.font_label, values=_list_project_files()
        )
        rename_combo.pack(fill=tk.X)

        tk.Label(
            rename_frame, text="New filename:", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor=tk.W
        ).pack(fill=tk.X, pady=(14, 4))

        rename_new_var = tk.StringVar()
        rename_entry = tk.Entry(
            rename_frame, textvariable=rename_new_var, font=self.font_mono,
            bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
            insertbackground=Theme.CYAN, borderwidth=0,
            highlightthickness=1, highlightcolor=Theme.CYAN_DIM,
            highlightbackground=Theme.BORDER, justify=tk.CENTER,
        )
        rename_entry.pack(fill=tk.X, ipady=5)

        def _on_rename_select(event=None):
            # Pre-fill the new-name box with the current name so the user
            # only has to tweak part of it.
            if rename_select_var.get():
                rename_new_var.set(rename_select_var.get())
        rename_combo.bind("<<ComboboxSelected>>", _on_rename_select)

        def _do_rename():
            old_name = rename_select_var.get()
            if not old_name:
                err_lbl.config(text="Please select a file to rename.")
                return
            new_name = rename_new_var.get().strip()
            if not new_name:
                err_lbl.config(text="Please enter a new filename.")
                return
            if any(c in new_name for c in INVALID_CHARS) or new_name in (".", ".."):
                err_lbl.config(text="Filename contains invalid characters.")
                return

            suffix = Path(new_name).suffix.lower()
            if suffix not in ALLOWED_NEW_EXTS:
                err_lbl.config(text="Only .ino, .h, .cpp, .txt are allowed.")
                return
            if not Path(new_name).stem:
                err_lbl.config(text="Please enter a name before the extension.")
                return

            src = self.sketch_dir_path / old_name
            dest = self.sketch_dir_path / new_name

            if not src.exists():
                err_lbl.config(text="Selected file no longer exists.")
                return
            try:
                same_file = src.resolve() == dest.resolve()
            except Exception:
                same_file = old_name == new_name
            if same_file:
                err_lbl.config(text="New name is the same as the current name.")
                return
            if dest.exists():
                err_lbl.config(text=f"\"{new_name}\" already exists.")
                return

            confirm = messagebox.askyesno(
                "Rename File",
                f"Rename \"{old_name}\" to \"{new_name}\"?\n\n"
                "Any unsaved changes open in its editor tab will be lost.",
                parent=dlg
            )
            if not confirm:
                return

            try:
                src.rename(dest)
            except Exception as e:
                err_lbl.config(text=f"Could not rename file: {e}")
                return

            self._append(f"  ✎ Renamed file: {old_name} → {new_name}", "success")
            _refresh_editor()
            dlg.destroy()

        # ── Delete sub-panel ─────────────────────────────────────────────
        delete_frame = tk.Frame(body, bg=Theme.BG_DARKEST)
        frames["delete"] = delete_frame

        tk.Label(
            delete_frame, text="Select a file to remove from the project:",
            font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor=tk.W
        ).pack(fill=tk.X, pady=(10, 4))

        delete_select_var = tk.StringVar()
        delete_combo = ttk.Combobox(
            delete_frame, textvariable=delete_select_var, state="readonly",
            font=self.font_label, values=_list_project_files()
        )
        delete_combo.pack(fill=tk.X)

        tk.Label(
            delete_frame,
            text="This permanently deletes the file from disk\nand closes its tab in the editor.",
            font=self.font_label, fg=Theme.ORANGE, bg=Theme.BG_DARKEST, justify=tk.CENTER
        ).pack(pady=(16, 0))

        def _do_delete():
            name = delete_select_var.get()
            if not name:
                err_lbl.config(text="Please select a file to delete.")
                return
            target = self.sketch_dir_path / name
            if not target.exists():
                err_lbl.config(text="Selected file no longer exists.")
                return

            confirm = messagebox.askyesno(
                "Delete File",
                f"Permanently delete \"{name}\"?\n\nThis cannot be undone.",
                parent=dlg
            )
            if not confirm:
                return

            try:
                target.unlink()
            except Exception as e:
                err_lbl.config(text=f"Could not delete file: {e}")
                return

            self._append(f"  🗑 Removed file from project: {name}", "warning")
            _refresh_editor()
            dlg.destroy()

        def _refresh_editor():
            if callable(getattr(self, "_load_editor_files", None)):
                self._load_editor_files()
            try:
                self._update_skip_compile_state()
            except Exception:
                pass

        ACTIONS = {
            "add":    ("Add",    Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, _do_add),
            "rename": ("Rename", Theme.BTN_UPLOAD,  Theme.BTN_UPLOAD_H,  _do_rename),
            "delete": ("Delete", Theme.BTN_STOP,     Theme.BTN_STOP_H,   _do_delete),
        }

        def _do_cancel():
            dlg.destroy()

        # ── Bottom Cancel / dynamic action button ───────────────────────
        btn_row = tk.Frame(dlg, bg=Theme.BG_DARKEST)
        btn_row.pack(pady=(16, 14))

        btn_cancel = tk.Button(
            btn_row, text="Cancel", command=_do_cancel,
            font=dlg_action_font, fg=Theme.TEXT_BRIGHT, bg=Theme.BTN_STOP,
            activebackground=Theme.BTN_STOP_H, activeforeground=Theme.TEXT_BRIGHT,
            relief=tk.FLAT, borderwidth=0, padx=20, pady=8, width=10, cursor="hand2",
        )
        btn_cancel.pack(side=tk.LEFT, padx=8)
        btn_cancel.bind("<Enter>", lambda e: btn_cancel.configure(bg=Theme.BTN_STOP_H))
        btn_cancel.bind("<Leave>", lambda e: btn_cancel.configure(bg=Theme.BTN_STOP))

        btn_action = tk.Button(
            btn_row, text="Add", command=_do_add,
            font=dlg_action_font, fg=Theme.TEXT_BRIGHT, bg=Theme.BTN_COMPILE,
            activebackground=Theme.BTN_COMPILE_H, activeforeground=Theme.TEXT_BRIGHT,
            relief=tk.FLAT, borderwidth=0, padx=20, pady=8, width=10, cursor="hand2",
        )
        btn_action.pack(side=tk.LEFT, padx=8)
        btn_action._bg_idle = Theme.BTN_COMPILE
        btn_action._bg_hover = Theme.BTN_COMPILE_H
        btn_action.bind("<Enter>", lambda e: btn_action.configure(bg=btn_action._bg_hover))
        btn_action.bind("<Leave>", lambda e: btn_action.configure(bg=btn_action._bg_idle))

        def _switch(which):
            current_tab[0] = which
            for f in frames.values():
                f.pack_forget()
            frames[which].pack(fill=tk.BOTH, expand=True)
            _clear_err()

            for key, btn in tab_btns.items():
                btn.configure(bg=Theme.BTN_FULL if key == which else Theme.BTN_CLEAR)

            text, bg, bg_hover, cmd = ACTIONS[which]
            btn_action.configure(text=text, bg=bg, activebackground=bg_hover, command=cmd)
            btn_action._bg_idle = bg
            btn_action._bg_hover = bg_hover

        tab_btns["add"] = self._make_btn(
            tab_bar, "➕ Add", lambda: _switch("add"), Theme.BTN_FULL, Theme.BTN_FULL_H, font=dlg_btn_font
        )
        tab_btns["add"].pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 3))

        tab_btns["rename"] = self._make_btn(
            tab_bar, "✎ Rename", lambda: _switch("rename"), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=dlg_btn_font
        )
        tab_btns["rename"].pack(side=tk.LEFT, expand=True, fill=tk.X, padx=3)

        tab_btns["delete"] = self._make_btn(
            tab_bar, "🗑 Delete", lambda: _switch("delete"), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=dlg_btn_font
        )
        tab_btns["delete"].pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(3, 0))

        dlg.bind("<Escape>", lambda e: _do_cancel())
        dlg.protocol("WM_DELETE_WINDOW", _do_cancel)
        add_entry.bind("<Return>", lambda e: _do_add())
        rename_entry.bind("<Return>", lambda e: _do_rename())

        _switch("add")
        add_entry.focus_set()

    def _save_active_folder(self, folder_path):
        """Save the currently active sketch folder in the instance config,
        so other windows can see (and avoid) it via get_occupied_folders().
        Mirrors _save_selected_port()."""
        try:
            config = load_gui_config()
            config["active_sketch_dir"] = str(Path(folder_path).resolve()) if folder_path else ""
            save_gui_config(config)
        except Exception:
            pass

    def _on_folder_changed(self):
        """Called whenever sketch_dir_path is set to a new folder.
        Updates the UI label, invalidates the compile cache, then scans
        includes in a background thread so the console gets an instant
        project-summary report."""
        # The native terminal owns its PTY sessions in a child process. Close
        # that child before rebinding the project so the next Terminal-tab
        # activation starts both shells in the new folder.
        try:
            self._dispose_project_terminal()
        except Exception:
            pass
        try:
            self._shell_stop_all()
            with self._shell_state_lock:
                self._shell_sessions.clear()
        except Exception:
            pass
        # If the AI Assistant (OpenCode) is running, restart it so the new
        # sketch directory becomes its session root instead of the previous
        # project's folder. When no AI process is active, only the watcher
        # baselines need refreshing for the new project.
        if getattr(self, "ai_controller", None):
            try:
                if hasattr(self.ai_controller, "relaunch_for_project"):
                    relaunched = self.ai_controller.relaunch_for_project(
                        self.sketch_dir_path
                    )
                    if relaunched and getattr(self, "_ai_side_visible", False):
                        # The old embedded window died with the old process;
                        # show the loading overlay while the fresh AI window
                        # boots into the new project, then re-poll so it gets
                        # embedded again.
                        try:
                            self._show_ai_loading_overlay_4s()
                        except Exception:
                            pass
                        try:
                            self._start_ai_embedding_poll()
                        except Exception:
                            pass
                elif hasattr(self.ai_controller, "reset_monitoring_state"):
                    self.ai_controller.reset_monitoring_state()
            except Exception as exc:
                print(f"[MCU Flasher] Could not restart AI Assistant: {exc}")
        if getattr(self, "editor_api", None):
            try:
                self.editor_api.bind_project(self.sketch_dir_path)
                if getattr(self, "editor_window", None):
                    self.editor_window.evaluate_js(
                        "if (window.resetAiReviewForProject) window.resetAiReviewForProject();"
                    )
            except Exception as exc:
                print(f"[MCU Flasher] Could not bind AI review journal: {exc}")
        self._clear_console()
        self._clear_serial_console()
        self._sketch_marquee_idx = 0
        self._sketch_marquee_dir = 1
        self._update_sketch_marquee()
        self._set_status(f"Project: {self.sketch_dir_path.name}", Theme.CYAN)
        self._save_active_folder(self.sketch_dir_path)
        try:
            align_sketch_filename_case(self.sketch_dir_path)
            heal_platformio_ini_symlinks_and_dirs(self._platformio_ini_path(), self.sketch_dir_path)
            if not is_unc_or_network_path(self.sketch_dir_path):
                hide_internal_project_metadata(self.sketch_dir_path)
        except Exception:
            pass

        # Update notification store and sync hardware state for newly active project
        try:
            if hasattr(self, "_get_project_notif_db_path") and dbs_create is not None and hasattr(dbs_create, "set_default_db_path"):
                dbs_create.set_default_db_path(self._get_project_notif_db_path())
            if hasattr(self, "_sync_project_hardware_state"):
                self._sync_project_hardware_state()
            if hasattr(self, "_load_persistent_notifications"):
                self._load_persistent_notifications()
        except Exception as exc:
            print(f"[MCU Flasher] Error updating project state & notifications: {exc}")

        if hasattr(self, "_load_editor_files") and not getattr(self, "_editor_files_load_pending", False):
            try:
                self._load_editor_files()
            except Exception:
                pass

        try:
            add_recent_project(str(self.sketch_dir_path))
        except Exception:
            pass
        
        # Auto-detect and set correct board based on new project files.
        # If a port is already selected, re-run the esptool probe so that
        # switching projects (without unplugging) still picks the right board
        # (e.g. going from Arduino R3 → ESP32-S3 with the same port connected).
        port = self.port_var.get()
        if port and not port.startswith("─"):
            threading.Thread(
                target=self._auto_detect_board_from_port,
                args=(port,),
                daemon=True,
            ).start()
        else:
            self._auto_select_board(show_msg=True)

        self._compat_warnings_approved_hash = None
        self._load_compile_cache()
        self._update_skip_compile_state()

        # Project reporting performs network-share reads, volume checks, and
        # include resolution. Keep it in the bounded worker pool; only its
        # console/status callbacks return to Tk through the UI queue.
        self._run_bg_task(
            self._report_project_includes,
            Path(self.sketch_dir_path),
            self.port_var.get(),
            self.board_var.get(),
        )

        self._update_editor_info()

        # Restore the '🔧 Compatible Devices' list cached at the last compile
        # (the analysis itself is compile-driven — folder switches only
        # reload the snapshot, never re-scan).
        self._load_compat_cache()

