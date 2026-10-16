#!/usr/bin/env python3
"""Read-only Ubuntu dependency checks; never install packages or open a GUI."""
from __future__ import annotations

from importlib import metadata

import argparse
import ctypes
import ctypes.util
import importlib.util
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.modules.ubuntu_release import require_supported_ubuntu
from src.modules.ubuntu_architecture import elf_matches_host, host_architecture

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
    ("glib-2.0", "libglib2.0-0t64"), ("xcb", "libxcb1"),
    ("asound", "libasound2t64"), ("xcb-cursor", "libxcb-cursor0"),
    ("xkbcommon-x11", "libxkbcommon-x11-0"), ("xcb-icccm", "libxcb-icccm4"),
    ("xcb-image", "libxcb-image0"), ("xcb-keysyms", "libxcb-keysyms1"),
    ("xcb-render-util", "libxcb-render-util0"), ("xcb-xinerama", "libxcb-xinerama0"),
    ("xcb-xkb", "libxcb-xkb1"), ("xcb-shape", "libxcb-shape0"),
    ("xcb-randr", "libxcb-randr0"), ("xcb-sync", "libxcb-sync1"),
    ("xcb-shm", "libxcb-shm0"), ("xcb-xfixes", "libxcb-xfixes0"),
    ("xcb-render", "libxcb-render0"), ("xcb-util", "libxcb-util1"),
    ("usb-1.0", "libusb-1.0-0"), ("udev", "libudev1"),
)
# Qt's ARM64 wheels link these against Ubuntu instead of bundling them. This
# list comes from the PySide6 6.12.0 / WebEngine 6.12.0.140 ELF dependency closure
# (workspace, xcb and Wayland client); keep amd64's existing prerequisites intact.
ARM_SYSTEM_LIBRARIES = (
    ("opus", "libopus0"), ("lcms2", "liblcms2-2"),
    ("minizip", "libminizip1t64"), ("snappy", "libsnappy1v5"),
    ("webp", "libwebp7"), ("webpdemux", "libwebpdemux2"), ("webpmux", "libwebpmux3"),
    ("brotlidec", "libbrotli1"), ("zstd", "libzstd1"), ("z", "zlib1g"),
    ("expat", "libexpat1"), ("ICE", "libice6"), ("SM", "libsm6"),
    ("wayland-client", "libwayland-client0"), ("wayland-cursor", "libwayland-cursor0"),
    ("wayland-egl", "libwayland-egl1"),
)
SYSTEM_TOOLS = (("bash", "bash"), ("git", "git"), ("make", "build-essential"),
                ("gcc", "build-essential"), ("g++", "build-essential"),
                ("xdg-open", "xdg-utils"), ("ldd", "libc-bin"), ("rg", "ripgrep"),
                ("secret-tool", "libsecret-tools"),
                ("gnome-keyring-daemon", "gnome-keyring"))
CLIPBOARD_PACKAGES = ("xclip", "wl-clipboard")


def system_libraries(architecture: str | None = None) -> tuple[tuple[str, str], ...]:
    """Return only this CPU's Ubuntu library prerequisites, without loading Qt."""
    return SYSTEM_LIBRARIES + (ARM_SYSTEM_LIBRARIES if (architecture or host_architecture()) == "arm64" else ())


def normalize_packages(packages) -> tuple[str, ...]:
    require_supported_ubuntu()
    renamed = {"libasound2": "libasound2t64", "libglib2.0-0": "libglib2.0-0t64"}
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
    if not packages:
        packages = ["python3-venv", "python3-tk", "build-essential", "git", "xdg-utils", "ripgrep", "libsecret-tools", "gnome-keyring", *CLIPBOARD_PACKAGES,
                    *(package for _, package in system_libraries())]
        if host_architecture() == "arm64":
            from direct.ubuntu.arm_qt5 import SYSTEM_PACKAGES
            packages.extend(SYSTEM_PACKAGES)
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
    require_supported_ubuntu()
    if not sys.platform.startswith("linux"):
        raise RuntimeError("This launcher requires native Ubuntu/Linux. Use MCU_Flasher.exe on Windows.")
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required. Install Ubuntu's python3 and python3-venv.")
    architecture = host_architecture(os.uname().machine)
    if struct.calcsize("P") != 8:
        raise RuntimeError("MCU Flasher requires a 64-bit Ubuntu Python interpreter (amd64 or ARM64).")
    if require_display and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("Open MCU Flasher from your Ubuntu desktop session. No DISPLAY or WAYLAND_DISPLAY is available.")
    missing = []
    if architecture == "arm64":
        from direct.ubuntu.arm_qt5 import missing_system_packages
        missing.extend(missing_system_packages())
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
    for name, package in system_libraries(architecture):
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
    failures = []
    for path in paths:
        if not path.is_file():
            raise RuntimeError(f"A Qt runtime file is missing: {path}. Rerun Ubuntu Bootstrap.")
        result = subprocess.run(["ldd", str(path)], capture_output=True, text=True, timeout=15, check=False)
        missing = [line.strip() for line in result.stdout.splitlines() if "not found" in line]
        if result.returncode or missing:
            detail = "; ".join(missing) or result.stderr.strip() or f"ldd returned {result.returncode}"
            failures.append(f"{path.name}: {detail}")
    if failures:
        raise RuntimeError("Qt native dependencies are unavailable:\n" + "\n".join(failures) + "\n"
                           "Install the Ubuntu desktop prerequisites:\n" + system_install_command())


def _check_qt_runtime_files(qt_root: Path, architecture: str) -> None:
    """Validate native CPU and all linked dependencies before importing Qt."""
    paths = (qt_root / "lib/libQt6Core.so.6", qt_root / "plugins/platforms/libqxcb.so",
             qt_root / "lib/libQt6WebEngineCore.so.6", qt_root / "libexec/QtWebEngineProcess")
    # Verify every target before letting ldd inspect any one of them. A copied
    # runtime must never reach native loading or a host loader for a foreign CPU.
    for native in paths:
        if not native.is_file():
            raise RuntimeError(f"A Qt runtime file is missing: {native}. Rerun Ubuntu Bootstrap.")
        if not elf_matches_host(native, architecture):
            raise RuntimeError("Ubuntu's Qt runtime was copied from another CPU architecture. "
                               "Run bash direct/ubuntu/run.sh --repair on this computer.")
    _check_linked_libraries(paths)


def bootstrap_ready(core) -> bool:
    """Ubuntu launch needs native CLI tools and certified native board packs."""
    from main.platforms.ubuntu_arduino import find_arduino_cli
    from main.platforms.ubuntu_opencode import find_opencode_cli
    from src.modules.offline_bootstrap import ready
    from src.modules.offline_mode import startup_ready
    # Old online installations only have SCons and no preparation certificate.
    # Let the launcher repair those before showing an unusable board selector.
    return bool(find_arduino_cli() and find_opencode_cli() and ready(core) and startup_ready(core))


def check_qt_python_compatibility() -> None:
    """Reject affected Linux wheels using metadata, before any native Qt import."""
    if not sys.platform.startswith("linux") or sys.version_info >= (3, 12):
        return
    try:
        version = metadata.version("PySide6")
        parts = version.split(".", 2)
        release = int(parts[0]), int(parts[1])
    except (metadata.PackageNotFoundError, ValueError, IndexError) as exc:
        raise RuntimeError("The Ubuntu Qt runtime version cannot be validated. "
                           "Run bash direct/ubuntu/run.sh --repair.") from exc
    if release >= (6, 12):
        raise RuntimeError(f"PySide6 {version} is incompatible with Ubuntu Python "
                           f"{sys.version_info[0]}.{sys.version_info[1]}. "
                           "This Linux wheel can crash while rendering. "
                           "Run bash direct/ubuntu/run.sh --repair to install PySide6 below 6.12.")


def check_platformio_click_compatibility() -> None:
    """Keep Ubuntu's PlatformIO CLI off Click's deprecated parsing APIs."""
    if not sys.platform.startswith("linux"):
        return
    try:
        version = metadata.version("click")
        match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:\.post[0-9]+)?"
                             r"(?:\+[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*)?", version)
        if match is None:
            raise ValueError("Unrecognized Click version")
        release = tuple(int(part) for part in match.groups())
    except (metadata.PackageNotFoundError, ValueError, TypeError) as exc:
        raise RuntimeError("The Ubuntu PlatformIO Click dependency cannot be validated. "
                           "Run bash direct/ubuntu/run.sh --repair.") from exc
    if not (8, 1, 8) <= release < (8, 2, 0):
        raise RuntimeError(f"Click {version} is incompatible with Ubuntu's PlatformIO runtime. "
                           "PlatformIO still uses parsing APIs deprecated in Click 8.2. "
                           "Run bash direct/ubuntu/run.sh --repair to install Click >=8.1.8,<8.2.")


def runtime_preflight(root=ROOT) -> None:
    """Validate private imports and native Qt plugins without creating windows."""
    require_supported_ubuntu()
    root = Path(root).resolve()
    expected = root / ".venv-linux"
    if sys.prefix == sys.base_prefix or Path(sys.prefix).resolve() != expected:
        raise RuntimeError("MCU Flasher must use its own .venv-linux environment. Rerun Ubuntu Bootstrap.")
    architecture = host_architecture()
    from direct.ubuntu.runtime_architecture import check_runtime_architecture
    check_runtime_architecture(expected, architecture)
    check_qt_python_compatibility()
    # Do not import a copied amd64 Qt extension on ARM64 (or vice versa).
    qt_spec = importlib.util.find_spec("PySide6")
    if qt_spec is not None and qt_spec.origin:
        qt_root = Path(qt_spec.origin).parent / "Qt"
        _check_qt_runtime_files(qt_root, architecture)
    check_platformio_click_compatibility()
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
    # Keep Qt5 and Qt6 native graphs in separate processes, as in production.
    result = subprocess.run([sys.executable, "-B", "-c",
                             "import PyQt5.QtWidgets, PyQt5.Qsci; "
                             "from pathlib import Path; from PyQt5.QtCore import QLibraryInfo; "
                             "print(Path(QLibraryInfo.location(QLibraryInfo.PluginsPath)) / 'platforms/libqxcb.so')"],
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
