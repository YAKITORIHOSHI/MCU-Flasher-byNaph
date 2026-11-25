"""One taskbar, Dock or panel icon for every window of this application.

Desktops group windows by an identity each process declares. Resources and the
sample viewer run in their own processes, so each must declare the same identity as
the workspace; otherwise they appear as a second, generic icon. Uses no GUI toolkit.
"""
from __future__ import annotations

import os
import sys

APPLICATION_NAME = "MCU Flasher by Naph"   # X11 WM_CLASS; StartupWMClass in the menu entry.
DESKTOP_FILE = "mcu-flasher"               # Wayland app ID; the menu entry's file name.
APP_USER_MODEL_ID = "naph.mcuflasher.gui.v3"  # Windows; the same as main/qt/taskbar.py.


def claim_process() -> None:
    """Call before the first window exists (before QApplication on macOS)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except (AttributeError, OSError):
            pass
    elif sys.platform == "darwin":
        # Qt would otherwise promote this helper to an application with its own Dock icon.
        os.environ.setdefault("QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM", "1")


def claim_application(app) -> None:
    """Name a helper's QApplication (PySide6 or PyQt5) like the workspace."""
    app.setApplicationName(APPLICATION_NAME)
    if sys.platform.startswith("linux"):
        app.setDesktopFileName(DESKTOP_FILE)
    elif sys.platform == "darwin":
        _share_dock_icon()


def _share_dock_icon() -> None:
    """Windows without a Dock icon: the workspace's icon stands for all of them."""
    try:
        import ctypes
        import ctypes.util
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "/usr/lib/libobjc.dylib")
        objc.objc_getClass.restype = objc.sel_registerName.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = objc.sel_registerName.argtypes = (ctypes.c_char_p,)
        send = objc.objc_msgSend
        send.restype, send.argtypes = ctypes.c_void_p, (ctypes.c_void_p, ctypes.c_void_p)
        application = send(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
        if not application:
            return
        policy = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(
            ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value)
        accessory = 1  # NSApplicationActivationPolicyAccessory: windows and focus, no Dock icon.
        policy(application, objc.sel_registerName(b"setActivationPolicy:"), accessory)
        activate = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool)(
            ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value)
        activate(application, objc.sel_registerName(b"activateIgnoringOtherApps:"), True)
    except (AttributeError, OSError, ValueError, TypeError):
        pass  # A second Dock icon is cosmetic; the window itself still works.
