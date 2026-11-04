"""Select separate Windows/native POSIX implementations without importing the other host.

Linux and macOS share one backend; only the modules with a real difference branch.
"""
from __future__ import annotations
import importlib
import sys


def get_platform_backend(platform_name: str | None = None):
    host = platform_name or sys.platform
    if host == "win32":
        return importlib.import_module("main.platforms.windows")
    if host.startswith("linux") or host == "darwin":
        return importlib.import_module("main.platforms.ubuntu")
    raise RuntimeError(f"Unsupported desktop host {host!r}. Use Windows, Linux or macOS.")
