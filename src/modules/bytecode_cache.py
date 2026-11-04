"""Keep compiled Python bytecode outside the installation.

A fresh or read-only installation otherwise recompiles every module from source
on each launch and build. Python revalidates each entry against its source file,
so an updated application can never run stale code from this cache.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def cache_directory() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / ".mcuflasher-app" / "pycache"
    value = os.environ.get("XDG_CACHE_HOME") or ""
    root = Path(value) if value.startswith("/") else Path.home() / ".cache"
    return root / "mcu-flasher" / "pycache"


def enable() -> None:
    """Enable the cache for this process and its Python children.

    Launchers pass ``-B`` to keep the installation clean; a separate prefix
    already does that. An explicit PYTHONDONTWRITEBYTECODE request is respected.
    """
    if os.environ.get("PYTHONDONTWRITEBYTECODE"):
        return
    try:
        directory = cache_directory()
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    sys.pycache_prefix = str(directory)
    sys.dont_write_bytecode = False
    os.environ["PYTHONPYCACHEPREFIX"] = str(directory)
