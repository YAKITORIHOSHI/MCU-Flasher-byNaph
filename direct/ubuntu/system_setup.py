"""Install detected Ubuntu prerequisites; only apt-get receives administrator rights."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys

from direct.ubuntu import preflight
from direct.ubuntu.preflight import (MissingSystemDependencies, CLIPBOARD_PACKAGES, SYSTEM_LIBRARIES,
                                    SYSTEM_TOOLS, normalize_packages,
                                    system_install_command)

APT_GET = Path("/usr/bin/apt-get")
SUDO = Path("/usr/bin/sudo")
PKEXEC = Path("/usr/bin/pkexec")
ALLOWED_PACKAGES = frozenset(("python3", "python3-venv", "python3-tk", "libasound2t64", "libglib2.0-0t64",
                              *CLIPBOARD_PACKAGES,
                              *(package for _, package in SYSTEM_LIBRARIES),
                              *(package for _, package in SYSTEM_TOOLS)))


def install_system_packages(packages, *, env=None) -> None:
    """One bounded install attempt, with OS-owned authentication and visible output."""
    packages = normalize_packages(packages)
    if not packages:
        return
    if not sys.platform.startswith("linux") or os.geteuid() == 0:
        raise RuntimeError("Run Ubuntu Bootstrap as your normal desktop account, without sudo.")
    if any(package not in ALLOWED_PACKAGES for package in packages):
        raise RuntimeError("Bootstrap received an unknown Ubuntu prerequisite package.")
    if not os.access(APT_GET, os.X_OK):
        raise RuntimeError("Automatic system setup requires Ubuntu's apt-get.\n" + system_install_command(packages))
    if sys.stdin.isatty() and os.access(SUDO, os.X_OK):
        elevation = [str(SUDO)]
    elif (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and os.access(PKEXEC, os.X_OK):
        elevation = [str(PKEXEC)]
    else:
        raise RuntimeError("Ubuntu administrator authentication is unavailable. "
                           "Open a desktop terminal and launch MCU Flasher again.\n" +
                           system_install_command(packages))
    print("Ubuntu Bootstrap will install: " + ", ".join(packages), flush=True)
    print("Authenticate using Ubuntu's administrator prompt. Setup continues automatically afterward.", flush=True)
    # Run Ubuntu's package manager directly, never this checkout or the GUI as root.
    options = ["-o", "DPkg::Lock::Timeout=120", "-o", "Acquire::Retries=1",
               "-o", "Acquire::http::Timeout=30", "-o", "Acquire::https::Timeout=30"]
    for action, arguments in (("refresh package indexes", ["update"]),
                              ("install prerequisites", ["install", "--yes", "--no-remove",
                                                         "--no-install-recommends", *packages])):
        result = subprocess.run([*elevation, str(APT_GET), *options, *arguments],
                                env=env, cwd="/", check=False)
        if result.returncode:
            raise RuntimeError(f"Ubuntu Bootstrap could not {action} (exit {result.returncode}). "
                               "Administrator authentication may have been cancelled, or apt reported an error above."
                               "\nResolve that error and launch again.\n" + system_install_command(packages))


def ensure_system_dependencies(*, require_display=False, env=None) -> None:
    try:
        preflight.host_preflight(require_display=require_display)
    except MissingSystemDependencies as missing:
        install_system_packages(missing.packages, env=env)
        importlib.invalidate_caches()
        # Never trust an apt exit code alone or retry authentication indefinitely.
        try:
            preflight.host_preflight(require_display=require_display)
        except MissingSystemDependencies as remaining:
            raise RuntimeError("Ubuntu packages were installed, but dependency checks still fail. "
                               "Bootstrap has stopped before preparing or launching the app.\n" + str(remaining)) from remaining
