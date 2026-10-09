"""Drain an owned programmer pipe after its output reader could not run."""
from __future__ import annotations

import os
import subprocess
import sys


def _read_available(stream) -> bytes | None:
    """Read only available pipe bytes; never wait for a quiet programmer."""
    descriptor = stream.fileno()
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        import msvcrt
        available = wintypes.DWORD()
        peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
        peek.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                         wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
        peek.restype = wintypes.BOOL
        if not peek(wintypes.HANDLE(msvcrt.get_osfhandle(descriptor)), None, 0,
                    None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in (109, 232):  # Closed pipe.
                return b""
            raise ctypes.WinError(error)
        if not available.value:
            return None
        count = min(65536, available.value)
    else:
        import select
        if not select.select([descriptor], [], [], 0)[0]:
            return None
        count = 65536
    return os.read(descriptor, count)


def finish_with_output_drain(process, guard=None, *, on_poll=None) -> int:
    """Await the captured writer with no competing reader or output backpressure.

    The caller must confirm its former pipe reader is absent or finished. Quiet
    output never triggers termination; the transport guard decides USB loss.
    """
    readable = process.stdout is not None
    while process.poll() is None:
        drained = False
        if readable:
            try:
                for _ in range(4):
                    chunk = _read_available(process.stdout)
                    if not chunk:
                        if chunk == b"":
                            readable = False
                        break
                    drained = True
            except (OSError, ValueError, AttributeError):
                readable = False
        poll = guard.poll if guard is not None else on_poll
        if poll is not None:
            try:
                poll()
            except (ConnectionError, OSError, subprocess.TimeoutExpired):
                # Preserve the busy worker if termination is denied/delayed.
                if guard is not None:
                    return guard.finish()
                raise
        try:
            process.wait(timeout=0.005 if drained else 0.05)
        except subprocess.TimeoutExpired:
            continue
    return process.returncode
