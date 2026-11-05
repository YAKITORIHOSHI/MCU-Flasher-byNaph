"""Give a top-level window the application's icon on the Windows taskbar.

The process-wide AppUserModelID groups windows but does not say what the group looks
like. With no shortcut carrying that ID, Windows 11 can settle on its generic application
icon while the title bar is correct, because the taskbar does not use the title bar's icon.
The shell's per-window properties name the icon directly, and setting a window's ID is the
documented way to make the taskbar read them again. Other hosts do nothing here.
"""
from __future__ import annotations

import ctypes
import os
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QTimer

ROOT = Path(__file__).resolve().parents[2]
# The launcher and workspace use this same ID for their processes; a different one
# would split their windows into separate taskbar groups.
APP_USER_MODEL_ID = "naph.mcuflasher.gui.v3"
DISPLAY_NAME = "MCU Flasher by Naph"
# A first launch can build the button without the icon, so ask again once it exists.
REFRESH_DELAYS_MS = (500, 2000)

_VT_EMPTY = 0
_VT_LPWSTR = 31
# PKEY_AppUserModel_* property IDs, all in the one format below.
_RELAUNCH_COMMAND = 2
_RELAUNCH_ICON = 3
_RELAUNCH_NAME = 4
_ID = 5
# IPropertyStore vtable: IUnknown's three methods, then GetCount, GetAt, GetValue, SetValue.
_RELEASE_SLOT = 2
_SET_VALUE_SLOT = 6


class _Guid(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16), ("data3", ctypes.c_uint16),
                ("data4", ctypes.c_ubyte * 8)]


class _PropertyKey(ctypes.Structure):
    _fields_ = [("format", _Guid), ("id", ctypes.c_uint32)]


class _PropVariant(ctypes.Structure):
    # A type and three reserved words, then a union two pointers wide.
    _fields_ = [("type", ctypes.c_uint16), ("reserved", ctypes.c_uint16 * 3),
                ("text", ctypes.c_wchar_p), ("padding", ctypes.c_void_p)]


def _guid(text: str) -> _Guid:
    return _Guid.from_buffer_copy(uuid.UUID(text).bytes_le)


_PROPERTY_STORE_IID = _guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
_APP_MODEL_FORMAT = _guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3")
_FUNCTION = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_RELEASE = _FUNCTION(ctypes.c_ulong, ctypes.c_void_p)
_SET_VALUE = _FUNCTION(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant))


def _write_properties(hwnd: int, values: list[tuple[int, str | None]]) -> bool:
    """Store (property ID, text) pairs on the window in order; None removes a property."""
    try:
        opener = ctypes.windll.shell32.SHGetPropertyStoreForWindow
        opener.argtypes = (ctypes.c_void_p, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p))
        opener.restype = ctypes.c_long
        store = ctypes.c_void_p()
        if opener(hwnd, ctypes.byref(_PROPERTY_STORE_IID), ctypes.byref(store)) < 0 or not store:
            return False
        vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        try:
            set_value = _SET_VALUE(vtable[_SET_VALUE_SLOT])
            # SetValue stores immediately; Windows documents Commit as having no effect here.
            results = [set_value(store, ctypes.byref(_PropertyKey(_APP_MODEL_FORMAT, pid)),
                                 ctypes.byref(_PropVariant(type=_VT_LPWSTR if text else _VT_EMPTY, text=text)))
                       for pid, text in values]
        finally:
            _RELEASE(vtable[_RELEASE_SLOT])(store)
        return all(result >= 0 for result in results)
    except (AttributeError, OSError, ValueError, ctypes.ArgumentError):
        return False


def relaunch_command() -> str:
    """What Windows runs for a pinned button; the normal launcher, so setup checks still apply."""
    launcher = ROOT / "LAUNCHER" / "WINDOWS.exe"
    if launcher.is_file():
        return f'"{launcher}"'
    host = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
    return f'"{host}" //nologo "{ROOT / "direct" / "windows" / "run.vbs"}"'


def set_window_identity(hwnd: int, *, app_id: str = APP_USER_MODEL_ID, relaunch: bool = True) -> bool:
    values: list[tuple[int, str | None]] = []
    if relaunch:
        # Windows ignores either of these without the other.
        values += [(_RELAUNCH_COMMAND, relaunch_command()), (_RELAUNCH_NAME, DISPLAY_NAME)]
    values.append((_RELAUNCH_ICON, f"{ROOT / 'src' / 'assets' / 'mcu_icon.ico'},0"))
    # Setting the ID notifies the taskbar, so everything it should read is stored first.
    values.append((_ID, app_id))
    return _write_properties(hwnd, values)


def clear_window_identity(hwnd: int) -> bool:
    return _write_properties(hwnd, [(_ID, None), (_RELAUNCH_ICON, None), (_RELAUNCH_NAME, None), (_RELAUNCH_COMMAND, None)])


def window_shown(window, *, app_id: str = APP_USER_MODEL_ID, relaunch: bool = True) -> None:
    """Call from showEvent, which Qt sends after creating the native window and before showing it."""
    if sys.platform != "win32":
        return
    hwnd = int(window.winId())
    if getattr(window, "_taskbar_hwnd", 0) == hwnd:
        return  # Restored from a minimized state.
    window._taskbar_hwnd = hwnd
    if not set_window_identity(hwnd, app_id=app_id, relaunch=relaunch):
        print(f"[MCU Flasher] Windows refused the taskbar properties for window {hwnd}", file=sys.stderr)

    def refresh() -> None:
        if window.isVisible() and getattr(window, "_taskbar_hwnd", 0) == hwnd:
            window.setWindowIcon(window.windowIcon())
            set_window_identity(hwnd, app_id=app_id, relaunch=relaunch)

    for delay in REFRESH_DELAYS_MS:
        QTimer.singleShot(delay, window, refresh)


def window_hidden(window, event) -> None:
    """Call from hideEvent. Windows requires a window's properties removed before it closes."""
    if sys.platform != "win32" or event.spontaneous():
        return  # A spontaneous hide is the user minimizing; the button stays.
    hwnd = getattr(window, "_taskbar_hwnd", 0)
    if hwnd:
        window._taskbar_hwnd = 0
        clear_window_identity(hwnd)
