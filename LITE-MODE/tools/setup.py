#!/usr/bin/env python3
"""Optional: install a Lite-only Python runtime without changing the main one."""
from pathlib import Path
import os
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from worker import host_check


def main():
    host_check()
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required")
    runtime = ROOT / ".venv"
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("Creating LITE-MODE/.venv", flush=True)
        venv.EnvBuilder(with_pip=True).create(runtime)
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "PIP_TARGET", "PIP_PREFIX", "MCU_FLASHER_OFFLINE_RUNTIME", "MCU_FLASHER_WORKSPACE_RUNTIME", "PIP_NO_INDEX"):
        env.pop(key, None)
    subprocess.run([str(python), "-B", "-m", "pip", "install", "--disable-pip-version-check", "-r", str(ROOT / "requirements.txt")],
        env=env, check=True, timeout=1200)
    print("Lite's Python tools are ready. Launch UBUNTU or WINDOWS.exe.")


if __name__ == "__main__":
    main()
