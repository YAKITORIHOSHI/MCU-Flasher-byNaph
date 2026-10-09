"""Read-only discovery of native Arduino CLI, independent of Windows installers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[2]
_WINDOWS_SUFFIXES = frozenset({".exe", ".cmd", ".bat", ".ps1"})


def _native_executable(candidate) -> str | None:
    if not candidate:
        return None
    try:
        path = Path(os.path.abspath(os.fspath(candidate)))
        target = path.resolve(strict=True)
        if path.suffix.casefold() in _WINDOWS_SUFFIXES or target.suffix.casefold() in _WINDOWS_SUFFIXES:
            return None
        if not target.is_file() or not os.access(path, os.X_OK):
            return None
        with path.open("rb") as stream:
            if stream.read(2) == b"MZ":
                return None
        return str(path)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def managed_arduino_cli(root=None) -> str | None:
    """Require an owned native installation receipt without running the CLI."""
    folder = Path(ROOT if root is None else root) / ".ubuntu-tools/arduino-cli"
    receipt = folder / "installation.json"
    try:
        if folder.parent.is_symlink() or folder.is_symlink() or receipt.is_symlink():
            return None
        if receipt.stat().st_size > 8192:
            return None
        data = json.loads(receipt.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("owner") != "mcu-flasher-ubuntu-arduino-cli"
                or data.get("architecture") != "linux-amd64"
                or data.get("executable") != "arduino-cli"):
            return None
        executable = folder / "arduino-cli"
        if executable.is_symlink():
            return None
        return _native_executable(executable)
    except (OSError, ValueError, TypeError):
        return None


def find_arduino_cli() -> str | None:
    """Find a prepared CLI or an account install; never start shell/downloads."""
    found = managed_arduino_cli()
    if found:
        return found
    try:
        found = _native_executable(shutil.which("arduino-cli"))
        if found:
            return found
        home = Path.home()
    except (OSError, RuntimeError, ValueError):
        return None
    for directory in (".local/bin", "bin", ".arduino-cli/bin"):
        found = _native_executable(home / directory / "arduino-cli")
        if found:
            return found
    return None
