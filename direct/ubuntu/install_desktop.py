#!/usr/bin/env python3
"""Generate a shortcut for this checkout; optionally add it to the user's app menu."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def string_value(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\r", "\\r").replace("\t", "\\t"))


def exec_argument(value: str) -> str:
    # Desktop Entry escaping applies twice: string escaping, then Exec quoting.
    escaped = "".join("\\" + char if char in '\\"`$' else char for char in value)
    return '"' + string_value(escaped).replace("%", "%%") + '"'


def desktop_entry(root: Path) -> str:
    executable = root / "MCU_Flasher"
    return "\n".join((
        "[Desktop Entry]", "Version=1.0", "Type=Application",
        "Name=MCU Flasher", "Comment=Compile, flash and monitor microcontrollers",
        # GLib checks the executable token before expanding %% field escapes.
        # A fixed env executable keeps literal percent paths in an argument.
        "Exec=/usr/bin/env " + exec_argument(str(executable)),
        "Path=" + string_value(str(root)),
        "Icon=" + string_value(str(root / "src/assets/mcu_icon.ico")),
        "Terminal=false", "Categories=Development;Electronics;",
        "StartupNotify=true", "Actions=Repair;", "",
        "[Desktop Action Repair]", "Name=Repair Ubuntu runtime",
        "Exec=/usr/bin/env " + exec_argument(str(executable)) + " --repair", "",
    ))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Also register in your Applications menu")
    args = parser.parse_args(argv)
    if not (ROOT / "MCU_Flasher").is_file():
        parser.error("Build the Ubuntu executable with bash direct/ubuntu/build_launcher.sh first")
    paths = [ROOT / "MCU Flasher.desktop"]
    if args.install:
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        if not data_home.is_absolute():
            parser.error("XDG_DATA_HOME must be an absolute path")
        paths.append(data_home / "applications/mcu-flasher.desktop")
    text = desktop_entry(ROOT)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        print(f"Shortcut created: {path}")
    print("If Ubuntu asks, right-click the shortcut and choose Allow Launching.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
