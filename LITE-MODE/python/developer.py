"""Owned Developer Access window, reusing the main portal and security service."""
from pathlib import Path
import json
import os
import sys
import threading
import time

LITE = Path(__file__).resolve().parents[1]


def portal_is_visible(dialog):
    """A shown Qt object alone cannot prove Windows showed its native HWND."""
    from PySide6.QtCore import Qt
    if not dialog.isVisible() or dialog.isMinimized():
        return False
    # Owned headless fixtures deliberately suppress their native surface.
    if dialog.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
        return True
    handle = dialog.windowHandle()
    if handle is None or not handle.isVisible():
        return False
    if sys.platform == "win32":
        import ctypes
        visible = ctypes.windll.user32.IsWindowVisible
        visible.argtypes = (ctypes.c_void_p,)
        visible.restype = ctypes.c_int
        return bool(visible(int(dialog.winId())))
    return True


def create_dialog(state, *, service_factory=None):
    from main.core import credential_store, owner_tickets
    from main.qt import owner_ticket_dialog
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFontDatabase, QIcon
    # Separate credentials and local state, with the same provider/auth rules.
    credential_store.CredentialStore.SERVICE = "MCUFlasher.Lite.Developer.v1"
    storage = Path(state) / "developer-access"
    storage.mkdir(parents=True, exist_ok=True)
    owner_tickets.get_owner_portal_storage_dir = lambda: storage
    if service_factory is None:
        service_type = owner_tickets.OwnerTicketService
        service_factory = lambda: service_type(storage_dir=storage)
    owner_ticket_dialog.OwnerTicketService = service_factory
    for font in (LITE / "assets/fonts").glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(font))
    dialog = owner_ticket_dialog.OwnerTicketDialog()
    # Startup can finish after the user has switched applications. First show
    # stays visible without activation; Lite grants focus to the verified PID
    # before sending the explicit activate command.
    dialog.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    dialog.setWindowTitle("Developer Access / MCU Flasher Lite")
    dialog.setWindowIcon(QIcon(str(LITE / "assets/lite.ico")))
    return dialog


def main():
    protocol = sys.stdout
    # Keep the helper's one-way readiness protocol separate from Qt/service logs.
    sys.stdout = sys.stderr
    from isolation import child_setup, protect_original, state_directory
    from worker import host_check
    host_check()
    state = state_directory()
    sys.path.insert(0, str(state / "engine"))
    child_setup()
    protect_original(LITE.parent)
    from PySide6.QtCore import QObject, Signal, QTimer, Qt
    from PySide6.QtWidgets import QApplication
    from main.qt.garbage_collection import install_gui_garbage_collector
    app = QApplication.instance() or QApplication([])
    install_gui_garbage_collector(app)
    app.setStyle("Fusion")
    app.setApplicationName("MCU Flasher Lite Developer Access")
    app.setDesktopFileName("dev.naph.mcuflasher.lite")
    dialog = create_dialog(state)
    # This portal normally belongs to the main workspace. As a standalone
    # helper, its accepted close/reject must also finish the Qt event loop.
    dialog.finished.connect(app.quit)

    class Controls(QObject):
        requested = Signal(str)

    controls = Controls(app)

    def dispatch(command):
        if command == "activate":
            dialog.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            if dialog.isMinimized():
                dialog.showNormal()
            else:
                dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            dialog.auto_refresh()
        elif command == "close":
            dialog.close()

    controls.requested.connect(dispatch)

    def read_commands():
        # A buffered stdin read on a daemon holds its lock at interpreter
        # shutdown. Use the raw descriptor so closing the portal can exit cleanly.
        pending = bytearray()
        oversized = False
        try:
            while chunk := os.read(sys.stdin.fileno(), 64):
                for byte in chunk:
                    if byte == 10:
                        if not oversized and pending in (b"activate", b"close"):
                            controls.requested.emit(pending.decode())
                        pending.clear()
                        oversized = False
                    elif len(pending) < 64:
                        pending.append(byte)
                    else:
                        oversized = True
        except OSError:
            pass
        # A crashed/closed workspace must not dismiss an unsaved portal form.

    threading.Thread(target=read_commands, name="lite-developer-control", daemon=True).start()
    dialog.show()
    ready_timer = QTimer(app)
    ready_timer.setInterval(25)
    deadline = time.monotonic() + 15

    def announce_ready():
        if portal_is_visible(dialog):
            ready_timer.stop()
            protocol.write(json.dumps({"event": "ready", "pid": os.getpid()}) + "\n")
            protocol.flush()
        elif time.monotonic() >= deadline:
            ready_timer.stop()
            print("Developer Access did not become visible before its startup deadline.", file=sys.stderr)
            app.exit(1)

    ready_timer.timeout.connect(announce_ready)
    ready_timer.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
