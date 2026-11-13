#!/usr/bin/env python3
"""Reuse the real portal with mocked accounts, network, vault and child launches."""
from pathlib import Path
import io
import argparse
import os
import sys
import tempfile
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from isolation import child_setup, environment
from worker import ORIGINAL, Worker, snapshot_engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-dir", type=Path)
    args = parser.parse_args()
    checks = []

    def check(value, name):
        assert value, name
        checks.append(name)
        print("PASS", name, flush=True)

    with tempfile.TemporaryDirectory(dir=ROOT / "temp", prefix="developer-verify-") as temporary:
        state = Path(temporary) / "state"
        state.mkdir()
        for name in ("tmp", "data", "cache", "config"):
            (state / name).mkdir()
        engine = snapshot_engine(ORIGINAL, state)
        os.environ.update(environment(state))
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        sys.path.insert(0, str(engine))
        child_setup()
        from PySide6.QtWidgets import QApplication, QLineEdit
        from main.core.credential_store import CredentialStore
        from main.core.owner_tickets import OwnerTicketService
        import developer
        app = QApplication.instance() or QApplication([])
        app.setStyle("Fusion")
        service = Mock(spec=OwnerTicketService)
        service.get_config.return_value = {"owner_email": "developer@example.com", "use_firebase": False}
        service.restore_session.return_value = (False, "No saved fixture session")
        service.check_connection.return_value = (False, "Offline fixture")
        service.is_cloud_authenticated = False
        service.get_tickets.return_value = []
        service.get_ticket_categories.return_value = ["General Defect"]
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network refused by fixture")), \
                patch.object(CredentialStore, "get", side_effect=AssertionError("Real vault refused by fixture")), \
                patch.object(CredentialStore, "set", side_effect=AssertionError("Real vault refused by fixture")):
            dialog = developer.create_dialog(state, service_factory=lambda: service)
            dialog.setAttribute(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            dialog.show()
            deadline = time.monotonic() + 10
            while (dialog.service is None or dialog._task_name) and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.01)
            # The shared portal queues auth-card reflow after switching pages.
            # Let normal resize/layout events settle before inspecting pixels.
            for _ in range(20):
                app.processEvents()
                time.sleep(.01)
            check(dialog.service is service, "real portal initializes through the existing service adapter")
            check(dialog.auth_card.width() >= 350, "portal login card completes its queued responsive layout")
            check(dialog.stack.currentWidget() == dialog.auth_page, "five-click entry still requires the main authentication gate")
            check(dialog.txt_auth_pwd.echoMode() == QLineEdit.EchoMode.Password, "passwords stay masked in the reused portal")
            check(not dialog.windowIcon().isNull(), "Developer Access uses Lite's own native icon")
            check(CredentialStore.SERVICE == "MCUFlasher.Lite.Developer.v1", "saved credentials have a Lite-only vault namespace")
            check((state / "developer-access").is_dir(), "local portal state is private to Lite")
            check((engine / "src/assets/icons/developer-access-chip.svg").is_file(), "reused portal artwork is present in the private snapshot")
            if args.render_dir:
                args.render_dir.mkdir(parents=True, exist_ok=True)
                check(dialog.grab().save(str(args.render_dir / "developer-access.png")), "real Developer Access login renders with fixture accounts only")
            dialog.close()
            app.processEvents()
        with patch("serial.tools.list_ports.comports", return_value=[]):
            output = Mock()
            worker = Worker(state, output)
            process = Mock()
            process.poll.return_value = None
            process.wait.return_value = 0
            process.stdin = io.BytesIO()
            try:
                with patch("worker.subprocess.Popen", return_value=process) as launch:
                    worker.dispatch({"cmd": "developer_access"})
                    worker.api.is_busy = True
                    worker.dispatch({"cmd": "developer_access"})
                    worker.api.is_busy = False
                    check(launch.call_count == 1, "repeated gestures foreground one portal without starting duplicates")
                    check(process.stdin.getvalue() == b"activate\n", "existing portal receives only the literal activation command")
                    check(launch.call_args.args[0][-1].endswith("python/developer.py"), "entry launches the dedicated helper rather than the main workspace")
                    check(launch.call_args.kwargs["env"]["MCU_LITE_STATE"] == str(state), "portal child inherits Lite isolation")
                process.poll.return_value = 0
                with patch("worker.subprocess.Popen", side_effect=OSError("fixture launch failure")):
                    try:
                        worker.dispatch({"cmd": "developer_access"})
                    except OSError:
                        check(not worker.busy(), "a failed portal launch leaves build controls available")
                    else:
                        raise AssertionError("Launch failure was not reported")
            finally:
                worker.close()
                worker.protected[:] = [ORIGINAL]
    print(f"\n{len(checks)} Developer Access checks passed. No real network, vault, desktop or device access.")


if __name__ == "__main__":
    main()
