"""CMD project directories and cleanup of this session's temporary UNC drive."""
from __future__ import annotations

import ctypes
import ntpath
from pathlib import Path
import tempfile

_STARTUP = '''@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
pushd "%MCU_TERMINAL_CWD%"
if errorlevel 1 (
  echo [MCU Flasher] Could not open the project folder. This terminal has stopped.
  exit /b 1
)
> "%MCU_TERMINAL_DRIVE_RECORD%" echo %CD:~0,2%
"%MCU_TERMINAL_SHELL%" /D /E:ON /V:OFF
set "MCU_TERMINAL_EXIT=%ERRORLEVEL%"
popd
exit /b %MCU_TERMINAL_EXIT%
'''


def _remote(drive):
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.c_ulong(len(buffer))
    query = ctypes.windll.mpr.WNetGetConnectionW
    query.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong))
    query.restype = ctypes.c_ulong
    status = query(drive, buffer, ctypes.byref(size))
    if status == 2250:  # ERROR_NOT_CONNECTED
        return None
    if status:
        raise OSError(status, "Could not inspect the terminal's network drive")
    return buffer.value


class CmdProjectDirectory:
    """A local startup script; the project path is expanded once as environment data.

    The supervising CMD performs popd on normal exit. Forced PTY close uses the
    recorded drive only if it was absent before spawn and still maps this share.
    Never cancel an existing drive or force-disconnect another process's files.
    """
    def __init__(self, target, executable, env):
        if any(char in target for char in ('"', '\r', '\n', '\0')):
            raise ValueError("Project path contains an invalid CMD directory character")
        self.share = ntpath.splitdrive(target)[0] if target.startswith("\\\\") else ""
        self.before = set()
        if self.share:
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                drive = letter + ":"
                try:
                    if _remote(drive) is not None:
                        self.before.add(drive)
                except OSError:
                    self.before.add(drive)  # Uncertain ownership is never ours.
        self.storage = tempfile.TemporaryDirectory(prefix="mcu-cmd-")
        self.root = Path(self.storage.name)
        self.record = self.root / "drive.txt"
        script = self.root / "project.cmd"
        script.write_bytes(_STARTUP.replace("\n", "\r\n").encode("ascii"))
        self.env = dict(env, MCU_TERMINAL_CWD=target, MCU_TERMINAL_SHELL=executable,
                        MCU_TERMINAL_DRIVE_RECORD=str(self.record))
        self.argv = [executable, "/D", "/E:ON", "/V:OFF", "/C", str(script)]
        self.cwd = str(self.root)
        if ntpath.splitdrive(self.cwd)[0].startswith("\\\\"):
            self.storage.cleanup()
            raise OSError("CMD needs a local temporary directory to open a network project")

    def close(self):
        try:
            if not self.share or not self.record.is_file():
                return
            drive = self.record.read_text(encoding="ascii").strip().upper()
            if len(drive) != 2 or drive[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" or drive[1] != ":" or drive in self.before:
                return
            remote = _remote(drive)
            if remote and ntpath.normcase(remote.rstrip("\\")) == ntpath.normcase(self.share.rstrip("\\")):
                cancel = ctypes.windll.mpr.WNetCancelConnection2W
                cancel.argtypes = (ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_int)
                cancel.restype = ctypes.c_ulong
                status = cancel(drive, 0, False)
                if status not in (0, 2250):
                    raise OSError(status, "The terminal's temporary network drive could not be released")
        finally:
            self.storage.cleanup()
