"""Select separate Windows/Ubuntu implementations without importing the other host."""
from __future__ import annotations
import importlib
import sys


def get_platform_backend(platform_name: str | None = None):
    host = platform_name or sys.platform
    if host == "win32":
        return importlib.import_module("main.platforms.windows")
    if host.startswith("linux"):
        return importlib.import_module("main.platforms.ubuntu")
    raise RuntimeError(f"Unsupported desktop host {host!r}. Use Windows or Ubuntu/Linux.")
