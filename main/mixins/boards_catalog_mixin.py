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

class BoardsCatalogMixin(_Base):
    """Mixin providing BoardsCatalogMixin capabilities for MCUUploadGUI."""
    def _open_download_manager(self):
        import subprocess, sys, os
        from pathlib import Path

        self._append_notif("  ℹ Launching Download Boards/Libraries Manager...", "info")

        parent_hwnd = None
        if sys.platform == "win32":
            try:
                parent_hwnd = self.root.winfo_id()
            except Exception:
                parent_hwnd = None

        def _bg_launch_worker():
            script_dir = SCRIPT_DIR
            script_path = script_dir / "src" / "modules" / "arduino_lib_req.py"

            # Check if a persistent Download Manager process is already sleeping in background
            active_proc = None
            for p in getattr(self, "_download_managers", []):
                if p.poll() is None:
                    active_proc = p
                    break

            if active_proc:
                # Send wake-up trigger file + Win32 HWND restore for instant unhide
                trigger_file = script_dir / "index_json" / ".show_dm_trigger"
                try:
                    trigger_file.parent.mkdir(parents=True, exist_ok=True)
                    trigger_file.write_text("show", encoding="utf-8")
                except Exception:
                    pass

                hwnd_file = script_dir / "index_json" / ".dm_hwnd"
                if sys.platform == "win32" and hwnd_file.exists():
                    try:
                        dm_hwnd = int(hwnd_file.read_text(encoding="utf-8").strip())
                        import ctypes
                        ctypes.windll.user32.ShowWindow(dm_hwnd, 9)  # SW_RESTORE / SW_SHOW
                        ctypes.windll.user32.SetForegroundWindow(dm_hwnd)
                    except Exception:
                        pass

                def _on_restored():
                    self._append_notif("  ⚡ Restored Download Manager instantly from memory (Sleep Mode).", "info")

                try:
                    self.root.after(0, _on_restored)
                except Exception:
                    pass
                return

            env = os.environ.copy()
            env["MCU_PREF_DIR"] = str(SCRIPT_DIR)
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            for k in ["_MEIPASS", "_MEIPASS2", "PYTHONHOME", "PYTHONPATH"]:
                env.pop(k, None)

            meipass = getattr(sys, '_MEIPASS', None)
            if meipass:
                path_val = env.get("PATH", "")
                paths = path_val.split(os.pathsep)
                cleaned_paths = [p for p in paths if p != meipass]
                env["PATH"] = os.pathsep.join(cleaned_paths)

            try:
                python_exe = None
                # Priority 1: Bundled private Python runtime
                # Priority 2: Virtualenv runtime
                # Priority 3: sys.executable (if not frozen)
                # Priority 4: System PATH python
                candidates = [
                    SCRIPT_DIR / "src" / "_python" / "pythonw.exe",
                    SCRIPT_DIR / "src" / "_python" / "python.exe",
                    SCRIPT_DIR / "env" / "Scripts" / "pythonw.exe",
                    SCRIPT_DIR / "env" / "Scripts" / "python.exe",
                ]
                for c in candidates:
                    if c.is_file():
                        python_exe = c
                        break

                if not python_exe and not getattr(sys, 'frozen', False):
                    p_exe = Path(sys.executable)
                    if sys.platform == "win32":
                        pythonw = p_exe.parent / "pythonw.exe"
                        python_exe = pythonw if pythonw.is_file() else p_exe
                    else:
                        python_exe = p_exe

                if not python_exe:
                    for name in ("pythonw", "python", "py"):
                        which = shutil.which(name)
                        if which:
                            python_exe = Path(which)
                            break

                if not python_exe:
                    python_exe = Path(sys.executable)

                cmd = [str(python_exe), str(script_path)]
                p = subprocess.Popen(cmd, env=env)

                def _on_launched():
                    self._download_managers.append(p)
                    self.root.after(1000, self._check_downloader_running)
                    self._append_notif("  ✔ Download Boards/Libraries Manager process ready.", "success")

                try:
                    self.root.after(0, _on_launched)
                except Exception:
                    pass
            except Exception as e:
                def _on_err(err_str=str(e)):
                    self._append_notif(f"  ✖ Failed to launch download manager: {err_str}", "error")

                try:
                    self.root.after(0, _on_err)
                except Exception:
                    pass

        import threading
        threading.Thread(target=_bg_launch_worker, daemon=True).start()

    def _check_downloader_running(self):
        # Filter completed processes
        still_running = []
        any_finished = False
        for p in self._download_managers:
            if p.poll() is None:
                still_running.append(p)
            else:
                any_finished = True
        
        self._download_managers = still_running
        
        if any_finished:
            self._reload_supported_boards()
        
        # While the download manager is still open, periodically check
        # for filesystem changes (new board installed / deleted) so the
        # board dropdown updates live without waiting for the manager to
        # close.
        if self._download_managers: 
            self._check_boards_dir_changed()
            self.root.after(2000, self._check_downloader_running)

    def _check_boards_dir_changed(self):
        """Detect changes in the Boards download directory and reload if needed.

        Watches both the download directory path itself (in case the user
        changed it in the Download Manager) and the actual boards.txt
        files on disk.
        """
        try:
            download_dir = _get_download_dir()

            boards_path = Path(download_dir) / "Boards"
            if boards_path.is_dir():
                # Build a snapshot of board folder names + boards.txt mtimes
                current_snapshot = {("__download_dir__", download_dir)}
                for p in boards_path.glob("**/boards.txt"):
                    try:
                        current_snapshot.add((str(p), os.path.getmtime(p)))
                    except OSError:
                        current_snapshot.add((str(p), 0))
            else:
                current_snapshot = {("__download_dir__", download_dir)}

            prev = getattr(self, "_boards_dir_snapshot", None)
            if prev is None:
                # First check — store baseline, no reload needed
                self._boards_dir_snapshot = current_snapshot
            elif current_snapshot != prev:
                # Something changed on disk or the directory moved — reload boards
                self._boards_dir_snapshot = current_snapshot
                self._reload_supported_boards()
        except Exception:
            pass

    def _reload_supported_boards(self):
        """Reload the dynamic boards from disk and refresh the dropdown list.
        Runs the heavy disk scan in a background thread to keep the GUI
        responsive, then applies the result on the main thread."""
        def _bg_load():
            try:
                new_boards = load_dynamic_boards({})
                new_usb_ids = load_downloaded_board_usb_ids(new_boards)
            except Exception as exc:
                self._post_ui(
                    lambda error=str(exc): self._append_notif(
                        f"  ⚠ Board catalog refresh skipped: {error}", "warning"
                    )
                )
                return
            # Route completion through the bounded UI queue instead of calling
            # Tk directly from the worker thread. This avoids cross-thread Tcl
            # stalls on Windows while keeping the scan off the UI thread.
            self._post_ui(
                lambda boards=new_boards, usb_ids=new_usb_ids:
                    self._apply_reloaded_boards(boards, usb_ids)
            )
        threading.Thread(target=_bg_load, daemon=True).start()

    def _apply_reloaded_boards(self, new_boards: dict, new_usb_ids=None):
        """Apply the reloaded board list on the main (UI) thread."""
        global SUPPORTED_BOARDS, DOWNLOADED_BOARD_USB_IDS
        SUPPORTED_BOARDS = new_boards
        if new_usb_ids is not None:
            DOWNLOADED_BOARD_USB_IDS = new_usb_ids
        
        old_boards = getattr(self, "_known_board_names", None)
        new_board_names = set(new_boards.keys())
        if old_boards is not None:
            added_boards = new_board_names - old_boards
            for board_name in added_boards:
                self._append_notif(
                    f"  📦 New Board Installed: \"{board_name}\"",
                    tag="success",
                    category="board_install",
                    title="Board Package Installed"
                )
        self._known_board_names = new_board_names

        if hasattr(self, 'board_combo') and self.board_combo:
            # Update the combobox's underlying option list
            if hasattr(self.board_combo, 'update_options'):
                self.board_combo.update_options(list(SUPPORTED_BOARDS.keys()))
            else:
                self.board_combo["values"] = list(SUPPORTED_BOARDS.keys())
            
            # If the current selected board is no longer in SUPPORTED_BOARDS
            curr = self.board_var.get()
            if curr not in SUPPORTED_BOARDS and SUPPORTED_BOARDS:
                new_board = next((b for b in SUPPORTED_BOARDS if b.lower() == "arduino uno"), next(iter(SUPPORTED_BOARDS.keys())))
                self.board_var.set(new_board)
                self._last_valid_board = new_board
                self._on_board_changed()
            elif not curr and SUPPORTED_BOARDS:
                new_board = next((b for b in SUPPORTED_BOARDS if b.lower() == "arduino uno"), next(iter(SUPPORTED_BOARDS.keys())))
                self.board_var.set(new_board)
                self._last_valid_board = new_board
                self._on_board_changed()
                
            self._append("  ℹ Reloaded supported boards list from disk.", "info")

    def _get_cpu_cores_jobs(self) -> int:
        try:
            data = _load_raw_config()
            setting = data.get("shared", {}).get("cpu_multithreading", "HIGH")
        except Exception:
            setting = "HIGH"
        
        return _resource_safe_worker_count(setting)

    def _is_internet_available(self, timeout: float = 2.0) -> bool:
        """Fast socket check for active internet connection."""
        import socket
        test_targets = [
            ("1.1.1.1", 53),
            ("8.8.8.8", 53),
            ("google.com", 80),
        ]
        for host, port in test_targets:
            try:
                sock = socket.create_connection((host, port), timeout=timeout)
                sock.close()
                return True
            except Exception:
                continue
        return False

