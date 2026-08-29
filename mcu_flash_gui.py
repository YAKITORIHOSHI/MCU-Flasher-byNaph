#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcu_flash_gui.py — Root forwarder / entry point for MCU Flasher by Naph.
Delegates directly to main.mcu_flash_gui.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

# Ensure root and main are on sys.path
_ROOT = Path(__file__).resolve().parent
_MAIN_DIR = _ROOT / "main"
_MODULES_DIR = _ROOT / "src" / "modules"

for _p in (_ROOT, _MAIN_DIR, _MODULES_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from main.mcu_flash_gui import *
from main.mcu_flash_gui import main

if __name__ == "__main__":
    sys.exit(main())
