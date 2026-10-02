"""Explicit handoff to a separate bootstrap process; the workspace stays offline."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import QMessageBox
from src.modules.offline_bootstrap import clean_bootstrap_environment

ROOT = Path(__file__).resolve().parents[2]


def launch_download_manager(parent=None):
    """Compatibility name used by the toolbar: open bootstrap, never a downloader."""
    env = clean_bootstrap_environment()
    try:
        if sys.platform == "win32":
            command = ["wscript.exe", "//nologo", str(ROOT / "direct/windows/run.vbs"), "--repair"]
            subprocess.Popen(command, cwd=ROOT, env=env, creationflags=0x08000000)
        else:
            terminal = shutil.which("x-terminal-emulator")
            if not terminal:
                raise RuntimeError("Open a terminal and run: bash direct/ubuntu/run.sh --repair")
            command = [terminal, "-e", sys.executable, str(ROOT / "direct/ubuntu/setup.py")]
            subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
        return True
    except OSError as exc:
        if parent:
            QMessageBox.warning(parent, "Bootstrap", f"Could not open bootstrap: {exc}")
        return False
    except RuntimeError as exc:
        if parent:
            QMessageBox.information(parent, "Bootstrap", str(exc))
        return False
