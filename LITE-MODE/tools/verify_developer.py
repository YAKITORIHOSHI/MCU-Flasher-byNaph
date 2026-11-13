#!/usr/bin/env python3
"""Reuse the real portal with mocked accounts, network, vault and child launches."""
from pathlib import Path
import json
import subprocess
import argparse
import os
import platform
import sys
import tempfile
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from isolation import child_setup, environment
from worker import ORIGINAL, Worker, snapshot_engine


def child_fixture():
    """Run the real helper entry point, replacing only account/device boundaries."""
    state = Path(os.environ["MCU_LITE_STATE"]).resolve()
    if not state.is_relative_to((ROOT / "temp").resolve()):
        raise RuntimeError("Helper fixture requires owned Lite temp storage")
    sys.path.insert(0, str(state / "engine"))
    child_setup()
    from PySide6.QtCore import QTimer, Qt, qVersion
    from PySide6.QtWidgets import QApplication
    from main.core.credential_store import CredentialStore
    from main.core.owner_tickets import OwnerTicketService
    import developer
    import faulthandler
    trace = (state / "helper-stack.txt").open("w")
    faulthandler.dump_traceback_later(6, file=trace)
    service = Mock(spec=OwnerTicketService)
    service.get_config.return_value = {"owner_email": "developer@example.com", "use_firebase": False}
    service.restore_session.return_value = (False, "No saved fixture session")
    service.check_connection.return_value = (False, "Offline fixture")
    service.is_cloud_authenticated = False
    service.get_tickets.return_value = []
    service.get_ticket_categories.return_value = ["General Defect"]
    original_create = developer.create_dialog

    def create(state):
        dialog = original_create(state, service_factory=lambda: service)
        native = os.environ.get("LITE_VERIFY_NATIVE") == "1"
        if native and os.environ.get("GITHUB_ACTIONS") != "true":
            raise RuntimeError("Native fixtures are restricted to an isolated Actions runner")
        if not native:
            dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        original_show = dialog.showNormal
        def activate():
            original_show()
            def report():
                dialog.grab().save(str(state / "helper-native.png"))
                (state / "helper-verification.json").write_text(json.dumps({
                    "activated": True, "auth_gate": dialog.stack.currentWidget() == dialog.auth_page,
                    "width": dialog.auth_card.width(), "qt": qVersion(), "platform": sys.platform,
                    "qpa": QApplication.platformName(), "native": native,
                    "os": platform.platform(), "python": sys.version,
                    "active": QApplication.activeWindow() is dialog,
                }))
            QTimer.singleShot(400, report)
        dialog.showNormal = activate
        QTimer.singleShot(15000, QApplication.instance().quit)
        return dialog

    with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Fixture network refused")), \
            patch.object(CredentialStore, "get", side_effect=AssertionError("Fixture vault refused")), \
            patch.object(CredentialStore, "set", side_effect=AssertionError("Fixture vault refused")), \
            patch("psutil.cpu_count", return_value=4), patch.object(developer, "create_dialog", create):
        try:
            return developer.main()
        finally:
            faulthandler.cancel_dump_traceback_later()
            trace.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-dir", type=Path)
    args = parser.parse_args()
    (ROOT / "temp").mkdir(exist_ok=True)
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
            real_popen = subprocess.Popen
            child = None
            try:
                def launch_fixture(command, **options):
                    nonlocal child
                    check(Path(command[-1]) == ROOT / "python/developer.py", "entry starts the dedicated helper")
                    if os.environ.get("LITE_VERIFY_NATIVE") == "1" and sys.platform == "win32":
                        options["env"]["QT_QPA_PLATFORM"] = "windows"
                    child = real_popen([sys.executable, "-B", str(Path(__file__).resolve()), "--child"], **options)
                    return child
                with patch("worker.subprocess.Popen", side_effect=launch_fixture) as launch:
                    worker.dispatch({"cmd": "developer_access"})
                    deadline = time.monotonic() + 20
                    while not worker.developer_ready and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(.02)
                    check(worker.developer_ready, "real helper process acknowledges its visible portal")
                    worker.api.is_busy = True
                    worker.dispatch({"cmd": "developer_access"})
                    worker.dispatch({"cmd": "developer_activate"})
                    worker.api.is_busy = False
                    check(launch.call_count == 1, "repeated gestures reuse one live helper even during a build")
                    check(launch.call_args.kwargs["env"]["MCU_LITE_STATE"] == str(state), "helper inherits Lite isolation")
                    report = state / "helper-verification.json"
                    while not report.is_file() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(.02)
                    data = json.loads(report.read_text())
                    check(data["activated"] and data["auth_gate"] and data["width"] >= 350,
                          "literal activation reaches the real responsive authentication dialog")
                    if args.render_dir:
                        (args.render_dir / "helper-verification.json").write_text(json.dumps(data, indent=2))
                        import shutil
                        shutil.copy2(state / "helper-native.png", args.render_dir / "helper-native.png")
                    child.stdin.write(b"close\n")
                    child.stdin.flush()
                    check(child.wait(timeout=10) == 0, "helper accepts graceful close and exits cleanly")
                with patch("worker.subprocess.Popen", side_effect=OSError("fixture launch failure")):
                    try:
                        worker.dispatch({"cmd": "developer_access"})
                    except OSError:
                        check(not worker.busy(), "failed portal launch leaves build controls available")
                    else:
                        raise AssertionError("Launch failure was not reported")
            finally:
                if child is not None and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)
                if args.render_dir:
                    import shutil
                    for name in ("developer-access.log", "helper-stack.txt"):
                        if (state / name).is_file():
                            shutil.copy2(state / name, args.render_dir / name)
                worker.close()
                worker.protected[:] = [ORIGINAL]
    print(f"\n{len(checks)} Developer Access checks passed. No real network, vault or device access; native windows only on isolated CI.")


if __name__ == "__main__":
    if "--child" in sys.argv:
        raise SystemExit(child_fixture())
    main()
