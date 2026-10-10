#!/usr/bin/env python3
"""Generate a movable folder shortcut and an optional fixed Applications entry."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# %k is supplied by the desktop launcher as one literal filename or URI argument.
# Only this fixed code becomes shell source; the checkout path never does. Use
# Bash builtins so even a missing system Python reaches run.sh's Bootstrap rescue.
# The shell entry also tolerates copies that have lost the native ELF's mode bit.
PORTABLE_LAUNCH_CODE = "\n".join((
    "fail_location() {",
    "    printf '%s\\n' 'MCU Flasher cannot locate its application folder. Keep this shortcut beside direct/, main/ and src/, or run bash direct/ubuntu/run.sh from that folder.' >&2",
    "    exit 1",
    "}",
    "location=${1-}",
    "if (( $# > 0 )); then shift; fi",
    "if [[ $location == file:* ]]; then",
    "    uri=${location#file:}",
    "    if [[ $uri == //* ]]; then",
    "        authority=${uri#//}",
    "        authority=${authority%%/*}",
    "        [[ -z $authority || ${authority,,} == localhost ]] || fail_location",
    '        uri=${uri#//"$authority"}',
    "    fi",
    "    [[ $uri == /* && $uri != *'?'* && $uri != *'#'* ]] || fail_location",
    "    decoded=",
    "    while [[ $uri == *%* ]]; do",
    "        decoded+=${uri%%\\%*}",
    "        uri=${uri#*\\%}",
    "        pair=${uri:0:2}",
    "        [[ $pair == [0-9A-Fa-f][0-9A-Fa-f] && $pair != 00 ]] || fail_location",
    '        printf -v byte \'%b\' "\\\\x$pair"',
    "        decoded+=$byte",
    "        uri=${uri:2}",
    "    done",
    "    location=$decoded$uri",
    "fi",
    "[[ $location == /* ]] || fail_location",
    'script="${location%/*}/direct/ubuntu/run.sh"',
    '[[ -f $script && -r $script ]] || fail_location',
    'exec /bin/bash "$script" --desktop "$@"',
))


def string_value(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\r", "\\r").replace("\t", "\\t"))


def exec_argument(value: str) -> str:
    # Desktop Entry escaping applies twice: string escaping, then Exec quoting.
    escaped = "".join("\\" + char if char in '\\"`$' else char for char in value)
    return '"' + string_value(escaped).replace("%", "%%") + '"'


def desktop_entry(root: Path, *, portable: bool = False) -> str:
    if portable:
        # Field codes must be outside quotes. Do not embed %k in shell source,
        # and do not use Path= with a stale checkout location before Exec runs.
        command = "/bin/bash -c " + exec_argument(PORTABLE_LAUNCH_CODE) + " mcu-flasher %k"
        location = ("Icon=applications-development",)
    else:
        command = "/bin/bash " + exec_argument(str(root / "direct/ubuntu/run.sh")) + " --desktop"
        location = ("Path=" + string_value(str(root)),
                    "Icon=" + string_value(str(root / "src/assets/mcu_icon.ico")))
    return "\n".join((
        "[Desktop Entry]", "Version=1.0", "Type=Application",
        "Name=MCU Flasher", "Comment=Compile, flash and monitor microcontrollers",
        "Exec=" + command, *location,
        "Terminal=false", "Categories=Development;Electronics;",
        "StartupNotify=true", "Actions=Repair;", "",
        "[Desktop Action Repair]", "Name=Repair Ubuntu runtime",
        "Exec=" + command + " --repair", "",
    ))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Also register in your Applications menu")
    args = parser.parse_args(argv)
    if not (ROOT / "direct/ubuntu/run.sh").is_file():
        parser.error("Keep direct/, main/ and src/ together in the application folder")
    entries = [(ROOT / "MCU Flasher.desktop", desktop_entry(ROOT, portable=True))]
    if args.install:
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        if not data_home.is_absolute():
            parser.error("XDG_DATA_HOME must be an absolute path")
        entries.append((data_home / "applications/mcu-flasher.desktop", desktop_entry(ROOT)))
    for path, text in entries:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        print(f"Shortcut created: {path}")
    print("If Ubuntu asks, right-click the shortcut and choose Allow Launching.")
    print("Keep the folder shortcut beside the app; regenerate the Applications entry after moving it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
