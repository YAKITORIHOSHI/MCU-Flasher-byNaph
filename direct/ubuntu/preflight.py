#!/usr/bin/env python3
"""Read-only Ubuntu dependency checks; never install packages or open a GUI."""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import importlib.util
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]

# Ubuntu package names are kept beside their SONAMEs so a first-run failure
# explains the actual missing dependency instead of Qt's generic xcb warning.
SYSTEM_LIBRARIES = (
    ("EGL", "libegl1"), ("GL", "libgl1"), ("nss3", "libnss3"),
    ("nspr4", "libnspr4"), ("gbm", "libgbm1"), ("xcb-dri3", "libxcb-dri3-0"),
    ("X11", "libx11-6"), ("X11-xcb", "libx11-xcb1"), ("Xext", "libxext6"),
    ("Xcomposite", "libxcomposite1"), ("Xdamage", "libxdamage1"),
    ("Xfixes", "libxfixes3"), ("Xrandr", "libxrandr2"), ("Xrender", "libxrender1"),
    ("Xtst", "libxtst6"), ("xkbfile", "libxkbfile1"), ("xkbcommon", "libxkbcommon0"),
    ("dbus-1", "libdbus-1-3"), ("fontconfig", "libfontconfig1"), ("freetype", "libfreetype6"),
    ("glib-2.0", "libglib2.0-0"), ("xcb", "libxcb1"),
    ("asound", "libasound2"), ("xcb-cursor", "libxcb-cursor0"),
    ("xkbcommon-x11", "libxkbcommon-x11-0"), ("xcb-icccm", "libxcb-icccm4"),
    ("xcb-image", "libxcb-image0"), ("xcb-keysyms", "libxcb-keysyms1"),
    ("xcb-render-util", "libxcb-render-util0"), ("xcb-xinerama", "libxcb-xinerama0"),
    ("xcb-xkb", "libxcb-xkb1"), ("xcb-shape", "libxcb-shape0"),
    ("xcb-randr", "libxcb-randr0"), ("xcb-sync", "libxcb-sync1"),
    ("xcb-shm", "libxcb-shm0"), ("xcb-xfixes", "libxcb-xfixes0"),
    ("xcb-render", "libxcb-render0"), ("xcb-util", "libxcb-util1"),
    ("usb-1.0", "libusb-1.0-0"), ("udev", "libudev1"),
)
SYSTEM_TOOLS = (("bash", "bash"), ("git", "git"), ("make", "build-essential"),
                ("gcc", "build-essential"), ("g++", "build-essential"),
                ("xdg-open", "xdg-utils"), ("ldd", "libc-bin"), ("rg", "ripgrep"))
CLIPBOARD_PACKAGES = ("xclip", "wl-clipboard")


def _ubuntu_t64_packages() -> bool:
    try:
        release = platform.freedesktop_os_release()
        if release.get("ID") == "ubuntu" and int(release.get("VERSION_ID", "0").split(".")[0]) >= 24:
            return True
    except (OSError, ValueError, AttributeError):
        pass
    return False


def _alsa_package() -> str:
    return "libasound2t64" if _ubuntu_t64_packages() else "libasound2"


def normalize_packages(packages) -> tuple[str, ...]:
    renamed = {"libasound2": "libasound2t64", "libglib2.0-0": "libglib2.0-0t64"} if _ubuntu_t64_packages() else {}
    return tuple(dict.fromkeys(renamed.get(item, item) for item in packages))


class MissingSystemDependencies(RuntimeError):
    """Known Ubuntu packages needed by Bootstrap, separate from fatal host errors."""

    def __init__(self, packages):
        self.packages = normalize_packages(packages)
        super().__init__("Ubuntu system dependencies are missing: " + ", ".join(self.packages) +
                         "\nLaunch MCU Flasher normally to install them through Ubuntu Bootstrap."
                         "\nManual alternative:\n" + system_install_command(self.packages))


def system_install_command(packages=None) -> str:
    """Return a manual recovery command; checks never execute it."""
    packages = packages or ["python3-venv", "python3-tk", "build-essential", "git", "xdg-utils", "ripgrep", *CLIPBOARD_PACKAGES,
                            *(package for _, package in SYSTEM_LIBRARIES)]
    return "sudo apt install " + " ".join(normalize_packages(packages))


def _check_tk() -> None:
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as exc:
        raise RuntimeError("Install python3-tk for the Python interpreter used by MCU Flasher.") from exc
    try:
        tk.Tcl()  # Check native Tcl data without requiring a display.
    except (tk.TclError, OSError) as exc:
        raise RuntimeError(f"Ubuntu's Tcl/Tk data is unavailable: {exc}. Install python3-tk.") from exc


def host_preflight(*, require_display=False) -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("This launcher requires native Ubuntu/Linux. Use MCU_Flasher.exe on Windows.")
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required. Install Ubuntu's python3 and python3-venv.")
    # Both Qt bindings are needed: PySide6 owns the workspace, while the library
    # sample viewer uses a separate PyQt5/QScintilla process. Their released Linux
    # wheels are jointly available on amd64; do not start an unattended Qt build.
    if struct.calcsize("P") != 8 or os.uname().machine.lower() not in ("x86_64", "amd64"):
        raise RuntimeError("The Ubuntu desktop release currently requires 64-bit Intel/AMD (amd64). "
                           "PyQt5/QScintilla sample-viewer wheels are unavailable for this architecture.")
    if require_display and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("Open MCU Flasher from your Ubuntu desktop session. No DISPLAY or WAYLAND_DISPLAY is available.")
    missing = []
    if importlib.util.find_spec("ensurepip") is None:
        missing.append("python3-venv")
    try:
        _check_tk()
    except RuntimeError:
        missing.append("python3-tk")
    for executable, package in SYSTEM_TOOLS:
        if not shutil.which(executable):
            missing.append(package)
    # Prepare both desktop backends so switching between X11 and Wayland does
    # not silently disable the assistant's native clipboard integration.
    if not (shutil.which("xclip") or shutil.which("xsel")):
        missing.append("xclip")
    if not (shutil.which("wl-copy") and shutil.which("wl-paste")):
        missing.append("wl-clipboard")
    for name, package in SYSTEM_LIBRARIES:
        try:
            library = ctypes.util.find_library(name)
            if not library:
                raise OSError(f"{name} was not found")
            ctypes.CDLL(library)
        except OSError:
            missing.append(package)
    if missing:
        raise MissingSystemDependencies(missing)


def _check_linked_libraries(paths) -> None:
    for path in paths:
        if not path.is_file():
            raise RuntimeError(f"A Qt runtime file is missing: {path}. Rerun Ubuntu Bootstrap.")
        result = subprocess.run(["ldd", str(path)], capture_output=True, text=True, timeout=15, check=False)
        missing = [line.strip() for line in result.stdout.splitlines() if "not found" in line]
        if result.returncode or missing:
            detail = "; ".join(missing) or result.stderr.strip() or f"ldd returned {result.returncode}"
            raise RuntimeError(f"Qt native dependencies are unavailable for {path.name}: {detail}\n"
                               "Install the Ubuntu desktop prerequisites:\n" + system_install_command())


def bootstrap_ready(core) -> bool:
    """Ubuntu launch needs native CLI tools and certified native board packs."""
    from main.platforms.ubuntu_arduino import find_arduino_cli
    from main.platforms.ubuntu_opencode import find_opencode_cli
    from src.modules.offline_bootstrap import ready
    from src.modules.offline_mode import startup_ready
    # Old online installations only have SCons and no preparation certificate.
    # Let the launcher repair those before showing an unusable board selector.
    return bool(find_arduino_cli() and find_opencode_cli() and ready(core) and startup_ready(core))


def runtime_preflight(root=ROOT) -> None:
    """Validate private imports and native Qt plugins without creating windows."""
    root = Path(root).resolve()
    expected = root / ".venv-linux"
    if sys.prefix == sys.base_prefix or Path(sys.prefix).resolve() != expected:
        raise RuntimeError("MCU Flasher must use its own .venv-linux environment. Rerun Ubuntu Bootstrap.")
    sys.path.insert(0, str(root))
    try:
        _check_tk()
        import PySide6
        import PySide6.QtWebEngineWidgets
        import serial
        import esptool
        import psutil
        import platformio
        import ptyprocess
        from src.modules.mbed_compat import prepare_dependencies
        prepare_dependencies()
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"Ubuntu runtime dependency validation failed: {exc}\n"
                           "Rerun Ubuntu Bootstrap; python3-tk must be supplied by Ubuntu.") from exc
    qt_root = Path(PySide6.__file__).parent / "Qt"
    _check_linked_libraries((qt_root / "plugins/platforms/libqxcb.so",
                            qt_root / "lib/libQt6WebEngineCore.so.6",
                            qt_root / "libexec/QtWebEngineProcess"))
    # Keep Qt5 and Qt6 native graphs in separate processes, as in production.
    result = subprocess.run([sys.executable, "-B", "-c",
                             "import PyQt5.QtWidgets, PyQt5.Qsci; "
                             "from pathlib import Path; import PyQt5; "
                             "print(Path(PyQt5.__file__).parent / 'Qt5/plugins/platforms/libqxcb.so')"],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError("The library sample viewer's PyQt5/QScintilla runtime is unavailable:\n" +
                           result.stderr.strip() + "\nRerun Ubuntu Bootstrap.")
    viewer_plugin = Path(result.stdout.strip())
    _check_linked_libraries((viewer_plugin,))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", action="store_true", help="Check the prepared private environment")
    parser.add_argument("--host", action="store_true", help="Check native Ubuntu prerequisites")
    parser.add_argument("--require-display", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.host or not args.runtime:
            host_preflight(require_display=args.require_display)
        if args.runtime:
            runtime_preflight()
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
