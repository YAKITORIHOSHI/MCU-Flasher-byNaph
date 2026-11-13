"""Owned Developer Access window, reusing the main portal and security service."""
from pathlib import Path
import sys
import threading

LITE = Path(__file__).resolve().parents[1]


def create_dialog(state, *, service_factory=None):
    from main.core import credential_store, owner_tickets
    from main.qt import owner_ticket_dialog
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
    dialog.setWindowTitle("Developer Access / MCU Flasher Lite")
    dialog.setWindowIcon(QIcon(str(LITE / "assets/lite.ico")))
    return dialog


def main():
    from isolation import child_setup, protect_original, state_directory
    from worker import host_check
    host_check()
    state = state_directory()
    sys.path.insert(0, str(state / "engine"))
    child_setup()
    protect_original(LITE.parent)
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setApplicationName("MCU Flasher Lite Developer Access")
    app.setDesktopFileName("dev.naph.mcuflasher.lite")
    dialog = create_dialog(state)

    class Controls(QObject):
        requested = Signal(str)

    controls = Controls(app)

    def dispatch(command):
        if command == "activate":
            dialog.showNormal()
            dialog.raise_()
            dialog.activateWindow()
            dialog.auto_refresh()
        elif command == "close":
            app.closeAllWindows()

    controls.requested.connect(dispatch)

    def read_commands():
        while line := sys.stdin.buffer.readline(64):
            if line in (b"activate\n", b"close\n"):
                controls.requested.emit(line.decode().strip())
        # A crashed/closed workspace must not dismiss an unsaved portal form.

    threading.Thread(target=read_commands, name="lite-developer-control", daemon=True).start()
    dialog.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
