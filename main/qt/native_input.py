"""Return input from an owned foreign HWND on an explicit Qt input event.

No polling, foreground activation, keyboard hooks or global Qt event filters.
SetParent can leave Qt's focus widget unchanged while another process's input
queue receives keystrokes, so a mouse press must count even without FocusIn.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                *[(name, wintypes.HWND) for name in
                  ("hwndActive", "hwndFocus", "hwndCapture", "hwndMenuOwner",
                   "hwndMoveSize", "hwndCaret")], ("rcCaret", wintypes.RECT)]


def _native_api():
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo)]
    user.GetGUIThreadInfo.restype = wintypes.BOOL
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user.SetFocus.argtypes = [wintypes.HWND]
    user.SetFocus.restype = wintypes.HWND
    kernel.GetCurrentThreadId.restype = wintypes.DWORD
    return user, kernel


def handoff_owned_input(destination, embedded_roots, *, api=None):
    """Move focus only from one of this workspace's verified embedded windows."""
    if not destination or not embedded_roots or (sys.platform != "win32" and api is None):
        return False
    try:
        user, kernel = api or _native_api()
        if not user.IsWindow(destination):
            return False
        destination_pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(destination, ctypes.byref(destination_pid))
        if destination_pid.value != os.getpid():
            return False
        info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
        if not user.GetGUIThreadInfo(0, ctypes.byref(info)) or not info.hwndFocus:
            return False
        focus = int(info.hwndFocus)
        roots = []
        for candidate in embedded_roots:
            root, expected_pid = candidate if isinstance(candidate, tuple) else (candidate, None)
            if not root or not user.IsWindow(int(root)):
                continue
            if expected_pid is not None:
                actual_pid = wintypes.DWORD()
                user.GetWindowThreadProcessId(int(root), ctypes.byref(actual_pid))
                if actual_pid.value != expected_pid:
                    continue
            roots.append(int(root))
        if not any(focus == root or user.IsChild(root, focus) for root in roots):
            return False
        if any(destination == root or user.IsChild(root, destination) for root in roots):
            return False
        current = kernel.GetCurrentThreadId()
        source = user.GetWindowThreadProcessId(focus, None)
        attached = source != current
        if attached and not user.AttachThreadInput(current, source, True):
            return False
        try:
            user.SetFocus(destination)
            actual = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
            moved = bool(user.GetGUIThreadInfo(current, ctypes.byref(actual)) and
                         (actual.hwndFocus == destination or
                          (actual.hwndFocus and user.IsChild(destination, actual.hwndFocus))))
        finally:
            if attached:
                user.AttachThreadInput(current, source, False)
        return moved
    except (OSError, AttributeError, TypeError, ValueError):
        return False


def return_input_to_qt(owner, widget):
    if sys.platform != "win32":
        return False
    ancestor = widget
    while ancestor is not None:
        if ancestor.property("mcuForeignInput"):
            return False
        ancestor = ancestor.parentWidget()
    roots = []
    for name, attribute in (("_ai_panel", "_ai_hwnd"), ("_terminal_panel", "_term_hwnd")):
        panel = getattr(owner, name, None)
        process = getattr(panel, "_proc", None)
        if process is not None:
            roots.append((getattr(panel, attribute, None), process.pid))
    # effectiveWinId uses an existing native ancestor; winId would create one.
    target = widget.focusProxy() or widget
    return handoff_owned_input(int(target.effectiveWinId() or 0), roots)
