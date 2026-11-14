#!/usr/bin/env python3
"""Generate a movable folder shortcut and an optional fixed Applications entry."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

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
        location = ()
    else:
        command = "/bin/bash " + exec_argument(str(root / "direct/ubuntu/run.sh")) + " --desktop"
        location = ("Path=" + string_value(str(root)),)
    return "\n".join((
        "[Desktop Entry]", "Version=1.0", "Type=Application",
        "Name=MCU Flasher", "Comment=Compile, flash and monitor microcontrollers",
        "Exec=" + command, *location, "Icon=mcu-flasher",
        "Terminal=false", "Categories=Development;Electronics;",
        "StartupNotify=true", "StartupWMClass=MCU Flasher by Naph", "Actions=Repair;", "",
        "[Desktop Action Repair]", "Name=Repair Ubuntu runtime",
        "Exec=" + command + " --repair", "",
    ))


# Lite registers under its Wayland app ID, so GNOME pairs its window with this entry.
LITE_ENTRY = "dev.naph.mcuflasher.lite.desktop"
LITE_ICON = "mcu-flasher-lite"


def lite_desktop_entry(root: Path) -> str:
    """All Apps entry for the separate Lite workbench in this application folder."""
    lite = root / "LITE-MODE"
    return "\n".join((
        "[Desktop Entry]", "Version=1.0", "Type=Application",
        "Name=MCU Flasher Lite", "GenericName=MCU Lite",
        "Comment=Lightweight compile, upload and serial monitor workbench",
        "Exec=/bin/bash " + exec_argument(str(lite / "run.sh")),
        "Path=" + string_value(str(lite)), "Icon=" + LITE_ICON,
        "Terminal=false", "Categories=Development;Electronics;",
        "Keywords=MCU;Lite;Arduino;Serial;Upload;",
        "StartupNotify=true", "StartupWMClass=dev.naph.mcuflasher.lite", "",
    ))


def register_lite(root: Path, data_home: Path) -> bool:
    """Register Lite in All Apps when this folder carries its Ubuntu launcher."""
    lite = root / "LITE-MODE"
    if not (lite / "run.sh").is_file() or not (lite / "assets/lite.png").is_file():
        return False
    install_icon(data_home, LITE_ICON, (lite / "assets/lite.png").read_bytes())
    write_if_changed(data_home / "applications" / LITE_ENTRY,
                     lite_desktop_entry(root).encode("utf-8"), 0o755)
    return True


def install_icon(data_home: Path, name: str, contents: bytes) -> bool:
    """Install a 256-pixel hicolor app icon so the running desktop shows it.

    GTK (and GNOME Shell's app grid and dock) notices new icons only when the
    theme directory's own timestamp changes, not a size subfolder's. Like
    xdg-icon-resource, bump it after a real change so the icon appears without
    logging out; unchanged registrations leave timestamps alone."""
    theme = data_home / "icons/hicolor"
    changed = write_if_changed(theme / f"256x256/apps/{name}.png", contents, 0o644)
    if changed:
        os.utime(theme)
    return changed


def write_if_changed(path: Path, contents: bytes, mode: int) -> bool:
    """Keep icon/theme directory timestamps stable on repeated registration.
    Returns whether the file's contents were replaced."""
    if path.is_symlink():
        raise OSError("A registered application file must not be a symbolic link")
    changed = not path.is_file() or path.read_bytes() != contents
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".mcu-registration-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(contents)
            temporary.chmod(mode)
            temporary.replace(path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    if path.stat().st_mode & 0o777 != mode:
        path.chmod(mode)
    return changed


def main(argv=None) -> int:
    from src.modules.ubuntu_release import enforce_supported_ubuntu
    if not enforce_supported_ubuntu():
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Also register in your Applications menu")
    args = parser.parse_args(argv)
    if not (ROOT / "direct/ubuntu/run.sh").is_file():
        parser.error("Keep direct/, main/ and src/ together in the application folder")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    if not data_home.is_absolute():
        parser.error("XDG_DATA_HOME must be an absolute path")
    # A theme name has no stale checkout path when the folder moves. Register
    # the existing app artwork for this account even for a folder-only shortcut.
    # PNG is supported by the desktop icon standard; ICO is Windows-specific.
    try:
        icon = (ROOT / "src/assets/mcu_icon.png").read_bytes()
    except OSError as exc:
        print(f"Cannot read MCU Flasher's application icon: {exc}", file=sys.stderr)
        return 1
    entries = [(ROOT / "MCU Flasher.desktop", desktop_entry(ROOT, portable=True))]
    if args.install:
        entries.append((data_home / "applications/mcu-flasher.desktop", desktop_entry(ROOT)))
    try:
        install_icon(data_home, "mcu-flasher", icon)
        for path, text in entries:
            write_if_changed(path, text.encode("utf-8"), 0o755)
            print(f"Shortcut created: {path}")
        if args.install and register_lite(ROOT, data_home):
            print(f"Shortcut created: {data_home / 'applications' / LITE_ENTRY}")
    except OSError as exc:
        print(f"Cannot register MCU Flasher's icon or shortcuts: {exc}", file=sys.stderr)
        return 1
    print("If Ubuntu asks, right-click the shortcut and choose Allow Launching.")
    print("Keep the folder shortcut beside the app; regenerate the Applications entry after moving it.")
    print("On another computer or account, generate shortcuts again to register the app icon there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
