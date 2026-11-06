#!/usr/bin/env python3
"""Download the default Board Browser cores on Linux and macOS.

Windows setup calls these two functions itself. Builds use the PlatformIO store,
so a failure here only leaves Resources without these boards: report it and go on.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from src.modules.bootstrap import ensure_arduino_avr_board, ensure_esp32_board_folder
    results = []
    for prepare in (ensure_arduino_avr_board, ensure_esp32_board_folder):
        try:
            results.append(bool(prepare()))
        except Exception as exc:
            print(f"Board folder download failed: {exc}", file=sys.stderr)
            results.append(False)
    return 0 if all(results) else 1


def prepare_default_board_folders(python, *, env=None) -> bool:
    """Run the download in the private runtime and report without raising."""
    import subprocess
    try:
        done = subprocess.run([str(python), "-B", str(Path(__file__).resolve())], cwd=ROOT, env=env, check=False)
        ok = done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        print("Warning: Arduino AVR or ESP32 could not be added to Resources. Compile and Upload are not "
              "affected. Rerun Bootstrap with network access to retry.")
    return ok


if __name__ == "__main__":
    raise SystemExit(main())
