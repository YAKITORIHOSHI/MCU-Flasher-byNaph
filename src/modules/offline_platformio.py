#!/usr/bin/env python3
"""PlatformIO entry point used by the workspace, with downloads prohibited."""
from pathlib import Path
import runpy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.modules.offline_runtime import activate, guard_platformio
from src.modules.offline_mode import offline_enabled

if __name__ == "__main__":
    activate(offline_enabled())
    guard_platformio()
    from src.modules.platformio_locks import package_locks
    with package_locks():
        runpy.run_module("platformio", run_name="__main__")
