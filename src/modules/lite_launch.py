"""Select the native Lite entry after the shared Bootstrap has succeeded."""
from __future__ import annotations
from pathlib import Path
import platform
import sys


def lite_command(root, arguments, *, system=None, machine=None):
    system = sys.platform if system is None else system
    machine = (machine or platform.machine()).lower()
    if system == "win32" and machine in {"amd64", "x86_64"}:
        name = "WINDOWS.exe"
    elif system.startswith("linux") and machine in {"amd64", "x86_64"}:
        name = "UBUNTU"
    elif system.startswith("linux") and machine in {"aarch64", "arm64"}:
        name = "UBUNTU_ARM64"
    else:
        raise RuntimeError("Lite currently requires native Ubuntu or Windows 10/11 x64")
    executable = Path(root) / "LITE-MODE" / name
    if not executable.is_file():
        raise RuntimeError(f"The Lite executable is missing: {executable}")
    command = [str(executable), "--from-bootstrap"]
    arguments = list(arguments)
    if "--project" in arguments:
        index = arguments.index("--project")
        if index + 1 >= len(arguments):
            raise ValueError("--project requires a sketch folder")
        command += ["--project", str(Path(arguments[index + 1]).absolute())]
    return command
