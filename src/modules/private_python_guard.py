#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
src.modules.private_python_guard — Strict Private Python Runtime Enforcer.

Guarantees that MCU Flasher by Naph ONLY executes using its own private Python
runtime located in:
    <project_root>/src/_python/python.exe (or pythonw.exe)

NO FALLBACKS TO SYSTEM / DESKTOP PYTHON ARE ALLOWED AT ALL COSTS.
If invoked by any other Python interpreter (system Python, Microsoft Store stub,
PATH python, etc.):
- Automatically re-launches under <project_root>/src/_python/python.exe with the
  exact same command-line arguments and an isolated environment.
- If the private runtime is missing or damaged, attempts auto-healing from
  installers/.handsoff/python-*-amd64.exe.
- If healing fails, halts immediately with a clear error message. It will NEVER
  fall back to running on the desktop/system Python.
"""
from __future__ import annotations

import os
import sys
import ctypes
import subprocess
from pathlib import Path


def find_project_root() -> Path:
    """Resolve the project root directory dynamically."""
    current = Path(__file__).resolve()
    for parent in [current.parent, current.parent.parent, current.parent.parent.parent]:
        if (parent / "src" / "modules").is_dir() or (parent / "main").is_dir():
            return parent
    return current.parent.parent.parent


_PROJECT_ROOT = find_project_root()
PRIVATE_PYTHON_DIR = _PROJECT_ROOT / "src" / "_python"


ENV_PYTHON_DIR = _PROJECT_ROOT / "env"


def is_running_private_python() -> bool:
    """Return True iff current sys.executable is inside src/_python or the project venv (env/)."""
    try:
        current_exe = Path(sys.executable).resolve()
        private_dir = PRIVATE_PYTHON_DIR.resolve()
        if private_dir in current_exe.parents or current_exe == (private_dir / "python.exe") or current_exe == (private_dir / "pythonw.exe"):
            return True
        env_dir = ENV_PYTHON_DIR.resolve()
        if env_dir in current_exe.parents or current_exe == (env_dir / "Scripts" / "python.exe") or current_exe == (env_dir / "Scripts" / "pythonw.exe"):
            return True
        return False
    except Exception:
        return False


def _heal_private_runtime_if_needed(target_dir: Path) -> bool:
    """Attempt silent installation of bundled Python from installers/.handsoff/."""
    if sys.platform != "win32":
        return False

    handsoff_dirs = [
        _PROJECT_ROOT / "installers" / ".handsoff",
        _PROJECT_ROOT.parent / "installers" / ".handsoff",
    ]
    installer = None
    for hdir in handsoff_dirs:
        if hdir.is_dir():
            candidates = sorted(hdir.glob("python-*-amd64.exe"), reverse=True)
            if candidates:
                installer = candidates[0]
                break

    if not installer or not installer.is_file():
        return False

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        # Run silent installer directly into src/_python
        install_cmd = [
            str(installer),
            "/quiet",
            "InstallAllUsers=0",
            f"TargetDir={target_dir}",
            "AssociateFiles=0",
            "Shortcuts=0",
            "Include_launcher=0",
            "InstallLauncherAllUsers=0",
            "PrependPath=0",
        ]
        res = subprocess.run(
            install_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            timeout=180,
        )
        return res.returncode == 0
    except Exception:
        return False


def get_private_python_exe(prefer_pythonw: bool = False) -> Path:
    """
    Get the exact Path to the private Python executable in src/_python.
    STRICT: NEVER returns any system or external Python.
    """
    pyw = PRIVATE_PYTHON_DIR / "pythonw.exe"
    py  = PRIVATE_PYTHON_DIR / "python.exe"

    target = pyw if prefer_pythonw and pyw.is_file() else py

    # Check if target exists and is working
    healthy = False
    if target.is_file():
        try:
            cf = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            res = subprocess.run(
                [str(target), "-c", "import sys, encodings; sys.exit(0)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=cf,
                timeout=5,
            )
            healthy = (res.returncode == 0)
        except Exception:
            healthy = False

    if not healthy:
        # Attempt healing
        if _heal_private_runtime_if_needed(PRIVATE_PYTHON_DIR):
            target = pyw if prefer_pythonw and pyw.is_file() else py
            if target.is_file():
                healthy = True

    if not healthy:
        err_msg = (
            "MCU Flasher Fatal Error:\n\n"
            "The required private Python runtime is missing or damaged at:\n"
            f"{PRIVATE_PYTHON_DIR}\n\n"
            "MCU Flasher is strictly configured to use its own isolated runtime.\n"
            "Running with the system Python on your desktop/operating system is STRICTLY FORBIDDEN.\n\n"
            "Please ensure the src/_python folder is intact or reinstall MCU Flasher."
        )
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.MessageBoxW(
                    0, err_msg, "MCU Flasher — Strict Runtime Policy", 0x10  # MB_ICONERROR
                )
            except Exception:
                pass
        print(err_msg, file=sys.stderr)
        sys.exit(1)

    return target


def sanitize_environment() -> dict[str, str]:
    """Return an os.environ copy stripped of system Python contamination."""
    env = os.environ.copy()
    # Strip variables that could cause system python or user site-packages to interfere
    for var in [
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
    ]:
        env.pop(var, None)

    # Prepend private python and Scripts to PATH
    scripts_dir = PRIVATE_PYTHON_DIR / "Scripts"
    path_val = env.get("PATH", "")
    paths = [str(PRIVATE_PYTHON_DIR), str(scripts_dir)]
    env["PATH"] = os.pathsep.join(paths) + os.pathsep + path_val
    return env


def enforce_private_python(prefer_pythonw: bool = False) -> None:
    """
    Call at the very beginning of application entry points.
    If the current process was started with any interpreter other than
    src/_python, this IMMEDIATELY replaces/relaunches with the private Python
    and terminates the current process.
    """
    if is_running_private_python():
        # Already running under private Python — ensure env is clean
        for var in ["PYTHONHOME", "PYTHONPATH"]:
            os.environ.pop(var, None)
        return

    # Not running under private Python — reject external/system Python and switch to private runtime
    print(
        f"[MCU Flasher] External system Python rejected ({sys.executable}). "
        f"Exclusively using private Python runtime at {PRIVATE_PYTHON_DIR}.",
        file=sys.stderr,
    )
    private_exe = get_private_python_exe(prefer_pythonw=prefer_pythonw)
    clean_env = sanitize_environment()

    cmd = [str(private_exe)] + sys.argv

    creationflags = 0
    if sys.platform == "win32" and prefer_pythonw:
        creationflags = 0x08000000  # CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_PROJECT_ROOT),
            env=clean_env,
            creationflags=creationflags,
        )
        # Exit immediately so desktop Python terminates
        sys.exit(proc.wait())
    except Exception as exc:
        err = f"Failed to hand off execution to private Python runtime:\n\n{exc}"
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.MessageBoxW(
                    0, err, "MCU Flasher — Launch Error", 0x10
                )
            except Exception:
                pass
        print(err, file=sys.stderr)
        sys.exit(1)
