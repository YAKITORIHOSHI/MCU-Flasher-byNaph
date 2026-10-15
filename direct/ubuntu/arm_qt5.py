"""Expose Ubuntu's native ARM64 Qt5 viewer without enabling system site packages.

PyQt5 does not publish Linux ARM64 wheels. Ubuntu supplies ABI-matched Qt5/SIP/
QScintilla packages; only the PyQt5 package is made visible to the private Python.
This module is copied into the venv by Bootstrap and must stay self-contained.
"""
from __future__ import annotations

from importlib.machinery import PathFinder
import os
from pathlib import Path
import platform
import struct
import sys

SYSTEM_DIST_PACKAGES = Path("/usr/lib/python3/dist-packages")
SYSTEM_PACKAGES = ("python3-pyqt5", "python3-pyqt5.qsci", "python3-pyqt5.sip")
HOOK_MODULE = "_mcu_ubuntu_arm_qt5"
HOOK_FILENAME = HOOK_MODULE + ".pth"
HOOK_CONTENT = f"import {HOOK_MODULE}; {HOOK_MODULE}.activate()\n"


def missing_system_packages() -> tuple[str, ...]:
    """Inspect exact ABI/ELF providers without importing Qt5 into the Qt6 process."""
    package = SYSTEM_DIST_PACKAGES / "PyQt5"
    missing = []
    for name, dependency in (("QtCore", "python3-pyqt5"), ("QtWidgets", "python3-pyqt5"),
                             ("Qsci", "python3-pyqt5.qsci"), ("sip", "python3-pyqt5.sip")):
        spec = PathFinder.find_spec("PyQt5." + name, [str(package)])
        try:
            with Path(spec.origin).open("rb") as stream:
                header = stream.read(20)
            compatible = (header[:6] == b"\x7fELF\x02\x01" and len(header) == 20
                          and int.from_bytes(header[18:20], "little") == 183)
        except (AttributeError, OSError, TypeError):
            compatible = False
        if not compatible:
            missing.append(dependency)
    return tuple(dict.fromkeys(missing))


class _UbuntuQt5Finder:
    """Resolve only PyQt5; all unrelated modules retain private-venv lookup."""

    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname == "PyQt5":
            return PathFinder.find_spec(fullname, [str(SYSTEM_DIST_PACKAGES)])
        return None


def activate() -> None:
    if (sys.platform.startswith("linux") and struct.calcsize("P") == 8
            and platform.machine().casefold() in ("aarch64", "arm64")):
        if not any(getattr(item, "_mcu_ubuntu_qt5", False) for item in sys.meta_path):
            finder = _UbuntuQt5Finder()
            finder._mcu_ubuntu_qt5 = True
            # A copied amd64 PyQt5 wheel must not shadow Ubuntu's ARM64 package.
            sys.meta_path.insert(0, finder)


def install_bridge(site_packages: Path) -> None:
    """Install two ordinary owned files; no external links or broad sys.path edits."""
    site_packages = Path(site_packages)
    site_packages.mkdir(parents=True, exist_ok=True)
    files = ((site_packages / (HOOK_MODULE + ".py"), Path(__file__).read_bytes()),
             (site_packages / HOOK_FILENAME, HOOK_CONTENT.encode("utf-8")))
    for path, content in files:
        if path.is_symlink():
            raise RuntimeError(f"Ubuntu Qt5 bridge must be a local file: {path}")
        if path.is_file() and path.read_bytes() == content:
            continue
        temporary = path.with_name(path.name + ".new")
        if temporary.is_symlink():
            raise RuntimeError(f"Ubuntu Qt5 bridge temporary file must be local: {temporary}")
        try:
            with temporary.open("wb") as stream:
                stream.write(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
