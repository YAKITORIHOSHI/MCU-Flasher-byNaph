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
from typing import TYPE_CHECKING
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
from main.dialogs import *
from main.editor_api import *

if TYPE_CHECKING:
    from main.mcu_flash_gui import MCUUploadGUI
    _Base = MCUUploadGUI
else:
    _Base = object

class ProjectTerminalMixin(_Base):
    """Mixin providing ProjectTerminalMixin capabilities for MCUUploadGUI."""
    def _shell_terminal_is_selected(self) -> bool:
        """Return whether the lazy Project Terminal tab is visible."""
        try:
            if not getattr(self, "monitors_pane_visible", True):
                return False
            tab_id = getattr(self, "_shell_terminal_tab_id", None)
            current = self.bottom_notebook.select()
            return bool(tab_id and current) and str(current) == str(tab_id)
        except Exception:
            return False

    def _configure_shell_ansi_tags(self):
        """Mirror the OpenCode/xterm dark palette on the Tk terminal surface."""
        palette = {
            "default": "#cccccc",
            # OpenCode uses ANSI black for muted placeholder/help text. Pure
            # black disappears against the application's dark terminal
            # surface, so use a readable muted gray for the normal ANSI slot.
            "black": "#8a8f9d",
            "red": "#cd3131",
            "green": "#0dbc79",
            "yellow": "#e5e510",
            "blue": "#2472c8",
            "magenta": "#bc3fbc",
            "cyan": "#11a8cd",
            "white": "#e5e5e5",
            "bright_black": "#666666",
            "bright_red": "#f14c4c",
            "bright_green": "#23d18b",
            "bright_yellow": "#f5f543",
            "bright_blue": "#3b8eea",
            "bright_magenta": "#d670d6",
            "bright_cyan": "#29b8db",
            "bright_white": "#ffffff",
        }
        try:
            self._shell_bold_font = tkfont.Font(
                family="Consolas",
                size=int(self.font_mono.cget("size")),
                weight="bold",
            )
        except Exception:
            self._shell_bold_font = self.font_mono
        suffixes = (
            "normal", "bold", "dim", "underline", "bold_underline", "dim_underline",
        )
        for name, foreground in palette.items():
            for suffix in suffixes:
                tag = f"ansi_{name}_{suffix}"
                self.shell_console.tag_configure(
                    tag,
                    foreground=foreground,
                    background="#0c0d10",
                    font=(self._shell_bold_font if "bold" in suffix else self.font_mono),
                    underline=("underline" in suffix),
                )

    def _configure_shell_selection_highlight(self):
        """Use a compact xterm-style selection instead of Tk's full-row fill."""
        self._shell_selected_range = None
        self._shell_selection_syncing = False
        self._shell_selection_release_id = None
        self.shell_console.configure(
            # The native ``sel`` tag paints empty cells through the remainder
            # of a visual row.  Hide that layer and paint only the selected
            # character range with our own tag below.
            selectbackground="#0c0d10",
            selectforeground="#cccccc",
            inactiveselectbackground="#0c0d10",
        )
        self.shell_console.tag_configure(
            "shell_selection",
            background="#264f78",
            foreground="#ffffff",
        )

        def sync_selection(_event=None):
            if self._shell_selection_syncing:
                return
            self._shell_selection_syncing = True
            try:
                ranges = self.shell_console.tag_ranges("sel")
                self.shell_console.tag_remove("shell_selection", "1.0", tk.END)
                if ranges:
                    start, end = str(ranges[0]), str(ranges[1])
                    self._shell_selected_range = (start, end)
                    self.shell_console.tag_add("shell_selection", start, end)
                    # Keep Tk's native range alive.  Removing it during or
                    # after <<Selection>> prevents the Text class binding
                    # from extending the selection and can also trigger a
                    # delayed empty-selection event.  Its visual colors are
                    # hidden above, while this exact-range tag supplies the
                    # compact xterm-style highlight.
                else:
                    self._shell_selected_range = None
            finally:
                self._shell_selection_syncing = False

        def clear_selection_on_press(_event=None):
            # Start each new mouse selection from a clean custom overlay.
            self._shell_selected_range = None
            self.shell_console.tag_remove("shell_selection", "1.0", tk.END)

        def finalize_selection(_event=None):
            # Let Tk's class binding finish the drag/double-click selection
            # before replacing its row-wide visual with our compact tag.
            try:
                if self._shell_selection_release_id is not None:
                    self.shell_console.after_cancel(self._shell_selection_release_id)
            except Exception:
                pass
            self._shell_selection_release_id = self.shell_console.after_idle(
                sync_selection
            )

        self._sync_shell_selection = sync_selection
        self.shell_console.bind("<<Selection>>", sync_selection, add="+")
        self.shell_console.bind("<Button-1>", clear_selection_on_press, add="+")
        self.shell_console.bind("<ButtonRelease-1>", finalize_selection, add="+")

    def _shell_refresh_switcher(self):
        """Paint the narrow shell switcher without rebuilding its widgets."""
        active = getattr(self, "_shell_active_kind", "pwsh")
        sessions = getattr(self, "_shell_sessions", {})
        for kind, button in getattr(self, "_shell_switch_buttons", {}).items():
            selected = kind == active
            running = bool(sessions.get(kind, {}).get("running"))
            button.configure(
                bg=Theme.BG_HOVER if selected else Theme.BG_DARK,
                fg=Theme.TEXT_BRIGHT if selected else (Theme.GREEN if running else Theme.TEXT),
                state=tk.NORMAL,
            )

    def _shell_switch_button_click(self, event, kind: str):
        """Activate a shell switcher button on the Tk thread immediately."""
        try:
            event.widget.focus_set()
        except Exception:
            pass
        try:
            self.root.after(0, lambda: self._shell_select(kind))
        except Exception:
            self._shell_select(kind)
        return "break"

    def _shell_current_target(self) -> Path:
        """Use the active sketch path, including an existing remote mapping."""
        try:
            target = self._mapped_or_sketch_dir(self.sketch_dir_path)
        except Exception:
            target = self.sketch_dir_path
        return Path(target or Path.home())

    @staticmethod
    def _shell_executable(kind: str) -> str | None:
        if kind == "cmd":
            # Use the native system command processor, not a COMSPEC shim that
            # may have been overridden by another CLI or launcher.
            system_root = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
            if system_root:
                native_cmd = Path(system_root) / "System32" / "cmd.exe"
                if native_cmd.exists():
                    return str(native_cmd)
            return shutil.which("cmd.exe") or os.environ.get("COMSPEC") or "cmd.exe"
        # The PowerShell slot is intentionally Windows PowerShell, not an
        # arbitrary `pwsh`/profile/CLI shim. This keeps the prompt and editing
        # behavior identical to a normal powershell.exe console.
        system_root = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
        if system_root:
            native_powershell = (
                Path(system_root) / "System32" / "WindowsPowerShell" /
                "v1.0" / "powershell.exe"
            )
            if native_powershell.exists():
                return str(native_powershell)
        return shutil.which("powershell.exe") or shutil.which("powershell")

    @staticmethod
    def _shell_display_name(kind: str) -> str:
        return "PowerShell" if kind == "pwsh" else "Command Prompt"

    @staticmethod
    def _shell_clean_output(data) -> str:
        """Normalize PTY control output for a Tk Text surface."""
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        text_value = str(data or "")
        # Shell prompts occasionally include ANSI CSI sequences and OSC title
        # updates. Tk's Text widget cannot interpret them; leaving an OSC title
        # update behind produces visible ``]0;Administrator`` garbage.
        text_value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text_value)
        text_value = re.sub(r"(?:\x1b)?\]0;[^\r\n]*(?:\x07|\x1b\\)?", "", text_value)
        text_value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text_value)
        text_value = text_value.replace("\x1b", "").replace("\x07", "")
        # Some Windows PTY paths strip ESC but leave the CSI payload. Do not
        # expose those fragments (for example ``[1;2m``) in the fallback view.
        text_value = re.sub(r"\[(?:\d{1,3}(?:;\d{1,3})*)m", "", text_value)
        # The remote-path bootstrap uses one quieting command before pushd;
        # keep that implementation detail out of the visible terminal.
        text_value = re.sub(r"(?im)^.*@echo\s+off.*(?:\n|$)", "", text_value)
        return text_value.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _shell_quote_powershell(path: str) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    def _shell_cd_command(self, kind: str, target: str) -> str:
        if kind == "cmd":
            # pushd handles UNC folders by assigning a temporary drive letter;
            # cd /d handles ordinary local or already-mapped paths.
            if is_unc_or_network_path(Path(target)):
                return f'pushd "{target}"\r\n'
            return f'cd /d "{target}"\r\n'
        return f"Set-Location -LiteralPath {self._shell_quote_powershell(target)}\r\n"

    def _shell_select(self, kind: str):
        """Switch shell sessions and start the selected one on demand."""
        if kind not in ("pwsh", "cmd"):
            return "break"
        self._shell_active_kind = kind
        self._shell_refresh_switcher()

        # Normal path: the actual terminal is xterm.js backed by a native
        # PowerShell/CMD PTY in the isolated child process.  The old Tk path
        # below remains available only after the native path explicitly fails.
        if not getattr(self, "_project_terminal_fallback", False):
            self._ensure_project_terminal_webview()
            if not getattr(self, "_project_terminal_fallback", False):
                self._project_terminal_send_control("select", kind)
                self._shell_set_status(
                    f"Loading {self._shell_display_name(kind)} • {self._shell_current_target()}"
                )
                return "break"

        session = self._shell_sessions.get(kind)
        if not session or not session.get("running"):
            self._shell_start(kind)
        else:
            target = str(self._shell_current_target())
            if session.get("target") != target:
                try:
                    session["pty"].write(self._shell_cd_command(kind, target))
                    session["target"] = target
                except Exception:
                    pass
        self._shell_render_session(kind)
        try:
            self.shell_console.focus_set()
        except Exception:
            pass
        return "break"

    def _ensure_project_terminal_webview(self):
        """Start the same child WebView2/PTTY architecture used by OpenCode."""
        if getattr(self, "_project_terminal_fallback", False):
            return False
        proc = getattr(self, "_project_terminal_proc", None)
        if proc is not None:
            try:
                if proc.poll() is None:
                    return True
            except Exception:
                pass

        if getattr(self, "_project_terminal_launching", False):
            return True

        script_path = SCRIPT_DIR / "src" / "modules" / "project_terminal.py"
        if (
            not script_path.exists()
            or sys.platform != "win32"
            or win32gui is None
            or win32con is None
        ):
            self._activate_project_terminal_fallback(
                "Native Project Terminal is unavailable on this platform."
            )
            return False

        try:
            fd, port_file = tempfile.mkstemp(prefix="mcu-terminal-", suffix=".json")
            os.close(fd)
            try:
                os.unlink(port_file)
            except OSError:
                pass
            target = str(self._shell_current_target())
            initial_cwd = str(getattr(self, "_shell_initial_cwd", Path.cwd()))
            launcher = sys.executable
            if getattr(sys, "frozen", False):
                for candidate in (
                    SCRIPT_DIR / "env" / "Scripts" / "pythonw.exe",
                    SCRIPT_DIR / "env" / "Scripts" / "python.exe",
                ):
                    if candidate.exists():
                        launcher = str(candidate)
                        break
            command = [
                launcher,
                str(script_path),
                "--launch-terminal",
                target,
                "--initial-cwd",
                initial_cwd,
                "--port-file",
                port_file,
            ]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._project_terminal_proc = subprocess.Popen(
                command,
                cwd=str(SCRIPT_DIR),
                creationflags=flags,
            )
            self._project_terminal_port_file = Path(port_file)
            self._project_terminal_port = None
            self._project_terminal_launching = True
            self._project_terminal_embed_attempts = 0
            self._shell_set_status("Starting native Project Terminal…")
            self._schedule_project_terminal_poll(50)
            return True
        except Exception as exc:
            self._activate_project_terminal_fallback(
                f"Native Project Terminal could not start: {exc}"
            )
            return False

    def _schedule_project_terminal_poll(self, delay_ms=100):
        try:
            if self._project_terminal_status_job:
                self.root.after_cancel(self._project_terminal_status_job)
        except Exception:
            pass
        try:
            self._project_terminal_status_job = self.root.after(
                max(25, int(delay_ms)), self._poll_project_terminal_webview
            )
        except Exception:
            self._project_terminal_status_job = None

    def _poll_project_terminal_webview(self):
        self._project_terminal_status_job = None
        if getattr(self, "_project_terminal_fallback", False):
            return
        proc = getattr(self, "_project_terminal_proc", None)
        try:
            if proc is not None and proc.poll() is not None:
                self._activate_project_terminal_fallback(
                    "Native Project Terminal closed before it could attach."
                )
                return
        except Exception:
            pass

        port_file = getattr(self, "_project_terminal_port_file", None)
        if port_file:
            try:
                if port_file.exists():
                    data = json.loads(port_file.read_text(encoding="utf-8"))
                    if data.get("error"):
                        self._activate_project_terminal_fallback(
                            f"Native Project Terminal failed: {data['error']}"
                        )
                        return
                    if data.get("xterm") is False:
                        self._activate_project_terminal_fallback(
                            "xterm.js could not load in WebView2."
                        )
                        return
                    if not getattr(self, "_project_terminal_port", None):
                        self._project_terminal_port = int(data["port"])
            except Exception:
                pass

        if getattr(self, "_project_terminal_port", None) and not getattr(
            self, "_project_terminal_embedded", False
        ):
            self._try_embed_project_terminal_window()

        if getattr(self, "_project_terminal_embedded", False) and not getattr(
            self, "_project_terminal_page_ready", False
        ):
            try:
                if port_file and port_file.exists():
                    data = json.loads(port_file.read_text(encoding="utf-8"))
                    if data.get("ready") is True:
                        self._reveal_project_terminal()
            except Exception:
                pass

        self._project_terminal_embed_attempts = getattr(
            self, "_project_terminal_embed_attempts", 0
        ) + 1
        if self._project_terminal_embed_attempts >= 160 and not getattr(
            self, "_project_terminal_embedded", False
        ):
            self._activate_project_terminal_fallback(
                "Native Project Terminal could not attach to the main window."
            )
            return
        if not getattr(self, "_project_terminal_embedded", False):
            self._schedule_project_terminal_poll(75)
        else:
            # Keep a lightweight watchdog so a child that exits later falls
            # back cleanly instead of leaving a dead native rectangle.
            self._schedule_project_terminal_poll(750)

    def _try_embed_project_terminal_window(self):
        if win32gui is None or win32con is None:
            return False
        if getattr(self, "_project_terminal_embedded", False):
            self._resize_project_terminal()
            return True
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, "MCU Flash GUI - Project Terminal")
        except Exception:
            hwnd = 0
        if not hwnd:
            return False

        try:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            frame = self._shell_terminal_embed_frame
            frame.update_idletasks()
            tk_hwnd = frame.winfo_id()
            try:
                style = win32gui.GetWindowLong(tk_hwnd, win32con.GWL_STYLE)
                win32gui.SetWindowLong(tk_hwnd, win32con.GWL_STYLE, style | win32con.WS_CLIPCHILDREN)
            except Exception:
                pass

            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            style &= ~(win32con.WS_CAPTION | win32con.WS_THICKFRAME |
                       win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX |
                       win32con.WS_SYSMENU | win32con.WS_POPUP | win32con.WS_BORDER)
            style |= win32con.WS_CHILD
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)

            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex_style &= ~(win32con.WS_EX_DLGMODALFRAME | win32con.WS_EX_APPWINDOW |
                          win32con.WS_EX_WINDOWEDGE | win32con.WS_EX_CLIENTEDGE)
            ex_style |= win32con.WS_EX_TOOLWINDOW
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)
            win32gui.SetParent(hwnd, tk_hwnd)

            self._project_terminal_hwnd = hwnd
            self._project_terminal_embedded = True
            self._project_terminal_launching = False
            self._project_terminal_page_ready = False
            self._shell_terminal_fallback_frame.pack_forget()
            self._shell_terminal_embed_frame.pack(fill=tk.BOTH, expand=True)
            # Keep the native surface hidden until the child reports that
            # xterm.js and the selected shell prompt are actually ready.
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            self._resize_project_terminal(force=True)
            self._shell_set_status(
                f"Loading terminal • {self._shell_current_target()}"
            )
            pending = getattr(self, "_project_terminal_pending_action", None)
            self._project_terminal_pending_action = None
            if pending:
                self._project_terminal_send_control(*pending)
            else:
                self._project_terminal_send_control("select", self._shell_active_kind)
            return True
        except Exception:
            self._project_terminal_embedded = False
            self._project_terminal_hwnd = None
            return False

    def _reveal_project_terminal(self):
        if not getattr(self, "_project_terminal_embedded", False):
            return
        if getattr(self, "_project_terminal_page_ready", False):
            return
        hwnd = getattr(self, "_project_terminal_hwnd", None)
        if not hwnd or win32gui is None or win32con is None:
            return
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            self._shell_terminal_placeholder.place_forget()
            self._project_terminal_page_ready = True
            self._resize_project_terminal(force=True)
            self._shell_set_status(
                f"Running • {self._shell_current_target()}"
            )
        except Exception:
            pass

    def _resize_project_terminal(self, event=None, force=False):
        if not getattr(self, "_project_terminal_embedded", False):
            return
        hwnd = getattr(self, "_project_terminal_hwnd", None)
        if not hwnd or win32gui is None or win32con is None:
            return
        try:
            frame = self._shell_terminal_embed_frame
            width = max(frame.winfo_width(), 20)
            height = max(frame.winfo_height(), 20)
            win32gui.SetWindowPos(
                hwnd, 0, 0, 0, width, height,
                win32con.SWP_FRAMECHANGED | win32con.SWP_NOZORDER |
                win32con.SWP_NOACTIVATE | 0x4000,
            )
        except Exception:
            pass

    def _project_terminal_send_control(self, action: str, kind: str):
        if getattr(self, "_project_terminal_fallback", False):
            return
        port = getattr(self, "_project_terminal_port", None)
        if not port:
            self._project_terminal_pending_action = (action, kind)
            return

        payload = json.dumps({"action": action, "shell": kind}).encode("utf-8")
        url = f"http://127.0.0.1:{int(port)}/control"

        def _send():
            try:
                from urllib.request import Request, urlopen
                request = Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=1.5) as response:
                    result = json.loads(response.read().decode("utf-8", errors="replace"))
                if result.get("success"):
                    self._post_ui(
                        lambda: self._shell_set_status(
                            f"Running • {self._shell_current_target()}"
                        )
                    )
            except Exception:
                # A short-lived control race is normal while WebView2 is
                # attaching. The next selection/restart sends a fresh command.
                pass

        threading.Thread(target=_send, name="MCUTerminalControl", daemon=True).start()

    def _activate_project_terminal_fallback(self, reason: str = ""):
        if getattr(self, "_project_terminal_fallback", False):
            return
        self._project_terminal_fallback = True
        self._project_terminal_fallback_revealed = False
        self._project_terminal_launching = False
        hwnd = getattr(self, "_project_terminal_hwnd", None)
        if hwnd and win32gui is not None:
            try:
                win32gui.SetParent(hwnd, 0)
                if win32con is not None:
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            except Exception:
                pass
        self._project_terminal_hwnd = None
        self._project_terminal_embedded = False
        self._project_terminal_page_ready = False
        self._terminate_project_terminal_process()
        try:
            self._shell_terminal_fallback_frame.pack_forget()
            self._shell_terminal_embed_frame.pack(fill=tk.BOTH, expand=True)
            self._shell_terminal_placeholder.configure(
                text="Starting compatible PowerShell and Command Prompt…"
            )
            self._shell_terminal_placeholder.place(
                relx=0.5, rely=0.5, anchor=tk.CENTER
            )
        except Exception:
            pass
        if reason:
            self._append(f"  ⚠ {reason} Using the compatibility terminal surface.", "warning")
        for kind in ("pwsh", "cmd"):
            try:
                self._shell_start(kind)
            except Exception:
                pass
        self._shell_select(self._shell_active_kind)

    def _reveal_project_terminal_fallback(self):
        """Show the compatibility surface only after a complete shell prompt."""
        if not getattr(self, "_project_terminal_fallback", False):
            return
        if getattr(self, "_project_terminal_fallback_revealed", False):
            return
        self._project_terminal_fallback_revealed = True
        try:
            self._shell_terminal_embed_frame.pack_forget()
            self._shell_terminal_fallback_frame.pack(fill=tk.BOTH, expand=True)
            self._shell_terminal_placeholder.place_forget()
            self._shell_render_session(self._shell_active_kind)
        except Exception:
            pass

    def _terminate_project_terminal_process(self):
        proc = getattr(self, "_project_terminal_proc", None)
        self._project_terminal_proc = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    if sys.platform == "win32":
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    else:
                        proc.terminate()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        port_file = getattr(self, "_project_terminal_port_file", None)
        self._project_terminal_port_file = None
        self._project_terminal_port = None
        if port_file:
            try:
                Path(port_file).unlink(missing_ok=True)
            except Exception:
                pass

    def _dispose_project_terminal(self):
        """Hide and terminate the isolated native Project Terminal child."""
        job = getattr(self, "_project_terminal_status_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
            self._project_terminal_status_job = None
        hwnd = getattr(self, "_project_terminal_hwnd", None)
        if hwnd and win32gui is not None:
            try:
                win32gui.SetParent(hwnd, 0)
                if win32con is not None:
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            except Exception:
                pass
        self._project_terminal_hwnd = None
        self._project_terminal_embedded = False
        self._project_terminal_launching = False
        self._project_terminal_fallback = False
        self._project_terminal_fallback_revealed = False
        self._project_terminal_page_ready = False
        self._terminate_project_terminal_process()
        try:
            self._shell_terminal_fallback_frame.pack_forget()
            self._shell_terminal_embed_frame.pack(fill=tk.BOTH, expand=True)
            self._shell_terminal_placeholder.place(
                relx=0.5, rely=0.5, anchor=tk.CENTER
            )
        except Exception:
            pass

    def _schedule_shell_prewarm(self):
        """Schedule legacy fallback prewarm only when native terminal fails.

        The normal xterm/WebView2 terminal is created lazily when its tab is
        selected. Keeping this hook for the Tk fallback preserves recovery on
        machines without WebView2 while avoiding two hidden shells at startup.
        """
        if getattr(self, "_shells_prewarmed", False):
            return
        if getattr(self, "_shell_prewarm_after_id", None) is not None:
            return
        try:
            self._shell_prewarm_after_id = self.root.after(
                650, self._prewarm_project_terminals
            )
        except Exception:
            self._shell_prewarm_after_id = None

    def _prewarm_project_terminals(self):
        self._shell_prewarm_after_id = None
        if getattr(self, "_shells_prewarmed", False):
            return
        if not getattr(self, "_startup_ready", False):
            return
        # The native terminal is intentionally lazy. Starting two extra
        # Windows shells during app startup was the source of repeated loading
        # and unnecessary CPU use on low-end devices. The selected shell is
        # created by xterm/WebView2 when the Terminal tab is actually opened.
        if not getattr(self, "_project_terminal_fallback", False):
            self._shells_prewarmed = True
            return
        self._shells_prewarmed = True
        for kind in ("pwsh", "cmd"):
            try:
                self._shell_start(kind)
            except Exception:
                pass
        try:
            self._shell_set_status("Shells ready • click PowerShell or Command Prompt to switch")
        except Exception:
            pass

    def _shell_start(self, kind: str):
        """Create one independent PTY worker; never run shell startup on Tk."""
        target = str(self._shell_current_target())
        with self._shell_state_lock:
            existing = self._shell_sessions.get(kind)
            if existing and existing.get("running"):
                return
            session = {
                "kind": kind,
                "target": target,
                "running": True,
                "pty": None,
                "output": "",
                "plain_output": "",
                "styled": [],
                "pending": deque(),
                "terminal": _ShellTerminalBuffer(columns=120),
            }
            self._shell_sessions[kind] = session
        self._shell_refresh_switcher()
        self._shell_set_status(f"Starting {self._shell_display_name(kind)} in {target}…")
        thread = threading.Thread(
            target=self._shell_worker, args=(session,),
            name=f"MCUShell-{kind}", daemon=True,
        )
        session["thread"] = thread
        thread.start()

    def _shell_append_output(self, session: dict, data):
        with self._shell_state_lock:
            terminal = session.setdefault("terminal", _ShellTerminalBuffer(columns=120))
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            raw_data = str(data or "")
            # A few Windows PTY paths deliver the CSI payload without the
            # leading ESC byte. Remove those orphaned SGR fragments before the
            # screen model sees them, while retaining normal terminal styling.
            data = re.sub(r"\[(?:\d{1,3}(?:;\d{1,3})*)m", "", raw_data)
            terminal.feed(data)
            text_value = terminal.render()
            session["output"] = text_value[-220000:]
            # The fallback Text widget is a transcript, not an xterm clone.
            # Rendering every VT redraw as a screen snapshot caused PowerShell
            # prompts to reflow into fragmented vertical text on some devices.
            # Preserve the visible text and strip only terminal control codes.
            visible = self._shell_clean_output(raw_data)
            plain_output = session.get("plain_output", "") + visible
            session["plain_output"] = plain_output[-220000:]
            session["styled"] = []
            pending = session.setdefault("pending", deque())
            pending.append(session["plain_output"])
            while len(pending) > 500:
                pending.popleft()

    def _shell_worker(self, session: dict):
        kind = session["kind"]
        target = session["target"]
        try:
            # pyrefly: ignore [missing-import]
            from winpty import PtyProcess
        except Exception as exc:
            self._shell_append_output(session, f"\r\n[MCU Flasher] PTY support is unavailable: {exc}\r\n")
            with self._shell_state_lock:
                session["running"] = False
            return

        executable = self._shell_executable(kind)
        if not executable:
            self._shell_append_output(
                session,
                f"\r\n[MCU Flasher] Could not find {'PowerShell' if kind == 'pwsh' else 'Command Prompt'}.\r\n",
            )
            with self._shell_state_lock:
                session["running"] = False
            return

        # Start directly in the project directory. This removes the extra
        # cd/cls round-trip that made cold shell startup feel slow.
        try:
            launch_cwd = Path(target)
            if not launch_cwd.is_dir():
                launch_cwd = Path(getattr(self, "_shell_initial_cwd", Path.cwd()))
            if not launch_cwd.is_dir():
                launch_cwd = Path.home()
        except Exception:
            launch_cwd = Path.home()
        spawn_cwd = str(launch_cwd)
        argv = (
            [executable, "-NoProfile", "-NoExit"]
            if kind == "pwsh" else [executable, "/D"]
        )
        pty = None
        try:
            pty = PtyProcess.spawn(argv, cwd=spawn_cwd, dimensions=(30, 120))
            with self._shell_state_lock:
                session["pty"] = pty

            cd_pending = False
            startup_probe = ""
            # PowerShell can take several seconds to print its first prompt on
            # a cold Windows profile.  Give the native banner plenty of time to
            # arrive; cmd.exe normally completes this path much sooner.
            startup_deadline = time.monotonic() + 30.0

            while True:
                with self._shell_state_lock:
                    if not session.get("running"):
                        break
                try:
                    data = pty.read(4096)
                except Exception:
                    break
                if data:
                    self._shell_append_output(session, data)
                    if not cd_pending and not session.get("ready"):
                        startup_probe = session.get("output", "")[-6000:]
                        if re.search(
                            r"(?m)(?:[A-Za-z]:[^\r\n]*>|PS [^\r\n]*>\s*)$",
                            startup_probe,
                        ):
                            with self._shell_state_lock:
                                session["ready"] = True
                    if cd_pending:
                        startup_probe = session.get("output", "")[-6000:]
                        prompt_seen = bool(
                            re.search(r"(?m)(?:[A-Za-z]:[^\r\n]*>|PS [^\r\n]*>\s*)$", startup_probe)
                        )
                        if prompt_seen or time.monotonic() >= startup_deadline:
                            try:
                                # Keep the navigation visible, then perform one
                                # native clear so the session settles on a
                                # clean project prompt:
                                # ``PS D:\project> cls`` / ``D:\project>cls``.
                                pty.write(self._shell_cd_command(kind, target) + "cls\r\n")
                            except Exception:
                                pass
                            cd_pending = False
                            with self._shell_state_lock:
                                session["ready"] = True
                elif cd_pending and time.monotonic() >= startup_deadline:
                    try:
                        pty.write(self._shell_cd_command(kind, target) + "cls\r\n")
                    except Exception:
                        pass
                    cd_pending = False
                    with self._shell_state_lock:
                        session["ready"] = True
        except Exception as exc:
            self._shell_append_output(session, f"\r\n[MCU Flasher] Could not start {kind}: {exc}\r\n")
        finally:
            with self._shell_state_lock:
                session["running"] = False
                if session.get("pty") is pty:
                    session["pty"] = None
            try:
                if pty:
                    pty.close(force=True)
            except Exception:
                pass

    def _shell_set_status(self, text_value: str):
        try:
            self.lbl_shell_terminal_status.configure(text=text_value)
        except Exception:
            pass

    def _shell_insert_snapshot(self, snapshot, fallback=""):
        """Paint one xterm-style snapshot without losing ANSI color runs."""
        self._shell_selected_range = None
        if isinstance(snapshot, str):
            if snapshot:
                self.shell_console.insert(tk.END, snapshot)
            return
        if not snapshot:
            if fallback:
                self.shell_console.insert(tk.END, fallback)
            return
        for row_index, runs in enumerate(snapshot):
            for text_value, tag in runs:
                if text_value:
                    self.shell_console.insert(tk.END, text_value, tag)
            if row_index < len(snapshot) - 1:
                self.shell_console.insert(tk.END, "\n")

    def _shell_render_session(self, kind: str):
        with self._shell_state_lock:
            session = self._shell_sessions.get(kind)
            output = session.get("plain_output", session.get("output", "")) if session else ""
            styled = []
            running = bool(session and session.get("running"))
            target = session.get("target", "") if session else str(self._shell_current_target())
        try:
            self._shell_selected_range = None
            self.shell_console.configure(state=tk.NORMAL)
            self.shell_console.delete("1.0", tk.END)
            if styled or output:
                self._shell_insert_snapshot(styled, output)
                self.shell_console.see(tk.END)
            self.shell_console.configure(state=tk.NORMAL)
            self._shell_set_status(
                f"{'Running' if running else 'Stopped'} • {target}"
            )
        except Exception:
            pass

    def _shell_output_pump(self):
        """Render bounded PTY batches on Tk, never from a shell thread."""
        self._shell_terminal_pump_after_id = None
        if not getattr(self, "_project_terminal_fallback", False):
            try:
                if self.root and self.root.winfo_exists():
                    self._shell_terminal_pump_after_id = self.root.after(
                        250, self._shell_output_pump
                    )
            except Exception:
                self._shell_terminal_pump_after_id = None
            return
        try:
            active = self._shell_active_kind
            active_snapshot = None
            with self._shell_state_lock:
                for session in self._shell_sessions.values():
                    pending = session.setdefault("pending", deque())
                    if session.get("kind") == active:
                        while pending:
                            active_snapshot = pending.popleft()
                    else:
                        pending.clear()
                current = self._shell_sessions.get(active)
                running = bool(current and current.get("running"))
                target = current.get("target", "") if current else ""
                current_ready = bool(current and current.get("ready"))
                current_failed = bool(
                    current
                    and not current.get("running")
                    and "[MCU Flasher]" in str(current.get("output", ""))
                )
            if current_ready or current_failed:
                self._reveal_project_terminal_fallback()
            if (
                active_snapshot is not None
                and getattr(self, "_project_terminal_fallback_revealed", False)
                and self._shell_terminal_is_selected()
            ):
                self.shell_console.configure(state=tk.NORMAL)
                self.shell_console.delete("1.0", tk.END)
                self._shell_insert_snapshot(active_snapshot)
                # Keep Tk's widget itself bounded in addition to the session
                # buffer, so long-lived shells cannot grow the UI indefinitely.
                try:
                    line_count = int(self.shell_console.index(tk.END).split(".")[0])
                    if line_count > 5000:
                        self.shell_console.delete("1.0", f"{line_count - 4500}.0")
                except Exception:
                    pass
                self.shell_console.see(tk.END)
                self.shell_console.configure(state=tk.NORMAL)
            if self._shell_terminal_is_selected() and current:
                self._shell_set_status(f"{'Running' if running else 'Stopped'} • {target}")
            self._shell_refresh_switcher()
        except Exception:
            pass
        try:
            if self.root and self.root.winfo_exists():
                self._shell_terminal_pump_after_id = self.root.after(40, self._shell_output_pump)
        except Exception:
            self._shell_terminal_pump_after_id = None

    def _shell_write_input(self, payload: str) -> bool:
        """Write keystrokes directly to the active shell's PTY."""
        if not payload:
            return False
        payload = re.sub(
            r"\x1b(?:\[\?[0-9;]*c|\[>[0-9;]*c|\[\?[0-9;]*\$y|\[[0-9;]*\$y|\]\d+;[^\x1b\x07]*(?:\x1b\\|\x07)?|P>\|[^\x1b\x07]*(?:\x1b\\|\x07)?|\[>[0-9;]*q)",
            "",
            str(payload),
        )
        if not payload:
            return False
        with self._shell_state_lock:
            session = self._shell_sessions.get(self._shell_active_kind)
            pty = session.get("pty") if session else None
            running = bool(session and session.get("running"))
        if not running or pty is None:
            self._shell_select(self._shell_active_kind)
            return False
        try:
            pty.write(payload)
            return True
        except Exception as exc:
            if session:
                self._shell_append_output(session, f"\r\n[MCU Flasher] Shell input failed: {exc}\r\n")
            return False

    def _shell_console_key(self, event):
        """Translate Tk keystrokes into native console/PTY input."""
        keysym = str(getattr(event, "keysym", "") or "")
        state = int(getattr(event, "state", 0) or 0)
        ctrl = bool(state & 0x0004)
        shift = bool(state & 0x0001)
        key_lower = keysym.lower()

        control_keys = {
            "a": "\x01", "b": "\x02", "c": "\x03", "d": "\x04",
            "e": "\x05", "f": "\x06", "h": "\x08", "k": "\x0b",
            "l": "\x0c", "n": "\x0e", "p": "\x10", "r": "\x12",
            "t": "\x14", "u": "\x15", "w": "\x17", "x": "\x18",
            "y": "\x19", "z": "\x1a",
        }
        special_keys = {
            "return": "\r", "kp_enter": "\r", "backspace": "\x08",
            "tab": "\x1b[Z" if shift else "\t", "escape": "\x1b",
            "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C",
            "left": "\x1b[D", "home": "\x1b[H", "end": "\x1b[F",
            "delete": "\x1b[3~", "insert": "\x1b[2~",
            "prior": "\x1b[5~", "next": "\x1b[6~",
        }

        if ctrl and key_lower in control_keys:
            payload = control_keys[key_lower]
        elif key_lower in special_keys:
            payload = special_keys[key_lower]
        else:
            payload = str(getattr(event, "char", "") or "")
            if ctrl:
                payload = ""

        if payload:
            self._shell_write_input(payload)
        return "break"

    def _shell_console_backspace(self, _event=None):
        """Send one native Backspace keystroke to the active shell only."""
        self._shell_write_input("\x08")
        return "break"

    def _shell_console_delete(self, _event=None):
        """Send one native Delete keystroke to the active shell only."""
        self._shell_write_input("\x1b[3~")
        return "break"

    def _shell_console_copy_or_interrupt(self, _event=None):
        """Use Windows Terminal semantics: copy a selection, else send ^C."""
        try:
            if getattr(self, "_shell_selected_range", None) or self.shell_console.tag_ranges("sel"):
                return self._shell_console_copy(_event)
        except Exception:
            pass
        self._shell_write_input("\x03")
        return "break"

    def _shell_console_paste(self, _event=None):
        try:
            value = self.root.clipboard_get()
        except Exception:
            value = ""
        if value:
            # A console Enter is CR; preserve pasted line breaks as console
            # submits instead of sending bare LF characters.
            value = str(value).replace("\r\n", "\r").replace("\n", "\r")
            self._shell_write_input(value)
        return "break"

    def _shell_console_copy(self, _event=None):
        try:
            selected_range = getattr(self, "_shell_selected_range", None)
            if selected_range:
                selected = self.shell_console.get(*selected_range)
            else:
                selected = self.shell_console.get("sel.first", "sel.last")
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
        except Exception:
            pass
        return "break"

    def _shell_clear_output(self):
        with self._shell_state_lock:
            session = self._shell_sessions.get(self._shell_active_kind)
            if session:
                session["output"] = ""
                session["plain_output"] = ""
                session["styled"] = []
                session.setdefault("pending", deque()).clear()
                session["terminal"] = _ShellTerminalBuffer(columns=120)
        try:
            self._shell_selected_range = None
            self.shell_console.configure(state=tk.NORMAL)
            self.shell_console.delete("1.0", tk.END)
            self.shell_console.configure(state=tk.NORMAL)
        except Exception:
            pass

    def _shell_stop(self, kind: str):
        with self._shell_state_lock:
            session = self._shell_sessions.get(kind)
            if not session:
                return
            session["running"] = False
            pty = session.get("pty")
        try:
            if pty:
                pty.close(force=True)
        except Exception:
            pass
        self._shell_refresh_switcher()

    def _shell_restart(self, kind: str | None = None):
        """Restart only the selected Project Terminal shell."""
        kind = kind or getattr(self, "_shell_active_kind", "pwsh")
        if kind not in ("pwsh", "cmd"):
            return "break"

        if not getattr(self, "_project_terminal_fallback", False):
            self._shell_active_kind = kind
            self._ensure_project_terminal_webview()
            if not getattr(self, "_project_terminal_fallback", False):
                self._shell_set_status(f"Restarting {self._shell_display_name(kind)}…")
                self._project_terminal_send_control("restart", kind)
                return "break"

        # Restart is intentionally scoped to one PTY.  The other shell keeps
        # its process, scrollback, and current command untouched.
        self._shell_stop(kind)
        if kind == getattr(self, "_shell_active_kind", "pwsh"):
            self._shell_clear_output()
        self._shell_set_status(f"Restarting {self._shell_display_name(kind)}…")
        self._shell_start(kind)
        self._shell_render_session(kind)
        try:
            self.shell_console.focus_set()
        except Exception:
            pass
        return "break"

    def _shell_stop_all(self):
        for kind in ("pwsh", "cmd"):
            try:
                self._shell_stop(kind)
            except Exception:
                pass

    def _on_bottom_notebook_tab_changed(self, _event=None):
        # This handler runs on Tk's thread, so it is the authoritative place to
        # publish whether serial rendering should be active.  The serial reader
        # itself never queries Tk widgets.
        serial_selected = self._serial_monitor_is_selected()
        self._serial_tab_visible = serial_selected
        if serial_selected:
            try:
                self.root.after_idle(self._flush_tagged_serial_lines)
            except Exception:
                pass

        if self._compatible_devices_is_selected():
            try:
                self.root.after_idle(self._repair_compatible_devices_interaction)
            except Exception:
                pass

        if self._shell_terminal_is_selected():
            try:
                self.root.after_idle(lambda: self._shell_select(self._shell_active_kind))
            except Exception:
                pass

    def _on_bottom_notebook_click_release(self, _event=None):
        """A real tab click should also reclaim focus from embedded native views."""
        def _after_click():
            if self._compatible_devices_is_selected():
                self._focus_compatible_search(select_all=False, select_tab=False)
        try:
            self.root.after_idle(_after_click)
        except Exception:
            pass

