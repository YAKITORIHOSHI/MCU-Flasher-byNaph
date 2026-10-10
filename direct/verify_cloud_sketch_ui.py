#!/usr/bin/env python3
"""Isolated cloud account, project synchronization and compact Qt UI checks."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt, QEvent, QCoreApplication, QPoint, QRect, QTimer
from PySide6.QtWidgets import QApplication, QWidget, QDialog, QMessageBox, QLineEdit
from main.qt.cloud_sketch_panel import CloudSketchPanel, CloudSketchDialog
from main.qt.project_dialog import ProjectDialog
from main.qt.theme import build_stylesheet, register_fonts

APP = QApplication.instance() or QApplication([])
register_fonts()


class FakeService:
    def __init__(self, root):
        self.root = root
        self.authenticated = True
        self.saved = {}
        self.calls = []
        self.threads = []
        self.rows = [{"id": "fixture", "name": "Sensor sketch", "revision": 3,
                      "file_count": 2, "updated_at": 1791619200000}]
        self.list_error = False
        self.hold = None
        self._store = object()

    def record(self, method, *args, **kwargs):
        self.threads.append(threading.get_ident())
        self.calls.append((method, args, kwargs))
        if self.hold is not None:
            self.hold.wait(3)

    @property
    def configured(self):
        self.threads.append(threading.get_ident())
        return True

    @property
    def is_authenticated(self):
        return self.authenticated

    @property
    def account_info(self):
        return {"uid": "fixture-user", "email": "fixture@example.invalid", "remember_me": False}

    @property
    def secure_storage_status(self):
        self.threads.append(threading.get_ident())
        return True, "Encrypted operating system credential vault"

    def list_sketches(self):
        self.record("list")
        if self.list_error:
            raise ValueError("Fixture disconnected while refreshing")
        return self.rows

    def saved_login(self):
        self.record("saved")
        return self.saved

    def restore_session(self):
        self.record("restore_session")
        return self.authenticated

    def sign_in(self, email, password, **kwargs):
        self.record("sign_in", email, password, **kwargs)
        self.authenticated = password != "bad-password"
        if not self.authenticated:
            raise ValueError("Sign-in failed")
        return self.account_info

    create_account = sign_in

    def sign_out(self, **kwargs):
        self.record("sign_out", **kwargs)
        self.authenticated = False
        if kwargs.get("forget_saved"):
            self.saved = {}

    def delete_account(self, password):
        self.record("delete", password)
        self.authenticated = False
        self.rows = []

    def working_directory(self, sketch_id):
        return self.root / "cloud-copy"

    def pull_project(self, sketch_id, destination=None, revision=None, **kwargs):
        self.record("pull", sketch_id, destination, revision)
        folder = Path(destination or self.working_directory(sketch_id))
        folder.mkdir(exist_ok=True)
        guard = kwargs.get("source_guard")
        with guard([folder / "Sensor.ino"]) if guard else nullcontext():
            (folder / "Sensor.ino").write_text("// cloud snapshot\n", encoding="utf-8")
            link = {"schema": 1, "uid": "fixture-user", "sketch_id": "fixture", "revision": 3,
                    "files": ["Sensor.ino"], "provider": "fixture"}
            (folder / ".mcu_cloud_link.json").write_text(json.dumps(link), encoding="utf-8")
        return folder

    def push_project(self, folder, sketch_id, revision):
        self.record("push", folder, sketch_id, revision)
        return {"revision": 4}

    def upload_project(self, folder, name=None):
        self.record("upload", folder, name)
        return {"id": "fixture", "revision": 1}

    def list_revisions(self, sketch_id):
        self.record("history", sketch_id)
        return [{"revision": 3, "created_at": 1791619200000}, {"revision": 2, "created_at": 1791532800000}]


class CloudUIChecks(unittest.TestCase):
    def setUp(self):
        output = ROOT / "temp/audit/cloud-sketch-ui"
        output.mkdir(parents=True, exist_ok=True)
        self.output = output
        self.fixture = tempfile.TemporaryDirectory(prefix="cloud-ui-", dir=ROOT / "temp/audit")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.local = self.root / "local"
        self.local.mkdir()
        (self.local / "Sensor.ino").write_text("// local fixture\n", encoding="utf-8")
        self.service = FakeService(self.root)
        self.backend = SimpleNamespace(cloud_sketch_service=self.service, sketch_dir_path=self.local,
            is_busy=False, active_operation=None, _current_op_phase=None, _cloud_project_operation=False,
            modified_files={}, ai_review_manager=None, ai_watcher=None, emit=Mock(),
            open_project_window=Mock(return_value={"success": True}), refresh_cloud_project_link=Mock(),
            get_recent_projects=Mock(return_value=[]), get_default_project_parent=Mock(return_value=str(self.root)))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("main.qt.cloud_sketch_panel.load_cloud_configuration", return_value={}))
        self.stack.enter_context(patch("main.qt.cloud_sketch_panel.save_cloud_configuration", return_value=True))
        self.stack.enter_context(patch("main.core.config.get_open_projects", return_value=[]))
        self.stack.enter_context(patch("main.core.config.find_project_window", return_value=None))
        self.stack.enter_context(patch("main.core.config.get_theme_mode", return_value="default"))
        self.widgets = []
        self.addCleanup(self.cleanup_widgets)

    def cleanup_widgets(self):
        self.service.hold = None
        for widget in reversed(self.widgets):
            panel = getattr(widget, "panel", widget)
            if hasattr(panel, "dispose") and not panel._busy:
                panel.dispose()
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while not predicate():
            APP.processEvents()
            if time.monotonic() > deadline:
                self.fail("Cloud UI fixture did not finish")
            time.sleep(.003)
        APP.processEvents()

    def panel(self, parent=None):
        panel = CloudSketchPanel(self.backend, parent, service=self.service)
        self.widgets.append(panel)
        panel.resize(720, 650)
        panel.show()
        if not panel._initialized:
            panel._initialize()
        self.wait(lambda: not panel._busy)
        return panel

    def test_secure_storage_network_and_source_work_stays_off_gui(self):
        panel = self.panel()
        self.assertTrue(self.service.threads)
        self.assertNotIn(threading.get_ident(), self.service.threads)
        self.assertEqual(panel._sketches.count(), 1)
        self.assertEqual(panel._password.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(panel._api_key.echoMode(), QLineEdit.EchoMode.Password)
        self.assertFalse(panel._pull_btn.isEnabled())

    def test_failed_sign_in_clears_previous_private_sketch_list(self):
        panel = self.panel()
        panel._email.setText("fixture@example.invalid")
        panel._password.setText("bad-password")
        panel._sign_in()
        self.wait(lambda: not panel._busy)
        self.assertFalse(panel._state["authenticated"])
        self.assertEqual(panel._sketches.count(), 0)
        self.assertFalse(panel._open_btn.isEnabled())

    def test_login_literal_password_options_and_forget_flow(self):
        self.service.authenticated = False
        panel = self.panel()
        panel._email.setText("fixture@example.invalid")
        panel._password.setText("  fixture pass  ")
        panel._save_login.setChecked(True)
        panel._remember.setChecked(True)
        panel._sign_in()
        self.wait(lambda: not panel._busy)
        call = next(row for row in self.service.calls if row[0] == "sign_in")
        self.assertEqual(call[1][1], "  fixture pass  ")
        self.assertEqual(call[2], {"save_login": True, "remember_me": True})
        self.assertEqual(panel._password.text(), "")
        self.assertTrue(panel._management.isVisible())
        self.assertTrue(panel._push_btn.isVisible())
        panel._forget_login()
        self.wait(lambda: not panel._busy)
        self.assertTrue(next(row for row in self.service.calls if row[0] == "sign_out")[2]["forget_saved"])

    def test_auth_screens_are_separate_and_sketch_actions_require_sign_in(self):
        self.service.authenticated = False
        panel = self.panel()
        self.assertTrue(panel._login_fields.isVisible())
        self.assertFalse(panel._register_fields.isVisible())
        self.assertTrue(panel._create_btn.isVisible())
        self.assertFalse(panel._management.isVisible())
        self.assertFalse(panel._sync_actions.isVisible())
        for button in (panel._refresh_btn, panel._upload_btn, panel._open_btn,
                       panel._push_btn, panel._pull_btn, panel._history_btn):
            self.assertFalse(button.isVisible(), button.text())
        before = len(self.service.calls)
        panel._upload_local()
        panel._open_cloud()
        panel._push_current()
        panel._pull_current()
        panel._load_history()
        self.assertEqual(len(self.service.calls), before)
        panel._show_create_account()
        self.assertFalse(panel._login_fields.isVisible())
        self.assertTrue(panel._register_fields.isVisible())
        self.assertTrue(panel._register_btn.isVisible())
        self.assertTrue(panel._back_to_sign_in.isVisible())
        panel._register_email.setText("new@example.invalid")
        panel._register_password.setText("secret1")
        panel._register_confirm.setText("different")
        panel._create_account()
        self.assertIn("do not match", panel._status.text())
        self.assertEqual(len(self.service.calls), before)

    def test_open_uses_separate_window_and_preserves_existing_working_copy(self):
        panel = self.panel()
        panel._open_cloud()
        self.wait(lambda: not panel._busy)
        self.backend.open_project_window.assert_called_once_with(str(self.service.working_directory("fixture")))
        path = self.service.working_directory("fixture") / "Sensor.ino"
        path.write_text("// unsaved closed local changes\n")
        panel._open_cloud()
        self.wait(lambda: not panel._busy)
        self.assertEqual(sum(row[0] == "pull" for row in self.service.calls), 1)
        self.assertEqual(path.read_text(), "// unsaved closed local changes\n")

    def test_source_busy_save_ack_and_editor_freeze_are_scoped(self):
        parent = QWidget()
        self.widgets.append(parent)
        editor = QWidget(parent)
        editor._autosave_enabled = True
        editor._autosave_timer = QTimer(editor)
        editor._autosave_timer.start(1000)
        callbacks = []
        editor.trigger_save_all = lambda **kwargs: callbacks.append(kwargs)
        parent._editor_panel = editor
        self.backend.modified_files = {str(self.local / "Sensor.ino"): True}
        panel = self.panel(parent)
        panel._upload_local()
        self.assertTrue(panel._waiting_for_save)
        self.assertFalse(any(row[0] == "upload" for row in self.service.calls))
        self.backend.modified_files.clear()
        self.service.hold = threading.Event()
        callbacks[0]["callback"]()
        self.assertTrue(self.backend._cloud_project_operation)
        self.assertFalse(editor.isEnabled())
        self.assertFalse(editor._autosave_timer.isActive())
        self.assertFalse(editor._autosave_enabled)
        self.service.hold.set()
        self.wait(lambda: not panel._busy)
        self.assertFalse(self.backend._cloud_project_operation)
        self.assertTrue(editor.isEnabled())
        self.assertTrue(editor._autosave_enabled)
        self.backend.emit.assert_any_call("operation:phase", {"phase": "cloud_sync", "op": "cloud_sync", "is_busy": True, "can_stop": False})

    def test_busy_action_and_pending_review_refuse_source_mutation(self):
        panel = self.panel()
        self.backend.active_operation = "compile"
        panel._upload_local()
        self.assertFalse(any(row[0] == "upload" for row in self.service.calls))
        self.backend.active_operation = None
        self.backend.ai_review_manager = SimpleNamespace(has_any_pending_ai_edits=lambda: True)
        panel._upload_local()
        self.assertFalse(any(row[0] == "upload" for row in self.service.calls))
        self.assertIn("pending AI", panel._status.text())

    def test_history_revision_restore_and_push_revision_refresh(self):
        self.service.pull_project("fixture", destination=self.local)
        panel = self.panel()
        panel._push_current()
        self.wait(lambda: not panel._busy)
        call = next(row for row in self.service.calls if row[0] == "push")
        self.assertEqual(call[1], (str(self.local), "fixture", 3))
        self.backend.refresh_cloud_project_link.assert_called_once_with()
        panel._load_history()
        self.wait(lambda: not panel._busy)
        self.assertEqual(panel._revisions.count(), 2)
        panel._revisions.setCurrentIndex(1)
        with patch("main.qt.cloud_sketch_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            panel._restore_revision()
        self.wait(lambda: not panel._busy)
        pulls = [row for row in self.service.calls if row[0] == "pull"]
        self.assertEqual(pulls[-1][1][2], 2)

    def test_configuration_saves_only_to_injected_secure_store_off_gui(self):
        self.service.authenticated = False
        panel = self.panel()
        panel._api_key.setText("fixture-api-key")
        panel._database_url.setText("https://fixture-default-rtdb.firebaseio.com")
        panel._project_id.setText("fixture")
        captured = []
        with patch("main.qt.cloud_sketch_panel.save_cloud_configuration", side_effect=lambda *args:
                   captured.append((args, threading.get_ident()))):
            panel._save_configuration()
            self.wait(lambda: not panel._busy)
        self.assertEqual(captured[0][0][0]["firebase_project_id"], "fixture")
        self.assertIs(captured[0][0][1], self.service._store)
        self.assertNotEqual(captured[0][1], threading.get_ident())
        self.assertIn("encrypted credential vault", panel._status.text())

    def test_account_delete_requires_confirmation_and_literal_password(self):
        panel = self.panel()
        with patch("main.qt.cloud_sketch_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            panel._delete_account()
        self.assertFalse(any(row[0] == "delete" for row in self.service.calls))
        with patch("main.qt.cloud_sketch_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("main.qt.cloud_sketch_panel.QInputDialog.getText", return_value=(" fixture password ", True)):
            panel._delete_account()
        self.wait(lambda: not panel._busy)
        self.assertEqual(next(row for row in self.service.calls if row[0] == "delete")[1], (" fixture password ",))
        self.assertFalse(panel._state["authenticated"])
        self.assertEqual(panel._sketches.count(), 0)

    def test_successful_pull_survives_failed_list_refresh_and_waits_for_reload(self):
        self.service.pull_project("fixture", destination=self.local)
        parent = QWidget()
        self.widgets.append(parent)
        editor = QWidget(parent)
        editor._autosave_enabled = True
        editor._autosave_timer = QTimer(editor)
        editor._autosave_timer.start(1000)
        parent._editor_panel = editor
        reload_callbacks = []
        parent._on_cloud_project_pulled = lambda path, **kwargs: reload_callbacks.append((path, kwargs))
        panel = self.panel(parent)
        pulled = Mock()
        panel.project_pulled.connect(pulled)
        self.service.list_error = True
        with patch("main.qt.cloud_sketch_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            panel._pull_current()
        self.assertTrue(panel._busy, panel._status.text())
        self.wait(lambda: bool(reload_callbacks))
        self.assertTrue(panel._busy)
        self.assertTrue(self.backend._cloud_project_operation)
        self.assertFalse(editor.isEnabled())
        self.assertFalse(editor._autosave_enabled)
        pulled.assert_not_called()
        reload_callbacks[0][1]["failure_callback"]()
        self.assertEqual(panel._refresh_btn.text(), "Retry reload")
        self.assertTrue(panel._refresh_btn.isEnabled())
        panel._refresh()
        self.assertEqual(len(reload_callbacks), 2)
        reload_callbacks[1][1]["callback"]()
        self.assertFalse(panel._busy)
        self.assertFalse(self.backend._cloud_project_operation)
        self.assertTrue(editor.isEnabled())
        self.assertFalse(editor._autosave_timer.isActive())
        pulled.assert_called_once_with(str(self.local))
        self.assertIn("refresh failed", panel._status.text().lower())

    def test_busy_cloud_job_blocks_close_and_other_view_worker(self):
        dialog = CloudSketchDialog(self.backend)
        self.widgets.append(dialog)
        dialog.show()
        self.wait(lambda: not dialog.panel._busy)
        other = self.panel()
        self.service.hold = threading.Event()
        dialog.panel._refresh()
        dialog.reject()
        self.assertTrue(dialog.isVisible())
        other._refresh()
        self.assertIn("Another cloud operation", other._status.text())
        self.service.hold.set()
        self.wait(lambda: not dialog.panel._busy)
        dialog.reject()
        self.assertFalse(dialog.isVisible())

    def test_partial_pull_recovery_error_keeps_editor_frozen_until_disk_reload(self):
        self.service.pull_project("fixture", destination=self.local)
        parent = QWidget()
        self.widgets.append(parent)
        editor = QWidget(parent)
        editor._autosave_enabled = True
        editor._autosave_timer = QTimer(editor)
        parent._editor_panel = editor
        reload_callbacks = []
        parent._on_cloud_project_pulled = lambda path, **kwargs: reload_callbacks.append(kwargs)
        panel = self.panel(parent)
        from main.core.cloud_sketch_service import CloudRecoveryError
        error = CloudRecoveryError("Recovery copy remains in the user profile.", project_root=self.local, recovery_dir=self.root / "recovery")
        with patch.object(self.service, "pull_project", side_effect=error), \
                patch("main.qt.cloud_sketch_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            panel._pull_current()
            self.wait(lambda: bool(reload_callbacks))
        self.assertTrue(self.backend._cloud_project_operation)
        self.assertFalse(editor.isEnabled())
        reload_callbacks[0]["callback"]()
        self.assertFalse(self.backend._cloud_project_operation)
        self.assertTrue(editor.isEnabled())
        self.assertIn("Recovery copy", panel._status.text())

    def test_disposed_completion_does_not_open_window(self):
        panel = self.panel()
        self.service.hold = threading.Event()
        panel._open_cloud()
        panel.dispose()
        self.service.hold.set()
        self.wait(lambda: not panel._busy)
        self.backend.open_project_window.assert_not_called()

    def test_project_selector_cloud_tab_and_actual_folder_action(self):
        records = [{"folder": str(self.local), "current": True}]
        with patch("main.core.config.get_open_projects", return_value=records):
            dialog = ProjectDialog(self.backend)
        self.widgets.append(dialog)
        self.assertEqual(dialog._tabs.tabText(dialog._tabs.count() - 1), "Cloud")
        with patch("main.qt.project_dialog.QDesktopServices.openUrl", return_value=True) as opened:
            dialog._open_project_folder()
            self.assertEqual(Path(opened.call_args.args[0].toLocalFile()), self.local)
        dialog.show()
        dialog._tabs.setCurrentIndex(dialog._tabs.count() - 1)
        self.wait(lambda: not dialog._cloud_panel._busy)
        dialog._cloud_panel._open_cloud()
        self.wait(lambda: not dialog._cloud_panel._busy)
        self.assertTrue(dialog.cloud_window_opened)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertIsNone(dialog.selected_project)

    def test_startup_cloud_login_and_connection_sections_render_all_palettes(self):
        self.service.authenticated = False
        panel = self.panel()
        panel._configure_toggle.setChecked(False)
        for mode in ("default", "light", "solarized_dark"):
            APP.setStyleSheet(build_stylesheet(mode))
            panel.apply_theme(mode)
            panel.resize(720, 650)
            panel._scroll.verticalScrollBar().setValue(0)
            APP.processEvents()
            self.assertTrue(panel._email.isVisible())
            self.assertTrue(panel._password.isVisible())
            self.assertTrue(panel._save_login.isVisible())
            self.assertTrue(panel._remember.isVisible())
            self.assertTrue(panel.grab().save(str(self.output / f"{mode}-login.png")))
        panel._configure_toggle.setChecked(True)
        APP.processEvents()
        panel._scroll.ensureWidgetVisible(panel._configuration_card)
        APP.processEvents()
        self.assertTrue(panel.grab().save(str(self.output / "connection-fields.png")))
        panel._configure_toggle.setChecked(False)
        panel._show_create_account()
        APP.processEvents()
        self.assertTrue(panel._register_fields.isVisible())
        self.assertTrue(panel.grab().save(str(self.output / "create-account.png")))

    def test_palettes_compact_actions_and_sensitive_field_capture_masking(self):
        panel = self.panel()
        for mode in ("default", "light", "solarized_dark"):
            APP.setStyleSheet(build_stylesheet(mode))
            panel.apply_theme(mode)
            for width, height in ((720, 650), (400, 360), (330, 270)):
                panel.resize(width, height)
                APP.processEvents()
                for button in (panel._refresh_btn, panel._upload_btn, panel._open_btn, panel._push_btn, panel._pull_btn, panel._history_btn):
                    geometry = QRect(button.mapTo(panel, QPoint()), button.size())
                    self.assertTrue(panel.rect().contains(geometry), (mode, width, height, button.text(), geometry))
                    self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance(button.text()) + 16)
                self.assertEqual(panel._password.echoMode(), QLineEdit.EchoMode.Password)
                self.assertEqual(panel._api_key.echoMode(), QLineEdit.EchoMode.Password)
                self.assertTrue(panel.grab().save(str(self.output / f"{mode}-{width}-{height}.png")))
        panel._configure_toggle.setChecked(True)
        panel._scroll.verticalScrollBar().setValue(panel._scroll.verticalScrollBar().maximum())
        APP.processEvents()
        self.assertTrue(panel.grab().save(str(self.output / "connection-settings.png")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
