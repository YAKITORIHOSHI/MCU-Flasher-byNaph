"""Cheap serial hotplug evidence for the port monitor; never opens a port.

A full scan reads USB metadata for every port. The monitor compares this signature
instead and runs the full scan only when it changes, so an unplugged or replugged
board is noticed quickly without asking SetupAPI or sysfs for everything each time.
"""
from __future__ import annotations

import os
import re
import sys

_LINUX_NODES = ("ttyUSB", "ttyACM", "ttyAMA", "ttyXRUSB", "ttySAC", "ttyGS", "ttyAP", "ttyS", "rfcomm")
_MAC_NODES = ("cu.", "tty.")
_USB_ID = re.compile(r"VID:PID=([0-9A-F]{4}:[0-9A-F]{4})", re.IGNORECASE)
_USB_SERIAL = re.compile(r"SER=(\S+)", re.IGNORECASE)


def signature(platform: str | None = None):
    """Names and node identities of serial devices, or None when they cannot be read.

    A replugged board keeps its name but gets a new device node, so POSIX entries
    include the inode: a replug between two polls is still a change.
    """
    platform = sys.platform if platform is None else platform
    try:
        if platform == "win32":
            import winreg
            entries = []
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
            except FileNotFoundError:
                return ()  # Windows removes the key while no serial port exists.
            with key:
                while True:
                    try:
                        name, value, _kind = winreg.EnumValue(key, len(entries))
                    except OSError:
                        break
                    entries.append((str(name), str(value)))
            return tuple(sorted(entries))
        prefixes = _MAC_NODES if platform == "darwin" else _LINUX_NODES
        entries = []
        with os.scandir("/dev") as nodes:
            for node in nodes:
                if node.name.startswith(prefixes):
                    entries.append((node.name, node.inode()))
        return tuple(sorted(entries))
    except OSError:
        return None


def node_identity(snapshot, device: str):
    """The inode this snapshot recorded for one POSIX device, if any."""
    name = os.path.basename(str(device or ""))
    return next((entry[1] for entry in snapshot or () if entry[0] == name), None)


def usb_identity(port) -> tuple[str, str] | None:
    """(VID:PID, serial number) from a port entry; None without USB evidence."""
    hwid = str((port or {}).get("hwid") or "")
    match = _USB_ID.search(hwid)
    if not match:
        return None
    serial = _USB_SERIAL.search(hwid)
    return match.group(1).upper(), serial.group(1).upper() if serial else ""


def same_device(identity, ports, candidates) -> str:
    """The one new port that is the same USB device, or "" when that is uncertain.

    A board can come back under another name (ttyACM1 for ttyACM0, or another COM
    number). A serial number decides; without one, only a single unambiguous match
    of the same model counts.
    """
    if not identity:
        return ""
    matches = [port["device"] for port in ports
               if port.get("device") in candidates and usb_identity(port) == tuple(identity)]
    return matches[0] if len(matches) == 1 else ""
