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

class WindowLifecycleMixin(_Base):
    """Mixin providing WindowLifecycleMixin capabilities for MCUUploadGUI."""
    def _on_close(self):
        self._dispose_active_ai_assistant()
        if getattr(self, "_framework_download_active", False):
            from tkinter import messagebox
            messagebox.showwarning(
                "Framework Download in Progress",
                "A critical framework/tool download is currently in progress. "
                "Closing the application now may corrupt your PlatformIO core installation.\n\n"
                "Please wait for the download to finish.",
                parent=self.root
            )
            return

        if self.is_busy:
            # Auto-recover: if no subprocess is actually running, clear stale busy flag
            if not self.process or self.process.poll() is not None:
                self.is_busy = False
                self._set_buttons_state(False)
                self._set_window_closable(True)
                # Fall through to normal close logic
            else:
                phase = getattr(self, "_current_op_phase", None)
                kind = getattr(self, "_active_reset_kind", None)

                # Allow closing if we are currently ONLY in the compilation phase!
                if phase == "compiling":
                    try:
                        self._do_stop()
                    except Exception:
                        pass
                    # Proceed to normal exit below
                else:
                    from tkinter import messagebox
                    if kind == "hard":
                        msg = ("A Hard Reset (bootloader burn) is in progress.\n\n"
                               "Interrupting this can permanently brick the board. "
                               "Please wait for it to finish.")
                    elif kind == "soft":
                        msg = ("A Soft Reset (flash rewrite) is in progress.\n\n"
                               "Interrupting this can leave the board in a broken state. "
                               "Please wait for it to finish.")
                    else:
                        msg = ("A Flash Upload is currently writing to the MCU memory.\n\n"
                               "Interrupting this direct flash write can leave your board corrupted. "
                               "Please wait for the upload to complete or click Stop.")
                    messagebox.showwarning("Flash Upload / Reset in Progress", msg, parent=self.root)
                    return

        # Check if there are unsaved changes in the active editor
        if getattr(self, "editor_mode", "default") == "monaco":
            if hasattr(self, "editor_api") and self.editor_api.modified_files:
                unsaved = [Path(p).name for p, is_modified in self.editor_api.modified_files.items() if is_modified]
                if unsaved:
                    import tkinter.messagebox as mb
                    names = "\n  • ".join(unsaved)
                    answer = mb.askyesnocancel(
                        "Unsaved Changes",
                        f"The following files have unsaved changes:\n\n  • {names}\n\n"
                        "Save before closing?",
                        parent=self.root,
                    )
                    if answer is None:       # Cancel closing
                        return
                    if answer:               # Yes — save all
                        if hasattr(self, "_save_all_editor_files"):
                            self._save_all_editor_files()
        else:
            tab_data = getattr(self, "editor_tab_data", None)
            if tab_data:
                unsaved = [d["path"].name for d in tab_data.values() if d.get("modified")]
                if unsaved:
                    import tkinter.messagebox as mb
                    names = "\n  • ".join(unsaved)
                    answer = mb.askyesnocancel(
                        "Unsaved Changes",
                        f"The following files have unsaved changes:\n\n  • {names}\n\n"
                        "Save before closing?",
                        parent=self.root,
                    )
                    if answer is None:       # Cancel closing
                        return
                    if answer:               # Yes — save all
                        if hasattr(self, "_save_all_editor_files"):
                            self._save_all_editor_files()

        try:
            if getattr(self, "_shell_prewarm_after_id", None) is not None:
                self.root.after_cancel(self._shell_prewarm_after_id)
                self._shell_prewarm_after_id = None
            if getattr(self, "_shell_terminal_pump_after_id", None) is not None:
                self.root.after_cancel(self._shell_terminal_pump_after_id)
                self._shell_terminal_pump_after_id = None
            self._shell_stop_all()
        except Exception:
            pass
        try:
            self._dispose_project_terminal()
        except Exception:
            pass

        # If editor was detached, dispose/close that window first before destroying main GUI
        try:
            self._close_detached_editor_window()
        except Exception:
            pass

        # Kill any active compile/upload/reset subprocess synchronously on exit
        if self.process and self.process.poll() is None:
            try:
                if sys.platform == "win32":
                    import subprocess
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                else:
                    self.process.kill()
            except Exception:
                pass

        # Kill any launched library manager subprocesses
        for p in getattr(self, "_download_managers", []):
            if p.poll() is None:
                try:
                    if sys.platform == "win32":
                        import subprocess
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                    else:
                        p.kill()
                except Exception:
                    pass

        # Signal any sleeping Download Manager process to exit permanently.
        # The DM uses "Sleep Mode" (hides instead of exiting on close) to keep
        # indexes cached in RAM.  A trigger file tells its poll loop to call
        # _force_exit() instead of staying alive as an orphan.
        try:
            dm_exit_trigger = SCRIPT_DIR / "index_json" / ".dm_force_exit"
            dm_exit_trigger.parent.mkdir(parents=True, exist_ok=True)
            dm_exit_trigger.write_text("exit", encoding="utf-8")
        except Exception:
            pass

        self._do_stop()
        self._syntax_bg_active = False
        if hasattr(self, "_monaco_autosave_worker"):
            self._monaco_autosave_worker.stop()
        if hasattr(self, "editor_api") and self.editor_api:
            try:
                self.editor_api.shutdown_ai_edit_backup()
            except Exception:
                pass
        if hasattr(self, "_bg_executor") and self._bg_executor:
            try:
                self._bg_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        # Stop serial monitor on close without blocking Tk on thread.join().
        self._monitor_should_run = False
        self._stop_serial_session()

        # Clean up this instance configuration from the shared file
        try:
            data = _load_raw_config()
            if "instances" in data and _INSTANCE_ID in data["instances"]:
                del data["instances"][_INSTANCE_ID]
                _save_raw_config(data)
        except Exception:
            pass

        # Detach Win32 thread input if attached
        try:
            pair = getattr(self, "_editor_attached_threads", None)
            if pair:
                import ctypes
                ctypes.windll.user32.AttachThreadInput(pair[0], pair[1], False)
        except Exception:
            pass

        # Hide all generated internal project files and MCU-FLASHER-SRC directory on close
        try:
            if hasattr(self, "sketch_dir_path") and self.sketch_dir_path:
                if not is_unc_or_network_path(self.sketch_dir_path):
                    hide_internal_project_metadata(self.sketch_dir_path)
        except Exception:
            pass

        try:
            set_monaco_boot_pending(False)
        except Exception:
            pass
        self.root.destroy()

        # -- Final safety net: kill the entire process tree --
        # Even after individually stopping every tracked subprocess, an orphan
        # child (Download Manager in Sleep Mode, AI subprocess, shell process)
        # can keep a `python.exe` entry visible in Task Manager.  Killing our
        # own PID with /T (tree) sweeps every remaining descendant.  This is
        # safe because we are about to os._exit(0) anyway.
        try:
            _own_pid = os.getpid()
            subprocess.Popen(
                ["taskkill", "/F", "/T", "/PID", str(_own_pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
        os._exit(0)

    def _set_window_closable(self, closable: bool):
        """Grey out (or restore) the window's native [X] close button and
        Alt+F4 at the OS level. This sits on top of the existing is_busy
        check in _on_close() as a stronger guarantee — Hard/Soft Reset
        write directly to flash and, for Hard Reset, the bootloader itself;
        an interrupted write there can brick the board or leave it in an
        unrecoverable boot loop, so during that window we don't want the
        close path reachable at all, not just intercepted-and-warned.
        """
        if win32gui is None or win32con is None:
            return  # non-Windows or pywin32 missing — the is_busy dialog in _on_close is still the backstop
        try:
            hwnd = self.root.winfo_id()
            menu = win32gui.GetSystemMenu(hwnd, False)
            flag = win32con.MF_ENABLED if closable else win32con.MF_GRAYED
            win32gui.EnableMenuItem(menu, win32con.SC_CLOSE, win32con.MF_BYCOMMAND | flag)
        except Exception:
            pass

