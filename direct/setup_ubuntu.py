#!/usr/bin/env python3
"""Compatible entry point for the separately maintained Ubuntu setup."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "ubuntu/setup.py"), run_name="__main__")
