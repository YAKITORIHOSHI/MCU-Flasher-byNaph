"""Install detected distribution prerequisites; only its package manager receives administrator rights."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys

from direct.ubuntu import fedora_packages, preflight
from direct.ubuntu.arm_qt5 import SYSTEM_PACKAGES as ARM_QT5_PACKAGES
from direct.ubuntu.preflight import (MissingSystemDependencies, CLIPBOARD_PACKAGES, SYSTEM_LIBRARIES, ARM_SYSTEM_LIBRARIES,
                                    SYSTEM_TOOLS, normalize_packages,
                                    system_install_command)

APT_GET = Path("/usr/bin/apt-get")
DNF = Path("/usr/bin/dnf")
# Present on Fedora's image-based desktops, where dnf cannot change the system.
ATOMIC_MARKER = Path("/run/ostree-booted")
SUDO = Path("/usr/bin/sudo")
PKEXEC = Path("/usr/bin/pkexec")
ALLOWED_PACKAGES = frozenset(("python3", "python3-venv", "libasound2t64", "libglib2.0-0t64",
                              *ARM_QT5_PACKAGES,
                              *CLIPBOARD_PACKAGES,
                              *(package for _, package in SYSTEM_LIBRARIES),
                              *(package for _, package in ARM_SYSTEM_LIBRARIES),
                              *(package for _, package in SYSTEM_TOOLS)))
ALLOWED_FEDORA_PACKAGES = frozenset((fedora_packages.VENV_PACKAGE, *fedora_packages.ARM_QT5_PACKAGES,
                                     *fedora_packages.CLIPBOARD_PACKAGES,
                                     *(package for _, package in fedora_packages.SYSTEM_LIBRARIES),
                                     *(package for _, package in fedora_packages.ARM_SYSTEM_LIBRARIES),
                                     *(package for _, package in fedora_packages.SYSTEM_TOOLS)))


def _package_commands(packages) -> tuple[tuple[str, list[str]], ...]:
    """One bounded command per step, run with the host's own package manager."""
    if preflight.package_manager() == "dnf":
        if ATOMIC_MARKER.exists():
            raise RuntimeError("Fedora Atomic desktops cannot install these packages automatically. "
                               "Layer them with rpm-ostree, restart and launch again:\n"
                               "sudo rpm-ostree install " + " ".join(packages))
        if not os.access(DNF, os.X_OK):
            raise RuntimeError("Automatic system setup requires Fedora's dnf.\n" + system_install_command(packages))
        return (("install prerequisites", [str(DNF), "install", "--assumeyes", "--refresh",
                                           "--setopt=install_weak_deps=False", "--setopt=timeout=30",
                                           "--setopt=retries=1", *packages]),)
    if not os.access(APT_GET, os.X_OK):
        raise RuntimeError("Automatic system setup requires Ubuntu's apt-get.\n" + system_install_command(packages))
    options = ["-o", "DPkg::Lock::Timeout=120", "-o", "Acquire::Retries=1",
               "-o", "Acquire::http::Timeout=30", "-o", "Acquire::https::Timeout=30"]
    return (("refresh package indexes", [str(APT_GET), *options, "update"]),
            ("install prerequisites", [str(APT_GET), *options, "install", "--yes", "--no-remove",
                                       "--no-install-recommends", *packages]))


def install_system_packages(packages, *, env=None) -> None:
    """One bounded install attempt, with OS-owned authentication and visible output."""
    from src.modules.ubuntu_release import require_supported_ubuntu
    require_supported_ubuntu()
    packages = normalize_packages(packages)
    if not packages:
        return
    label = preflight.linux_label()
    if not sys.platform.startswith("linux") or os.geteuid() == 0:
        raise RuntimeError(f"Run {label} Bootstrap as your normal desktop account, without sudo.")
    allowed = ALLOWED_FEDORA_PACKAGES if preflight.package_manager() == "dnf" else ALLOWED_PACKAGES
    if any(package not in allowed for package in packages):
        raise RuntimeError(f"Bootstrap received an unknown {label} prerequisite package.")
    commands = _package_commands(packages)
    if sys.stdin.isatty() and os.access(SUDO, os.X_OK):
        elevation = [str(SUDO)]
    elif (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and os.access(PKEXEC, os.X_OK):
        elevation = [str(PKEXEC)]
    else:
        raise RuntimeError(f"{label} administrator authentication is unavailable. "
                           "Open a desktop terminal and launch MCU Flasher again.\n" +
                           system_install_command(packages))
    print(f"{label} Bootstrap will install: " + ", ".join(packages), flush=True)
    print(f"Authenticate using {label}'s administrator prompt. Setup continues automatically afterward.", flush=True)
    # Run the distribution's package manager directly, never this checkout or the GUI as root.
    for action, command in commands:
        result = subprocess.run([*elevation, *command], env=env, cwd="/", check=False)
        if result.returncode:
            raise RuntimeError(f"{label} Bootstrap could not {action} (exit {result.returncode}). "
                               "Administrator authentication may have been cancelled, or the package manager reported an error above."
                               "\nResolve that error and launch again.\n" + system_install_command(packages))


def ensure_system_dependencies(*, require_display=False, env=None) -> None:
    try:
        preflight.host_preflight(require_display=require_display)
    except MissingSystemDependencies as missing:
        install_system_packages(missing.packages, env=env)
        importlib.invalidate_caches()
        # Never trust a package manager's exit code alone or retry authentication indefinitely.
        try:
            preflight.host_preflight(require_display=require_display)
        except MissingSystemDependencies as remaining:
            raise RuntimeError(f"{preflight.linux_label()} packages were installed, but dependency checks still fail. "
                               "Bootstrap has stopped before preparing or launching the app.\n" + str(remaining)) from remaining
