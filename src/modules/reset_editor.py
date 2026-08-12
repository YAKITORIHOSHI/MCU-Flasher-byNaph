#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reset_editor.py

Standalone recovery tool for MCU Flasher by Naph.

If the Monaco (VS Code-style) editor is causing crashes, freezes, or other
hardware-compatibility problems on a low-spec machine, run this script to
force the app's File Editor setting back to "Default" (the lightweight
Tkinter editor) without having to open the app itself.

It edits the same config file the main app reads/writes:
    ~/.mcu_gui_config.json   ->  data["shared"]["editor_mode"]

Usage:
    python reset_editor.py
"""

import json
import sys
from pathlib import Path

CONFIG_FILE = Path.home() / ".mcu_gui_config.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠ Could not parse existing config ({e}). Starting fresh.")
            return {}
    return {}


def save_config(data: dict):
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main():
    print("MCU Flasher — Editor Reset Tool")
    print(f"Config file: {CONFIG_FILE}")
    print()

    data = load_config()
    shared = data.setdefault("shared", {})

    previous_mode = shared.get("editor_mode", "default")

    # Force back to the lightweight Tkinter editor.
    shared["editor_mode"] = "default"

    # Clear the Monaco crash-safety sentinel too, so a stale flag doesn't
    # linger around and confuse the app's own auto-revert logic next launch.
    shared["monaco_boot_pending"] = False

    try:
        save_config(data)
    except Exception as e:
        print(f"✖ Failed to write config: {e}")
        sys.exit(1)

    if previous_mode == "monaco":
        print("✔ Editor was set to Monaco — it has been reset to Default.")
    else:
        print("✔ Editor was already set to Default. Sentinel cleared, nothing else to do.")

    print()
    print("You can now safely restart MCU Flasher by Naph.")


if __name__ == "__main__":
    main()