#!/usr/bin/env python3
"""Create/repair the app's Ubuntu venv without changing system Python."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_DIR = ROOT / ".venv-linux"
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true", help="Open MCU Flasher after setup")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        print("This setup is for Ubuntu/Linux. On Windows use MCU_Flasher.exe.", file=sys.stderr)
        return 1
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required.", file=sys.stderr)
        return 1
    from src.modules.runtime_resources import enforce_minimum_cpu_requirement
    if not enforce_minimum_cpu_requirement():
        return 1
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("Run setup as your normal desktop account, without sudo.", file=sys.stderr)
        return 1
    try:
        # In-place repair preserves installed packages; never clear an environment.
        venv.EnvBuilder(with_pip=True, symlinks=True).create(ENV_DIR)
        python = ENV_DIR / "bin" / "python"
        subprocess.run([
            str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "--prefer-binary", "-r", str(ROOT / "direct" / "requirements-ubuntu.txt"),
        ], check=True)
        subprocess.run([str(python), "-c",
                        "import PySide6.QtWebEngineWidgets, serial, psutil, platformio, ptyprocess"], check=True)
        print("Ubuntu runtime ready. Launch with: bash direct/runThisOnUbuntu.sh")
        if args.launch:
            return subprocess.call([str(python), str(ROOT / "mcu_flash_gui.py")], cwd=ROOT)
        return 0
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Setup failed: {exc}\nCheck network access and install python3-venv. "
              "Then rerun this command to repair the environment.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
