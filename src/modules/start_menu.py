"""MCU Flasher's entry in the Windows Start menu, written by Setup.

Windows lists the shortcuts in this account's Start Menu\\Programs folder among its apps,
so with one there MCU Flasher is found by name wherever its folder is. The shortcut starts
the launcher in that folder (LAUNCHER\\WINDOWS.exe) with the app's icon, and carries the
app's AppUserModelID, so the taskbar shows the app's own name and icon for its windows.
Setup writes it each time it runs, so a moved folder is followed.

The shortcut is written in a child process (this file run as a script), so a shell that
misbehaves can only cost the entry, never Setup itself. Standard library only: Setup runs
it before anything else is installed. Other hosts do nothing.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
NAME = "MCU Flasher by Naph"
DESCRIPTION = "Compile and upload sketches to microcontroller boards"
# main.qt.taskbar's ID: the taskbar shows this shortcut's name and icon for those windows.
APP_USER_MODEL_ID = "naph.mcuflasher.gui.v3"
WRITE_SECONDS = 30

_FUNCTION = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_HRESULT = ctypes.c_long
_VT_LPWSTR = 31
_CLSCTX_INPROC_SERVER = 1
_COINIT_APARTMENTTHREADED = 2
_RPC_E_CHANGED_MODE = -2147417850       # 0x80010106: this thread already chose its apartment
# Methods by their place in each interface, counted after IUnknown's QueryInterface (0),
# AddRef (1) and Release (2).
_QUERY_INTERFACE, _RELEASE = 0, 2
_LINK_GET_PATH, _LINK_SET_DESCRIPTION, _LINK_SET_WORKING_DIRECTORY = 3, 7, 9      # IShellLinkW
_LINK_GET_ARGUMENTS, _LINK_SET_ARGUMENTS, _LINK_SET_ICON_LOCATION, _LINK_SET_PATH = 10, 11, 17, 20
_FILE_LOAD, _FILE_SAVE = 5, 6                                                     # IPersistFile
_STORE_GET_VALUE, _STORE_SET_VALUE, _STORE_COMMIT = 5, 6, 7                       # IPropertyStore


class _Guid(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16), ("data3", ctypes.c_uint16),
                ("data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _Guid:
    return _Guid.from_buffer_copy(uuid.UUID(text).bytes_le)


class _PropertyKey(ctypes.Structure):
    _fields_ = [("format", _Guid), ("id", ctypes.c_uint32)]


class _PropVariant(ctypes.Structure):
    # A type and three reserved words, then a union two pointers wide.
    _fields_ = [("type", ctypes.c_uint16), ("reserved", ctypes.c_uint16 * 3),
                ("text", ctypes.c_wchar_p), ("padding", ctypes.c_void_p)]


_SHELL_LINK = _guid("00021401-0000-0000-C000-000000000046")
_SHELL_LINK_IID = _guid("000214F9-0000-0000-C000-000000000046")
_PERSIST_FILE_IID = _guid("0000010B-0000-0000-C000-000000000046")
_PROPERTY_STORE_IID = _guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
_PROGRAMS_FOLDER = _guid("A77F5D77-2E2B-44C3-A6A2-ABA601054A51")       # FOLDERID_Programs
_APP_ID_FORMAT, _APP_ID = _guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5   # PKEY_AppUserModel_ID


def programs_folder() -> Path | None:
    """This account's Start Menu\\Programs folder, or None where there is none."""
    if sys.platform != "win32":
        return None
    try:
        known = ctypes.windll.shell32.SHGetKnownFolderPath
        known.argtypes = (ctypes.POINTER(_Guid), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        known.restype = _HRESULT
        found = ctypes.c_void_p()
        if known(ctypes.byref(_PROGRAMS_FOLDER), 0, None, ctypes.byref(found)) >= 0 and found.value:
            try:
                return Path(ctypes.wstring_at(found.value))
            finally:
                ctypes.windll.ole32.CoTaskMemFree(found)
    except (AttributeError, OSError, ValueError, ctypes.ArgumentError):
        pass
    appdata = os.environ.get("APPDATA", "").strip()
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" if appdata else None


def entry_path(folder: Path | None = None) -> Path | None:
    folder = folder or programs_folder()
    return folder / f"{NAME}.lnk" if folder else None


# ── The shell link, through COM ──────────────────────────────────────────────

def _method(pointer, slot, *argtypes):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = _FUNCTION(_HRESULT, ctypes.c_void_p, *argtypes)(vtable[slot])
    return lambda *args: function(pointer, *args)


def _check(result: int, what: str) -> None:
    if result < 0:
        raise OSError(f"{what} failed (0x{result & 0xFFFFFFFF:08X})")


def _release(pointer) -> None:
    if pointer:
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        _FUNCTION(ctypes.c_ulong, ctypes.c_void_p)(vtable[_RELEASE])(pointer)


def _query(pointer, iid: _Guid) -> ctypes.c_void_p:
    found = ctypes.c_void_p()
    _check(_method(pointer, _QUERY_INTERFACE, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p))(
        ctypes.byref(iid), ctypes.byref(found)), "QueryInterface")
    return found


class _Apartment:
    """COM for this thread, balanced on the way out when this call started it."""

    def __enter__(self):
        start = ctypes.windll.ole32.CoInitializeEx
        start.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        start.restype = _HRESULT
        result = start(None, _COINIT_APARTMENTTHREADED)
        self._owned = result >= 0
        if result < 0 and result != _RPC_E_CHANGED_MODE:
            _check(result, "CoInitializeEx")
        return self

    def __exit__(self, *_exc):
        if self._owned:
            ctypes.windll.ole32.CoUninitialize()


def _new_link() -> ctypes.c_void_p:
    create = ctypes.windll.ole32.CoCreateInstance
    create.argtypes = (ctypes.POINTER(_Guid), ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Guid),
                       ctypes.POINTER(ctypes.c_void_p))
    create.restype = _HRESULT
    link = ctypes.c_void_p()
    _check(create(ctypes.byref(_SHELL_LINK), None, _CLSCTX_INPROC_SERVER, ctypes.byref(_SHELL_LINK_IID),
                  ctypes.byref(link)), "Creating a shell link")
    return link


def write_link(link_path: Path, target: Path, icon: Path, working: Path, *, arguments: str = "",
               app_id: str = APP_USER_MODEL_ID, description: str = DESCRIPTION) -> None:
    """Write one shortcut, complete or not at all; raises OSError with the step that failed."""
    link_path = Path(link_path)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    staged = link_path.with_name(link_path.name + ".partial")
    with _Apartment():
        link = _new_link()
        store = persist = None
        try:
            text = ctypes.c_wchar_p
            _check(_method(link, _LINK_SET_PATH, text)(str(target)), "SetPath")
            _check(_method(link, _LINK_SET_ARGUMENTS, text)(arguments), "SetArguments")
            _check(_method(link, _LINK_SET_WORKING_DIRECTORY, text)(str(working)), "SetWorkingDirectory")
            _check(_method(link, _LINK_SET_DESCRIPTION, text)(description), "SetDescription")
            _check(_method(link, _LINK_SET_ICON_LOCATION, text, ctypes.c_int)(str(icon), 0), "SetIconLocation")
            store = _query(link, _PROPERTY_STORE_IID)
            key = _PropertyKey(_APP_ID_FORMAT, _APP_ID)
            value = _PropVariant(type=_VT_LPWSTR, text=app_id)
            _check(_method(store, _STORE_SET_VALUE, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant))(
                ctypes.byref(key), ctypes.byref(value)), "Setting the app ID")
            _check(_method(store, _STORE_COMMIT)(), "Commit")
            persist = _query(link, _PERSIST_FILE_IID)
            _check(_method(persist, _FILE_SAVE, text, ctypes.c_int)(str(staged), 1), "Save")
        finally:
            for pointer in (persist, store, link):
                _release(pointer)
    os.replace(staged, link_path)


def read_link(link_path: Path) -> dict:
    """A shortcut's target, arguments and app ID, as Windows reads them back."""
    with _Apartment():
        link = _new_link()
        store = persist = None
        try:
            persist = _query(link, _PERSIST_FILE_IID)
            _check(_method(persist, _FILE_LOAD, ctypes.c_wchar_p, ctypes.c_uint32)(str(link_path), 0), "Load")
            target = ctypes.create_unicode_buffer(32768)
            _check(_method(link, _LINK_GET_PATH, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)(
                target, len(target), None, 0), "GetPath")
            arguments = ctypes.create_unicode_buffer(32768)
            _check(_method(link, _LINK_GET_ARGUMENTS, ctypes.c_wchar_p, ctypes.c_int)(arguments, len(arguments)),
                   "GetArguments")
            store = _query(link, _PROPERTY_STORE_IID)
            key, value = _PropertyKey(_APP_ID_FORMAT, _APP_ID), _PropVariant()
            _check(_method(store, _STORE_GET_VALUE, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant))(
                ctypes.byref(key), ctypes.byref(value)), "Reading the app ID")
            try:
                app_id = value.text if value.type == _VT_LPWSTR else ""
            finally:
                ctypes.windll.ole32.PropVariantClear(ctypes.byref(value))
            return {"target": target.value, "arguments": arguments.value, "app_id": app_id}
        finally:
            for pointer in (persist, store, link):
                _release(pointer)


# ── Setup's call ─────────────────────────────────────────────────────────────

def ensure_entry(root: Path = ROOT, *, run=subprocess.run, folder: Path | None = None) -> tuple[bool, str]:
    """Write the Start menu entry for the app in ``root``; (written, what to log). Never raises."""
    if sys.platform != "win32" and run is subprocess.run:
        return True, ""
    launcher = Path(root) / "LAUNCHER" / "WINDOWS.exe"
    if not launcher.is_file():
        return False, f"The Start menu entry was not added: {launcher} is missing."
    link = entry_path(folder)
    if link is None:
        return False, "The Start menu entry was not added: this account has no Start menu folder."
    icon = Path(root) / "src" / "assets" / "mcu_icon.ico"
    command = [sys.executable, "-I", str(Path(__file__).resolve()), str(link), str(launcher),
               str(icon if icon.is_file() else launcher), str(root)]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        result = run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                     timeout=WRITE_SECONDS, **options)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, f"The Start menu entry could not be written: {exc}"
    if result.returncode:
        reason = (result.stderr or "").strip().splitlines()[-1:] or [f"exit {result.returncode}"]
        return False, f"The Start menu entry could not be written: {reason[0]}"
    return True, f"{NAME} is in the Start menu, so Windows Search finds it by name."


if __name__ == "__main__":
    try:
        if len(sys.argv) != 5:
            raise SystemExit("usage: start_menu.py LINK TARGET ICON WORKING_FOLDER")
        write_link(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
    except OSError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
