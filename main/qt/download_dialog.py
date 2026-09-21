#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.download_dialog — Download Manager launcher and dialog for MCU Flasher by Naph.

Launches and manages the standalone Arduino Boards & Libraries Manager (arduino_lib_req.py)
as a responsive background process with instant restore from sleep mode.
"""
from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path
from typing import Optional

# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import QWidget, QMessageBox
from main.qt.signals import signals as sig_bus

_this_file = Path(__file__).resolve()
_project_root = _this_file.parent.parent.parent


def _find_python_executable() -> Path:
    """Strictly return the private Python executable in src/_python. No fallbacks."""
    from src.modules.private_python_guard import get_private_python_exe
    return get_private_python_exe(prefer_pythonw=True)


def launch_download_manager(parent: Optional[QWidget] = None) -> bool:
    """
    Launch or restore the Arduino Boards & Libraries Manager.
    Returns True if launched or restored successfully.
    """
    script_path = _project_root / "src" / "modules" / "arduino_lib_req.py"
    if not script_path.exists():
        if parent:
            QMessageBox.critical(
                parent,
                "Download Manager Missing",
                f"Could not find download manager script at:\n{script_path}",
            )
        return False

    index_json_dir = _project_root / "index_json"
    index_json_dir.mkdir(parents=True, exist_ok=True)

    # Purge stale exit trigger
    exit_trigger = index_json_dir / ".dm_force_exit"
    if exit_trigger.exists():
        try:
            exit_trigger.unlink()
        except Exception:
            pass

    # Check if a persistent Download Manager is sleeping in background
    hwnd_file = index_json_dir / ".dm_hwnd"
    active_hwnd = None
    if sys.platform == "win32" and hwnd_file.exists():
        try:
            dm_hwnd = int(hwnd_file.read_text(encoding="utf-8").strip())
            import ctypes
            if ctypes.windll.user32.IsWindow(dm_hwnd):
                active_hwnd = dm_hwnd
        except Exception:
            active_hwnd = None

    if active_hwnd:
        # Send wake-up trigger file + Win32 HWND restore for instant unhide
        trigger_file = index_json_dir / ".show_dm_trigger"
        try:
            trigger_file.write_text("show", encoding="utf-8")
        except Exception:
            pass

        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(active_hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(active_hwnd)
        except Exception:
            pass

        try:
            sig_bus.notification.emit({
                "message": "⚡ Restored Download Manager instantly from memory.",
                "type": "info",
            })
        except Exception:
            pass
        return True

    # Launch new process
    python_exe = _find_python_executable()
    env = os.environ.copy()
    env["MCU_PREF_DIR"] = str(_project_root)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for k in ["_MEIPASS", "_MEIPASS2", "PYTHONHOME", "PYTHONPATH"]:
        env.pop(k, None)

    cmd = [str(python_exe), str(script_path)]

    try:
        creationflags = 0
        # If running python.exe (not pythonw.exe) on Windows, suppress console window
        if sys.platform == "win32" and python_exe.name.lower() == "python.exe":
            try:
                from src.modules.win_subprocess_hide import CREATE_NO_WINDOW
                creationflags = CREATE_NO_WINDOW
            except Exception:
                creationflags = 0x08000000

        subprocess.Popen(
            cmd,
            env=env,
            cwd=str(_project_root),
            creationflags=creationflags,
        )

        try:
            sig_bus.notification.emit({
                "message": "✔ Launched Download Boards & Libraries Manager.",
                "type": "success",
            })
        except Exception:
            pass
        return True
    except Exception as e:
        if parent:
            QMessageBox.critical(
                parent,
                "Error Launching Download Manager",
                f"Failed to launch download manager:\n\n{e}",
            )
        return False
