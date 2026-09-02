#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import os
import time
import subprocess
from typing import TYPE_CHECKING
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox


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

class SettingsDialogMixin(_Base):
    """Mixin providing SettingsDialogMixin capabilities for MCUUploadGUI."""
    def _restart_periodic_reload(self):
        """Cancel any existing periodic reload timer and restart if enabled."""
        if getattr(self, "_periodic_reload_after_id", None):
            try:
                self.root.after_cancel(self._periodic_reload_after_id)
            except Exception:
                pass
            self._periodic_reload_after_id = None

        if getattr(self, "periodic_reload_enabled", False):
            interval_ms = max(2000, int(getattr(self, "periodic_reload_interval_s", 5)) * 1000)
            self._periodic_reload_after_id = self.root.after(interval_ms, self._periodic_reload_tick)

    def _periodic_reload_tick(self):
        """Timer tick — reload open tabs/files if disk content differs from memory buffer."""
        if not getattr(self, "periodic_reload_enabled", False):
            self._periodic_reload_after_id = None
            return

        try:
            if getattr(self, "editor_mode", "default") == "monaco":
                # Skip auto-reload if the editor buffer is modified / dirty to protect user's typing
                is_dirty = False
                if hasattr(self, "editor_api") and self.editor_api:
                    active_path = getattr(self.editor_api, "active_file_path", None)
                    if active_path:
                        is_dirty = self.editor_api.modified_files.get(active_path, False)
                    else:
                        is_dirty = any(self.editor_api.modified_files.values())

                if not is_dirty:
                    if hasattr(self, "_reload_current_editor_file") and callable(self._reload_current_editor_file):
                        self._reload_current_editor_file()
            else:
                if hasattr(self, "_reload_default_tabs_if_changed") and callable(self._reload_default_tabs_if_changed):
                    self._reload_default_tabs_if_changed()
        except Exception:
            pass

        if getattr(self, "periodic_reload_enabled", False):
            interval_ms = max(2000, int(getattr(self, "periodic_reload_interval_s", 5)) * 1000)
            self._periodic_reload_after_id = self.root.after(interval_ms, self._periodic_reload_tick)

    def _build_editor_restart_command(self) -> list[str]:
        """Build a clean command line for an editor-mode restart.

        Skips the bootstrap pipeline since dependencies and tools are already
        verified on this machine. Directly restarts the GUI with the open project.
        """
        project_path = str(Path(self.sketch_dir_path).resolve(strict=False))
        gui_script = SCRIPT_DIR / "mcu_flash_gui.py"
        if not gui_script.exists():
            gui_script = SCRIPT_DIR / "main" / "mcu_flash_gui.py"

        venv_python = SCRIPT_DIR / "env" / "Scripts" / "pythonw.exe"
        if not venv_python.exists():
            venv_python = SCRIPT_DIR / "env" / "Scripts" / "python.exe"
        py_exe = str(venv_python if venv_python.exists() else sys.executable)

        command = [py_exe, str(gui_script), "--project", project_path]
        if "--new-window" in sys.argv:
            command.append("--new-window")
        return command

    def _confirm_restart_edits(self, parent=None) -> bool:
        """Protect modified editor buffers before an application restart."""
        modified = []
        if getattr(self, "editor_mode", "default") == "monaco":
            api = getattr(self, "editor_api", None)
            if api:
                modified = [
                    Path(path).name
                    for path, is_modified in api.modified_files.items()
                    if is_modified
                ]
        else:
            tab_data = getattr(self, "editor_tab_data", None) or {}
            modified = [
                data["path"].name for data in tab_data.values()
                if data.get("modified")
            ]

        if not modified:
            return True

        from tkinter import messagebox
        names = "\n  • ".join(modified)
        answer = messagebox.askyesnocancel(
            "Unsaved Changes",
            f"The following files have unsaved changes:\n\n  • {names}\n\n"
            "Save before restarting?",
            parent=parent or self.root,
        )
        if answer is None:
            return False
        if answer:
            if hasattr(self, "_save_all_editor_files") and callable(self._save_all_editor_files):
                self._save_all_editor_files()

            # Saving is synchronous in both editor implementations.  Abort the
            # restart if any buffer still reports dirty (for example, a write
            # failed because the project is read-only).
            if getattr(self, "editor_mode", "default") == "monaco":
                api = getattr(self, "editor_api", None)
                still_dirty = bool(api and any(api.modified_files.values()))
            else:
                still_dirty = any(
                    data.get("modified")
                    for data in (getattr(self, "editor_tab_data", None) or {}).values()
                )
            if still_dirty:
                messagebox.showerror(
                    "Restart Cancelled",
                    "One or more files could not be saved. The current window will remain open.",
                    parent=parent or self.root,
                )
                return False
        return True

    def _restart_for_editor_change(self, parent=None) -> bool:
        """Start a replacement editor process without blocking the Tk window."""
        from tkinter import messagebox
        import subprocess

        if getattr(self, "_editor_restart_in_progress", False):
            return True
        if self.is_busy:
            messagebox.showwarning(
                "Restart Unavailable",
                "Finish or stop the current compile/upload/reset operation before restarting.",
                parent=parent or self.root,
            )
            return False
        if not self._confirm_restart_edits(parent=parent):
            return False

        command = self._build_editor_restart_command()
        logs_dir = SCRIPT_DIR / "logs"
        restart_log = logs_dir / "editor_restart.log"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            restart_log.write_text(
                "Editor restart requested.\n"
                f"Current PID: {os.getpid()}\n"
                f"Command: {command!r}\n",
                encoding="utf-8",
            )
            # Remove stale crash output so an immediate restart failure can
            # only report diagnostics from the replacement being launched now.
            (logs_dir / "gui_crash.log").write_text("", encoding="utf-8")
        except Exception:
            pass

        released_mutex = _release_gui_instance()
        try:
            env = os.environ.copy()
            if getattr(sys, "frozen", False):
                # Prevent a PyInstaller child from inheriting the current
                # extraction directory as though it were the same process.
                env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
                env.pop("_MEIPASS", None)
                env.pop("_MEIPASS2", None)

            startupinfo = None
            creationflags = 0
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            replacement = subprocess.Popen(
                command,
                cwd=str(SCRIPT_DIR),
                env=env,
                stdin=subprocess.DEVNULL,
                startupinfo=startupinfo,
                creationflags=creationflags,
                close_fds=True,
            )
        except Exception as exc:
            if released_mutex:
                _claim_gui_instance()
            try:
                with restart_log.open("a", encoding="utf-8") as stream:
                    stream.write(f"Could not launch replacement: {exc!r}\n")
            except Exception:
                pass
            messagebox.showerror(
                "Restart Failed",
                f"The replacement application could not be started:\n\n{exc}\n\n"
                "The current window has been kept open.\n\n"
                f"Log: {restart_log}",
                parent=parent or self.root,
            )
            return False

        # The previous implementation waited here with time.sleep() for two
        # seconds. Because this method runs on Tk's UI thread, that made the
        # settings dialog and main window look frozen exactly while Monaco was
        # starting. Keep the old window responsive behind a small handoff cover
        # and poll through root.after() instead.
        self._editor_restart_in_progress = True
        handoff_overlay = None
        try:
            if parent is not None and parent.winfo_exists():
                parent.grab_release()
                parent.withdraw()
            handoff_overlay = CircularLoadingOverlay(
                self.root,
                bg_color=Theme.BG_DARKEST,
                spinner_color=Theme.CYAN,
                text="⚡ Switching to Monaco Editor",
            )
            handoff_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            handoff_overlay.lift()
            handoff_overlay.update_message(
                "⚡ Switching to Monaco Editor",
                "Starting the VS Code-style editor…",
            )
        except Exception:
            handoff_overlay = None

        handoff_deadline = time.monotonic() + 2.0

        def _remove_handoff_overlay():
            if handoff_overlay is not None:
                try:
                    handoff_overlay.stop_and_destroy()
                except Exception:
                    try:
                        handoff_overlay.destroy()
                    except Exception:
                        pass

        def _restore_current_gui(exit_code):
            self._editor_restart_in_progress = False
            _remove_handoff_overlay()
            if released_mutex:
                _claim_gui_instance()
            crash_log = SCRIPT_DIR / "logs" / "gui_crash.log"
            try:
                detail = crash_log.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            try:
                with restart_log.open("a", encoding="utf-8") as stream:
                    stream.write(f"Replacement exited early with code {exit_code}.\n")
                    if detail:
                        stream.write(detail[-4000:] + "\n")
            except Exception:
                pass
            try:
                if parent is not None and parent.winfo_exists():
                    parent.deiconify()
                    parent.lift()
                    parent.grab_set()
            except Exception:
                pass
            messagebox.showerror(
                "Restart Failed",
                f"The Monaco replacement process exited before it was ready "
                f"(code {exit_code}).\n\n"
                "The current window has been kept open.\n\n"
                f"Log: {restart_log}",
                parent=parent if parent is not None else self.root,
            )

        def _finish_handoff():
            _remove_handoff_overlay()
            try:
                with restart_log.open("a", encoding="utf-8") as stream:
                    stream.write(f"Replacement PID {replacement.pid} remained alive; closing old GUI.\n")
            except Exception:
                pass
            try:
                self._cleanup_active_editor()
            except Exception:
                pass
            try:
                self._do_stop()
            except Exception:
                pass
            try:
                if self.serial_conn and self.serial_conn.is_open:
                    self.serial_conn.close()
            except Exception:
                pass
            try:
                self.root.destroy()
            except Exception:
                pass
            os._exit(0)

        def _poll_replacement():
            exit_code = replacement.poll()
            if exit_code is not None:
                _restore_current_gui(exit_code)
                return
            if time.monotonic() >= handoff_deadline:
                _finish_handoff()
                return
            try:
                self.root.after(75, _poll_replacement)
            except Exception:
                _finish_handoff()

        self.root.after(75, _poll_replacement)
        return True

    def _open_settings(self):
        # Settings are intentionally locked while an operation is active.  The
        # wide Settings button is disabled by _set_buttons_state(); keep this
        # guard as the final authority so compact-menu/keyboard/programmatic
        # paths cannot bypass that lock.
        try:
            settings_disabled = str(self.btn_settings.cget("state")) == str(tk.DISABLED)
        except Exception:
            settings_disabled = False
        if getattr(self, "is_busy", False) or settings_disabled:
            self._set_status("Busy — Settings are available when the current operation finishes", Theme.YELLOW)
            return

        # Create dialog
        dlg = tk.Toplevel(self.root)
        dlg.title("MCU Flasher Settings")
        
        # Set window icon if available
        try:
            icon_path = SCRIPT_DIR / "src" / "assets" / "mcu_icon.ico"
            if not icon_path.exists():
                icon_path = SCRIPT_DIR / "src" / "mcu_icon.ico"
            if icon_path.exists():
                dlg.iconbitmap(str(icon_path))
        except Exception:
            pass
        
        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        if not isinstance(board_info, dict):
            board_info = {}
        platform = board_info.get("platform", "").lower()
        reset_capabilities = board_reset_capabilities(
            platform,
            board_info.get("board", ""),
            board_name,
            board_info.get("framework", ""),
        )
        # USB bridge names are not MCU identities. Only show destructive Hard
        # Reset when the selected board has a registered safe handler.
        is_esp = bool(reset_capabilities.get("hard_reset_ui"))
        can_soft_reset = bool(
            board_info.get("pio_resolved", True)
            and reset_capabilities.get("soft_reset")
        )

        dlg.configure(bg=Theme.BG_DARKEST)
        dlg.resizable(True, True)
        dlg.transient(self.root)
        dlg.grab_set()

        settings_scale = _get_widget_dpi_scale(dlg)

        def sp(value):
            return max(1, round(value * settings_scale))

        settings_host = tk.Frame(dlg, bg=Theme.BG_DARKEST)
        settings_host.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        settings_host.grid_rowconfigure(0, weight=1)
        settings_host.grid_columnconfigure(0, weight=1)
        settings_canvas = tk.Canvas(
            settings_host, bg=Theme.BG_DARKEST, highlightthickness=0, borderwidth=0
        )
        settings_scroll = ttk.Scrollbar(
            settings_host, orient=tk.VERTICAL, command=settings_canvas.yview
        )
        settings_canvas.configure(yscrollcommand=settings_scroll.set)
        settings_canvas.grid(row=0, column=0, sticky="nsew")
        settings_body = tk.Frame(settings_canvas, bg=Theme.BG_DARKEST)
        settings_window = settings_canvas.create_window(
            (0, 0), window=settings_body, anchor=tk.NW
        )
        scroll_state = {"visible": False}
        settings_layout_state = {"narrow": None}

        def _sync_settings_scrollbar():
            if not settings_canvas.winfo_exists():
                return
            bbox = settings_canvas.bbox("all")
            content_height = (bbox[3] - bbox[1]) if bbox else 0
            needs_scroll = content_height > settings_canvas.winfo_height() + 1
            if needs_scroll and not scroll_state["visible"]:
                settings_scroll.grid(row=0, column=1, sticky="ns")
                scroll_state["visible"] = True
            elif not needs_scroll and scroll_state["visible"]:
                settings_scroll.grid_remove()
                settings_canvas.yview_moveto(0)
                scroll_state["visible"] = False

        def _on_settings_body_configure(_event=None):
            settings_canvas.configure(scrollregion=settings_canvas.bbox("all"))
            settings_canvas.after_idle(_sync_settings_scrollbar)

        def _on_settings_canvas_configure(event):
            settings_canvas.itemconfigure(settings_window, width=event.width)
            note_wrap = max(sp(240), event.width - sp(100))
            try:
                editor_note.configure(wraplength=note_wrap)
                autosave_note.configure(wraplength=note_wrap)
                hide_warnings_note.configure(wraplength=note_wrap)
            except (NameError, tk.TclError):
                pass
            narrow = (event.width / settings_scale) < 470
            if settings_layout_state["narrow"] != narrow:
                settings_layout_state["narrow"] = narrow
                try:
                    for widget in (
                        cpu_label, cpu_combo, monitor_font_label,
                        monitor_font_combo, monitor_font_unit,
                        theme_label, theme_combo,
                        editor_label, editor_combo,
                    ):
                        widget.pack_forget()
                    if narrow:
                        cpu_label.pack(anchor=tk.W, pady=(0, sp(3)))
                        cpu_combo.pack(fill=tk.X)
                        monitor_font_label.pack(anchor=tk.W, pady=(0, sp(3)))
                        monitor_font_combo.pack(side=tk.LEFT)
                        monitor_font_unit.pack(side=tk.LEFT, padx=(sp(5), 0))
                        theme_label.pack(anchor=tk.W, pady=(0, sp(3)))
                        theme_combo.pack(fill=tk.X)
                        editor_label.pack(anchor=tk.W, pady=(0, sp(3)))
                        editor_combo.pack(fill=tk.X)
                    else:
                        cpu_label.pack(side=tk.LEFT, padx=(0, sp(10)))
                        cpu_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
                        monitor_font_label.pack(side=tk.LEFT, padx=(0, sp(10)))
                        monitor_font_combo.pack(side=tk.LEFT)
                        monitor_font_unit.pack(side=tk.LEFT, padx=(sp(5), 0))
                        theme_label.pack(side=tk.LEFT, padx=(0, sp(10)))
                        theme_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
                        editor_label.pack(side=tk.LEFT, padx=(0, sp(10)))
                        editor_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)

                    if btn_hard is not None:
                        btn_hard.pack_forget()
                        btn_soft.pack_forget()
                        if narrow:
                            btn_hard.pack(fill=tk.X, padx=sp(6), pady=(0, sp(5)))
                            btn_soft.pack(fill=tk.X, padx=sp(6))
                        else:
                            btn_hard.pack(side=tk.LEFT, padx=sp(10), expand=True, fill=tk.X)
                            btn_soft.pack(side=tk.LEFT, padx=sp(10), expand=True, fill=tk.X)
                except (NameError, tk.TclError):
                    pass
            settings_canvas.after_idle(_sync_settings_scrollbar)

        def _on_settings_mousewheel(event):
            if scroll_state["visible"]:
                settings_canvas.yview_scroll(int(-event.delta / 120), "units")
                return "break"
            return None

        settings_body.bind("<Configure>", _on_settings_body_configure)
        settings_canvas.bind("<Configure>", _on_settings_canvas_configure)
        dlg.bind("<MouseWheel>", _on_settings_mousewheel)

        # Section: CPU Cores MultiThreading
        tk.Label(settings_body, text="Performance Settings", font=self.font_title, fg=Theme.CYAN, bg=Theme.BG_DARKEST).pack(pady=(sp(12), sp(5)))

        cpu_frame = tk.Frame(settings_body, bg=Theme.BG_DARKEST)
        cpu_frame.pack(fill=tk.X, padx=sp(25), pady=sp(5))

        cpu_label = tk.Label(
            cpu_frame, text="CPU Cores Multithreading:", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST
        )
        cpu_label.pack(side=tk.LEFT, padx=(0, sp(10)))
        
        total_processors = os.cpu_count() or 4
        reserved_processors = _system_reserved_cpu_count(total_processors)
        low_jobs = _resource_safe_worker_count("LOW", total_processors)
        medium_jobs = _resource_safe_worker_count("MEDIUM", total_processors)
        high_jobs = _resource_safe_worker_count("HIGH", total_processors)

        low_val = f"LOW ({low_jobs} Jobs)"
        med_val = f"MEDIUM ({medium_jobs} Jobs)"
        high_val = f"HIGH ({high_jobs} Jobs + {reserved_processors} Reserved)"
        
        try:
            current_setting = _load_raw_config().get("shared", {}).get("cpu_multithreading", "HIGH")
        except Exception:
            current_setting = "HIGH"
            
        default_combo_val = high_val
        if current_setting == "LOW":
            default_combo_val = low_val
        elif current_setting == "MEDIUM":
            default_combo_val = med_val
            
        cpu_var = tk.StringVar(value=default_combo_val)
        cpu_combo = ttk.Combobox(
            cpu_frame, textvariable=cpu_var, font=self.font_label, state="readonly",
            values=[low_val, med_val, high_val], width=30
        )
        cpu_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        if self.is_busy:
            cpu_combo.configure(state="disabled")

        try:
            current_g_setting = _load_raw_config().get("shared", {}).get("graphics_acceleration", "ON")
        except Exception:
            current_g_setting = "ON"

        g_var = tk.BooleanVar(value=(current_g_setting == "ON"))
        
        g_frame = tk.Frame(settings_body, bg=Theme.BG_DARKEST)
        g_frame.pack(fill=tk.X, padx=sp(25), pady=sp(5))

        cb_g_accel = tk.Checkbutton(
            g_frame, text="Graphics Acceleration (Smooth sash resize)", variable=g_var,
            font=self.font_label, fg=Theme.TEXT, bg=Theme.BG_DARKEST,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_DARKEST,
            activeforeground=Theme.TEXT,
        )
        cb_g_accel.pack(side=tk.LEFT)
        if self.is_busy:
            cb_g_accel.configure(state="disabled")

        monitor_font_frame = tk.Frame(settings_body, bg=Theme.BG_DARKEST)
        monitor_font_frame.pack(fill=tk.X, padx=sp(25), pady=sp(5))
        monitor_font_label = tk.Label(
            monitor_font_frame, text="Build / Serial / Syntax font size:",
            font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST,
        )
        monitor_font_label.pack(side=tk.LEFT, padx=(0, sp(10)))
        monitor_font_var = tk.StringVar(value=str(self.monitor_font_size))
        monitor_font_combo = ttk.Combobox(
            monitor_font_frame, textvariable=monitor_font_var, state="readonly",
            values=[str(size) for size in range(8, 25)], width=5,
            font=self.font_label,
        )
        monitor_font_combo.pack(side=tk.LEFT)
        monitor_font_unit = tk.Label(
            monitor_font_frame, text="pt", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST
        )
        monitor_font_unit.pack(side=tk.LEFT, padx=(sp(5), 0))
        if self.is_busy:
            monitor_font_combo.configure(state="disabled")

        current_hide_build_console_warnings = get_hide_build_console_warnings()
        hide_build_console_warnings_var = tk.BooleanVar(
            value=current_hide_build_console_warnings
        )
        hide_warnings_frame = tk.Frame(settings_body, bg=Theme.BG_DARKEST)
        hide_warnings_frame.pack(fill=tk.X, padx=sp(25), pady=sp(5))
        cb_hide_warnings = tk.Checkbutton(
            hide_warnings_frame,
            text="Hide warnings in Build Console",
            variable=hide_build_console_warnings_var,
            font=self.font_label,
            fg=Theme.TEXT,
            bg=Theme.BG_DARKEST,
            selectcolor=Theme.BG_DARK,
            activebackground=Theme.BG_DARKEST,
            activeforeground=Theme.TEXT,
        )
        cb_hide_warnings.pack(side=tk.LEFT)
        if self.is_busy:
            cb_hide_warnings.configure(state="disabled")

        hide_warnings_note = tk.Label(
            settings_body,
            text=(
                "Only warning-tagged lines are hidden from the Build Console; "
                "compiler and toolchain behavior is unchanged."
            ),
            font=self.font_label,
            fg=Theme.TEXT_DIM,
            bg=Theme.BG_DARKEST,
            wraplength=sp(440),
            justify=tk.LEFT,
        )
        hide_warnings_note.pack(fill=tk.X, padx=sp(25), pady=(0, sp(5)))

        # Horizontal separator
        sep_theme = tk.Frame(settings_body, bg=Theme.BORDER, height=1)
        sep_theme.pack(fill=tk.X, padx=sp(25), pady=sp(10))

        # Section: Appearance & Theme
        tk.Label(settings_body, text="Appearance & Theme", font=self.font_title, fg=Theme.CYAN, bg=Theme.BG_DARKEST).pack(pady=(0, sp(5)))

        theme_frame = tk.Frame(settings_body, bg=Theme.BG_DARKEST)
        theme_frame.pack(fill=tk.X, padx=sp(25), pady=sp(5))

        theme_label = tk.Label(
            theme_frame, text="Color Theme:", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST
        )
        theme_label.pack(side=tk.LEFT, padx=(0, sp(10)))

        theme_default_label = "Default (Dark Cyberpunk)"
        theme_light_label = "Light (Clean & Bright)"
        theme_solarized_label = "Solarized Dark (Teal / Cyan)"

        current_theme_mode = get_theme_mode()
        if current_theme_mode == "light":
            _start_theme_val = theme_light_label
        elif current_theme_mode in ("solarized_dark", "solarized"):
            _start_theme_val = theme_solarized_label
        else:
            _start_theme_val = theme_default_label

        theme_var = tk.StringVar(value=_start_theme_val)
        theme_combo = ttk.Combobox(
            theme_frame, textvariable=theme_var, font=self.font_label, state="readonly",
            values=[theme_default_label, theme_light_label, theme_solarized_label], width=36
        )
        theme_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        if self.is_busy:
            theme_combo.configure(state="disabled")

        theme_system_frame = tk.Frame(settings_body, bg=Theme.BG_DARKEST)
        theme_system_frame.pack(fill=tk.X, padx=sp(25), pady=(sp(2), sp(2)))

        current_theme_saved, current_theme_follow_sys = get_theme_settings()
        theme_system_var = tk.BooleanVar(value=current_theme_follow_sys)

        def _on_theme_system_toggle():
            if theme_system_var.get():
                detected = _detect_system_theme()
                theme_var.set(theme_light_label if detected == "light" else theme_default_label)
                theme_combo.configure(state="disabled")
            else:
                if not self.is_busy:
                    theme_combo.configure(state="readonly")
                    if current_theme_saved == "light":
                        theme_var.set(theme_light_label)
                    elif current_theme_saved in ("solarized_dark", "solarized"):
                        theme_var.set(theme_solarized_label)
                    else:
                        theme_var.set(theme_default_label)

        cb_theme_system = tk.Checkbutton(
            theme_system_frame, text="System Default (Follow Windows Light / Dark mode)",
            variable=theme_system_var, command=_on_theme_system_toggle,
            font=self.font_label, fg=Theme.TEXT, bg=Theme.BG_DARKEST,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_DARKEST,
            activeforeground=Theme.TEXT,
        )
        cb_theme_system.pack(side=tk.LEFT)
        if self.is_busy:
            cb_theme_system.configure(state="disabled")

        _on_theme_system_toggle()

        theme_note = tk.Label(
            settings_body, text="Changing the theme applies across the main UI, editor, and integrated terminal.",
            font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, wraplength=sp(440), justify=tk.LEFT
        )
        theme_note.pack(fill=tk.X, padx=sp(25), pady=(0, sp(5)))

        # Horizontal separator
        sep_editor = tk.Frame(settings_body, bg=Theme.BORDER, height=1)
        sep_editor.pack(fill=tk.X, padx=sp(25), pady=sp(10))

        # Section: File Editor
        tk.Label(settings_body, text="File Editor", font=self.font_title, fg=Theme.CYAN, bg=Theme.BG_DARKEST).pack(pady=(0, sp(5)))

        editor_frame = tk.Frame(settings_body, bg=Theme.BG_DARKEST)
        editor_frame.pack(fill=tk.X, padx=sp(25), pady=sp(5))

        editor_label = tk.Label(
            editor_frame, text="Editor:", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST
        )
        editor_label.pack(side=tk.LEFT, padx=(0, sp(10)))

        default_label = "Default (Tkinter, lightweight)"
        monaco_label = "Monaco (VS Code-style, heavier)"
        current_editor_mode = getattr(self, "editor_mode", None) or get_editor_mode()

        if current_editor_mode == "monaco":
            _start_val = monaco_label
        else:
            _start_val = default_label
        editor_var = tk.StringVar(value=_start_val)

        editor_combo = ttk.Combobox(
            editor_frame, textvariable=editor_var, font=self.font_label, state="readonly",
            values=[default_label, monaco_label], width=36
        )
        editor_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        if self.is_busy:
            editor_combo.configure(state="disabled")

        editor_note = tk.Label(
            settings_body, text="Changing the editor takes effect the next time the app is started.",
            font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, wraplength=sp(440), justify=tk.LEFT
        )
        editor_note.pack(fill=tk.X, padx=sp(25), pady=(0, sp(5)))

        # Track whether the user has confirmed the Monaco crash-risk warning
        # during this dialog session, so we don't nag them repeatedly if they
        # flip the combobox back and forth before hitting Save.
        editor_var._monaco_confirmed = (current_editor_mode == "monaco")

        def _on_editor_choice(event=None):
            if editor_var.get() == monaco_label and not getattr(editor_var, "_monaco_confirmed", False):
                from tkinter import messagebox
                proceed = messagebox.askyesno(
                    "Monaco Editor Warning",
                    "The Monaco editor is a heavier, browser-based editor.\n\n"
                    "On low-spec devices, it may cause the application to "
                    "freeze or crash on startup.\n\n"
                    "Do you want to continue selecting Monaco?",
                    parent=dlg
                )
                if proceed:
                    editor_var._monaco_confirmed = True
                else:
                    editor_var.set(default_label)

        editor_combo.bind("<<ComboboxSelected>>", _on_editor_choice)

        # Horizontal separator
        sep_autosave_top = tk.Frame(settings_body, bg=Theme.BORDER, height=1)
        sep_autosave_top.pack(fill=tk.X, padx=sp(25), pady=sp(10))

        # Section: Auto-Save
        tk.Label(settings_body, text="Auto-Save", font=self.font_title, fg=Theme.CYAN, bg=Theme.BG_DARKEST).pack(pady=(0, sp(5)))

        autosave_frame = tk.Frame(settings_body, bg=Theme.BG_DARKEST)
        autosave_frame.pack(fill=tk.X, padx=sp(25), pady=sp(5))

        current_autosave_enabled, current_autosave_delay = get_autosave_settings()

        autosave_var = tk.BooleanVar(value=current_autosave_enabled)
        autosave_delay_var = tk.StringVar(value=str(current_autosave_delay))

        autosave_delay_ent = tk.Entry(
            autosave_frame, textvariable=autosave_delay_var, width=7,
            font=self.font_label, bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
            insertbackground=Theme.CYAN, borderwidth=0,
            highlightthickness=1, highlightcolor=Theme.CYAN_DIM, highlightbackground=Theme.BORDER,
            justify=tk.CENTER,
        )

        def _on_autosave_toggle():
            autosave_delay_ent.configure(state=(tk.NORMAL if autosave_var.get() else tk.DISABLED))

        def _on_autosave_toggle_user():
            _on_autosave_toggle()
            self._append(
                f"  ℹ Auto-Save: {'ON' if autosave_var.get() else 'OFF'}",
                "info"
            )

        cb_autosave = tk.Checkbutton(
            autosave_frame, text="Enable Auto-Save", variable=autosave_var,
            command=_on_autosave_toggle_user,
            font=self.font_label, fg=Theme.TEXT, bg=Theme.BG_DARKEST,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_DARKEST,
            activeforeground=Theme.TEXT,
        )
        cb_autosave.pack(side=tk.LEFT)

        autosave_delay_label = tk.Label(
            autosave_frame, text="Delay (ms):", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST
        )
        autosave_delay_label.pack(side=tk.LEFT, padx=(sp(15), sp(5)))
        autosave_delay_ent.pack(side=tk.LEFT)

        _on_autosave_toggle()  # apply initial enabled/disabled state to the delay field

        autosave_note = tk.Label(
            settings_body,
            text="Automatically saves modified files after you stop typing for the given delay.",
            font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, wraplength=sp(440), justify=tk.LEFT
        )
        autosave_note.pack(fill=tk.X, padx=sp(25), pady=(0, sp(5)))

        # Horizontal separator
        sep_startup = tk.Frame(settings_body, bg=Theme.BORDER, height=1)
        sep_startup.pack(fill=tk.X, padx=sp(25), pady=sp(10))

        # Section: Startup
        tk.Label(settings_body, text="Startup", font=self.font_title, fg=Theme.CYAN, bg=Theme.BG_DARKEST).pack(pady=(0, sp(5)))

        startup_frame = tk.Frame(settings_body, bg=Theme.BG_DARKEST)
        startup_frame.pack(fill=tk.X, padx=sp(25), pady=sp(5))
        tk.Label(
            startup_frame,
            text="Bootstrap runs before the main app on every launch.",
            font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST,
        ).pack(side=tk.LEFT)

        # Horizontal separator
        sep = tk.Frame(settings_body, bg=Theme.BORDER, height=1)
        sep.pack(fill=tk.X, padx=sp(25), pady=sp(10))

        # Reset & Recovery frame
        reset_frame = tk.LabelFrame(
            settings_body, text="Hardware Reset Operations", font=self.font_label,
            fg=Theme.CYAN, bg=Theme.BG_DARKEST, bd=1, relief=tk.SOLID,
            padx=sp(10), pady=sp(10)
        )
        reset_frame.pack(fill=tk.X, padx=sp(25), pady=sp(5))

        def run_hard_reset():
            self._do_hard_reset(dlg)

        def run_soft_reset():
            self._do_soft_reset(dlg)

        if is_esp:
            hard_reset_label = (
                "⚡ Hard Reset (Erase Flash)"
                if reset_capabilities.get("hard_strategy") == "esp8266_erase"
                else "⚡ Hard Reset (Bootloader)"
            )
            btn_hard = self._make_btn(
                reset_frame, hard_reset_label, run_hard_reset,
                Theme.BTN_STOP, Theme.BTN_STOP_H, font=self.font_label
            )
            btn_hard.pack(side=tk.LEFT, padx=sp(10), expand=True, fill=tk.X)

            btn_soft = self._make_btn(
                reset_frame, "🔄 Soft Reset (Reset Flash)", run_soft_reset,
                Theme.BTN_MONITOR, Theme.BTN_MONITOR_H, font=self.font_label
            )
            btn_soft.pack(side=tk.LEFT, padx=sp(10), expand=True, fill=tk.X)
        else:
            btn_hard = None
            btn_soft = self._make_btn(
                reset_frame, "🔄 Soft Reset (Reset Flash)", run_soft_reset,
                Theme.BTN_MONITOR, Theme.BTN_MONITOR_H, font=self.font_label
            )
            btn_soft.pack(fill=tk.X, padx=sp(10))

        if not can_soft_reset:
            btn_soft.configure(
                state=tk.DISABLED,
                text="Soft Reset unavailable (Arduino framework required)",
                cursor="arrow",
            )
        
        if not self._is_board_recognized():
            reset_disabled_state = tk.DISABLED
            if btn_hard is not None:
                btn_hard.configure(state=reset_disabled_state)
            btn_soft.configure(state=reset_disabled_state)
            tk.Label(
                reset_frame,
                text="⚠ Board on this port hasn't been recognized yet.",
                font=self.font_status, fg=Theme.YELLOW, bg=Theme.BG_DARKEST
            ).pack(side=tk.BOTTOM, pady=(sp(6), 0))

        btn_frame = tk.Frame(dlg, bg=Theme.BG_DARKEST)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=sp(10), padx=sp(25))
        
        def reset_settings():
            cpu_var.set(high_val)
            g_var.set(True)
            monitor_font_var.set("12")
            hide_build_console_warnings_var.set(False)
            theme_system_var.set(False)
            theme_var.set(theme_default_label)
            _on_theme_system_toggle()
            editor_var.set(default_label)
            editor_var._monaco_confirmed = False
            self._append("  ℹ Settings reset to default values. Click Save to apply.", "info")
            
        reset_btn = self._make_btn(btn_frame, "Reset Defaults", reset_settings, Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_label)
        reset_btn.pack(side=tk.LEFT, padx=sp(5))

        def save_settings():
            cpu_sel = cpu_var.get()
            if "LOW" in cpu_sel:
                cpu_key = "LOW"
            elif "MEDIUM" in cpu_sel:
                cpu_key = "MEDIUM"
            else:
                cpu_key = "HIGH"
            
            g_val = "ON" if g_var.get() else "OFF"
            try:
                monitor_font_size_new = max(8, min(24, int(monitor_font_var.get())))
            except (TypeError, ValueError):
                monitor_font_size_new = 12
            hide_build_console_warnings_new = hide_build_console_warnings_var.get()

            follow_sys = theme_system_var.get()
            if follow_sys:
                detected = _detect_system_theme()
                new_theme_mode = detected
                saved_mode = "default" if detected == "default" else "light"
            else:
                theme_sel = theme_var.get()
                if theme_sel == theme_light_label:
                    new_theme_mode = "light"
                elif theme_sel == theme_solarized_label:
                    new_theme_mode = "solarized_dark"
                else:
                    new_theme_mode = "default"
                saved_mode = new_theme_mode

            theme_changed = (new_theme_mode != current_theme_mode or follow_sys != current_theme_follow_sys)

            autosave_enabled_new = autosave_var.get()
            try:
                autosave_delay_new = max(200, int(autosave_delay_var.get()))
            except (TypeError, ValueError):
                self._append("  ✖ Auto-Save delay must be a whole number (ms). Settings not saved.", "error")
                return

            try:
                data = _load_raw_config()
                if "shared" not in data:
                    data["shared"] = {}
                data["shared"]["cpu_multithreading"] = cpu_key
                data["shared"]["graphics_acceleration"] = g_val
                data["shared"]["monitor_font_size"] = monitor_font_size_new
                data["shared"]["hide_build_console_warnings"] = hide_build_console_warnings_new
                data["shared"]["theme_mode"] = saved_mode
                data["shared"]["theme_follow_system"] = follow_sys
                set_theme_mode(saved_mode, follow_system=follow_sys)
                data["shared"]["autosave_enabled"] = autosave_enabled_new
                data["shared"]["autosave_delay_ms"] = autosave_delay_new
                data["shared"]["periodic_reload_enabled"] = False

                if editor_var.get() == monaco_label:
                    new_editor_mode = "monaco"
                else:
                    new_editor_mode = "default"
                mode_changed = new_editor_mode != current_editor_mode
                
                if mode_changed and current_editor_mode == "monaco" and new_editor_mode == "default":
                    from tkinter import messagebox
                    proceed = messagebox.askokcancel(
                        "Dispose Monaco Editor",
                        "Switching to another editor will dispose the Monaco editor.\n\n"
                        "To use Monaco again, you will need to restart the application.\n\n"
                        "Do you want to proceed?",
                        parent=dlg
                    )
                    if not proceed:
                        return

                data["shared"]["editor_mode"] = new_editor_mode
                if mode_changed:
                    # Fresh choice — clear any stale crash sentinel from a
                    # previous mode so the next boot check starts clean.
                    data["shared"]["monaco_boot_pending"] = False

                _save_raw_config(data)
                if cpu_key != current_setting:
                    self._append(f"  ✔ CPU multithreading set to {cpu_key}.", "success")
                if g_val != current_g_setting:
                    self._append(f"  ✔ Graphics acceleration set to {g_val}.", "success")
                    try:
                        self.main_pane.configure(opaqueresize=(g_val == "ON"))
                    except Exception:
                        pass
                if monitor_font_size_new != self.monitor_font_size:
                    self._apply_monitor_font_size(monitor_font_size_new)
                    self._append(f"  ✔ Build / Serial / Syntax font size set to {monitor_font_size_new} pt.", "success")
                if hide_build_console_warnings_new != current_hide_build_console_warnings:
                    self._append(
                        "  ✔ Build Console warnings: "
                        f"{'hidden' if hide_build_console_warnings_new else 'visible'}.",
                        "success",
                    )
                if theme_changed:
                    if follow_sys:
                        self._append(f"  ✔ Theme mode set to System Default ({new_theme_mode.replace('_', ' ').title()}).", "success")
                    else:
                        self._append(f"  ✔ Theme mode set to {new_theme_mode.replace('_', ' ').title()}.", "success")
                    self._apply_theme_to_ui(new_theme_mode)

                autosave_changed = (
                    autosave_enabled_new != current_autosave_enabled
                    or autosave_delay_new != current_autosave_delay
                )
                self.autosave_enabled = autosave_enabled_new
                self.autosave_delay_ms = autosave_delay_new
                if hasattr(self, "_monaco_autosave_worker") and self._monaco_autosave_worker:
                    self._monaco_autosave_worker.update_state()
                if not self.autosave_enabled and hasattr(self, "_autosave_cancel_all") and callable(self._autosave_cancel_all):
                    self._autosave_cancel_all()

                if autosave_changed:
                    if autosave_enabled_new:
                        self._append(
                            f"  ✔ Auto-Save: ON — delay {autosave_delay_new} ms.",
                            "success"
                        )
                    else:
                        self._append("  ✔ Auto-Save: OFF.", "success")

                self.periodic_reload_enabled = False

                if mode_changed:
                    if new_editor_mode == "monaco":
                        # Monaco requires pywebview running on the main thread —
                        # it cannot be hot-swapped at runtime. Always require a
                        # restart, regardless of whether editor_window still
                        # exists from a previous Monaco session (it's orphaned
                        # and cannot be re-embedded after cleanup).
                        from tkinter import messagebox
                        restart = messagebox.askyesno(
                            "Restart Required",
                            "Switching to the Monaco editor requires restarting the application.\n\n"
                            "Would you like to restart the application now?",
                            parent=dlg
                        )
                        # Save the preference for the next launch, but keep
                        # the live runtime marked as Default until a verified
                        # replacement actually takes over.  Setting
                        # self.editor_mode to Monaco here used to make the
                        # still-running Default editor follow Monaco-only close
                        # and save paths when the user declined or restart failed.
                        self._append("  ✔ Editor mode set to Monaco (requires restart to load).", "info")
                        if restart:
                            if self._restart_for_editor_change(parent=dlg):
                                return
                    else:
                        self._cleanup_active_editor()
                        self.editor_mode = new_editor_mode
                        self._build_editor(self.editor_frame)
                        self._append(f"  ✔ File editor switched to {new_editor_mode.capitalize()} instantly.", "success")
                
                try:
                    if hasattr(self, "_sync_project_hardware_state"):
                        self._sync_project_hardware_state()
                except Exception:
                    pass
            except Exception as e:
                self._append(f"  ✖ Failed to save settings: {e}", "error")

            dlg.destroy()

        save_btn = self._make_btn(btn_frame, "Save", save_settings, Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, font=self.font_label)
        save_btn.pack(side=tk.RIGHT, padx=sp(5))

        cancel_btn = self._make_btn(btn_frame, "Cancel", dlg.destroy, Theme.BTN_STOP, Theme.BTN_STOP_H, font=self.font_label)
        cancel_btn.pack(side=tk.RIGHT, padx=sp(5))

        # Fit the dialog to its scaled content. The canvas only scrolls when
        # the active monitor genuinely cannot provide enough vertical space.
        dlg.update_idletasks()
        desired_width = max(sp(540), settings_body.winfo_reqwidth() + sp(20))
        work_left, work_top, work_right, work_bottom = _get_monitor_work_area(dlg)
        max_width = max(sp(400), (work_right - work_left) - sp(40))
        max_height = max(sp(360), (work_bottom - work_top) - sp(50))
        desired_width = min(desired_width, max_width)
        # Apply the intended width before measuring height, otherwise Tk's
        # initial one-pixel canvas temporarily selects the narrow layout.
        settings_canvas.itemconfigure(settings_window, width=desired_width)
        initial_event = type("_SettingsConfigure", (), {"width": desired_width})()
        _on_settings_canvas_configure(initial_event)
        dlg.update_idletasks()
        desired_height = (
            settings_body.winfo_reqheight() + btn_frame.winfo_reqheight() + sp(22)
        )
        desired_height = min(desired_height, max_height)
        dlg.minsize(min(sp(420), desired_width), min(sp(420), desired_height))
        center_toplevel(
            dlg, self.root,
            width=desired_width / settings_scale,
            height=desired_height / settings_scale,
        )
        # The first measurement may use the narrow stacked layout while the
        # canvas is still only one pixel wide. Re-measure once the target
        # width is applied so a normal display does not retain blank space.
        dlg.update_idletasks()
        fitted_height = min(
            settings_body.winfo_reqheight() + btn_frame.winfo_reqheight() + sp(22),
            max_height,
        )
        if abs(fitted_height - desired_height) > sp(4):
            center_toplevel(
                dlg, self.root,
                width=desired_width / settings_scale,
                height=fitted_height / settings_scale,
            )
        dlg.after_idle(_sync_settings_scrollbar)
