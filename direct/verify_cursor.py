"""Hardware-free pointer recovery and focus preservation checks."""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget, QLineEdit, QVBoxLayout
from main.qt import cursor_visibility
from main.qt.cursor_visibility import WorkspacePointerGuard

APP = QApplication.instance() or QApplication([])


class CursorChecks(unittest.TestCase):
    def setUp(self):
        self.owner = QWidget()
        self.layout = QVBoxLayout(self.owner)
        self.text = QLineEdit(self.owner)
        self.layout.addWidget(self.text)
        self.host = QWidget(self.owner)
        self.host.setProperty("mcuTextPointer", True)
        self.proxy = QWidget(self.host)
        self.guard = WorkspacePointerGuard(self.owner)
        self.owner.show()
        self.owner.activateWindow()
        self.text.setFocus()
        APP.processEvents()

    def tearDown(self):
        APP.removeEventFilter(self.guard)
        self.owner.close()
        self.owner.deleteLater()
        APP.processEvents()

    def test_blank_pointer_restored_without_changing_focus(self):
        focused = APP.focusWidget()
        self.proxy.setCursor(Qt.CursorShape.BlankCursor)
        self.assertEqual(self.proxy.cursor().shape(), Qt.CursorShape.IBeamCursor)
        self.text.setCursor(Qt.CursorShape.BlankCursor)
        self.assertEqual(self.text.cursor().shape(), Qt.CursorShape.IBeamCursor)
        self.owner.setCursor(Qt.CursorShape.BlankCursor)
        self.assertEqual(self.owner.cursor().shape(), Qt.CursorShape.ArrowCursor)
        self.assertIs(APP.focusWidget(), focused)

    def test_preserves_link_resize_and_busy_pointers(self):
        for shape in (Qt.CursorShape.PointingHandCursor, Qt.CursorShape.SizeHorCursor,
                      Qt.CursorShape.WaitCursor, Qt.CursorShape.IBeamCursor):
            self.proxy.setCursor(shape)
            self.assertEqual(self.proxy.cursor().shape(), shape)
        self.assertIsNone(APP.overrideCursor())

    def test_other_windows_unchanged_but_owned_detached_window_covered(self):
        unrelated = QWidget()
        unrelated.setCursor(Qt.CursorShape.BlankCursor)
        self.assertEqual(unrelated.cursor().shape(), Qt.CursorShape.BlankCursor)
        if sys.platform.startswith("linux"):
            detached = self._assert_owned_detached_pointer_is_restored()
        else:
            # Exercise the Linux tree-scanner path on Windows too, where the
            # app-wide event filter would otherwise restore the cursor first.
            APP.removeEventFilter(self.guard)
            with patch.object(cursor_visibility.sys, "platform", "linux"):
                WorkspacePointerGuard(self.owner)
                detached = self._assert_owned_detached_pointer_is_restored()
        detached.close()
        unrelated.close()

    def _assert_owned_detached_pointer_is_restored(self):
        detached = QWidget(self.owner, Qt.WindowType.Window)
        child = QWidget(detached)
        child.setCursor(Qt.CursorShape.BlankCursor)
        # Ubuntu's guard defers scans until widget construction has returned.
        APP.processEvents()
        self.assertEqual(child.cursor().shape(), Qt.CursorShape.ArrowCursor)
        return detached

    def test_editor_restores_focus_without_reloading_or_changing_position(self):
        from main.qt.editor_panel import MonacoEditorPanel
        fake = Mock()
        fake.isVisible.return_value = True
        fake._view.isEnabled.return_value = True
        MonacoEditorPanel.restore_input_focus(fake)
        fake._view.setFocus.assert_called_once_with(Qt.FocusReason.OtherFocusReason)
        script = fake._view.page().runJavaScript.call_args.args[0]
        self.assertIn("editorInstance.focus()", script)
        self.assertNotIn("setModel", script)
        self.assertNotIn("setPosition", script)
        fake._view.load.assert_not_called()
        fake.isVisible.return_value = False
        fake._view.setFocus.reset_mock()
        MonacoEditorPanel.restore_input_focus(fake)
        fake._view.setFocus.assert_not_called()

    def test_delayed_upload_actions_reject_changed_target_or_new_operation(self):
        from main.qt.main_window import MCUMainWindow
        backend = SimpleNamespace(current_board="fixture", current_port="MOCK",
                                  sketch_dir_path=ROOT / "temp", is_busy=False)
        fake = SimpleNamespace(_backend=backend, _operation_generation=1,
                               _active_operation=None, _focus_serial_monitor=Mock(),
                               _post_upload_dtr_pulse=Mock())
        fake._post_upload_target = lambda: MCUMainWindow._post_upload_target(fake)
        target = fake._post_upload_target()
        MCUMainWindow._finish_upload_ui(fake, 1, target)
        fake._focus_serial_monitor.assert_called_once()
        fake._focus_serial_monitor.reset_mock()
        backend.current_port = "OTHER"
        MCUMainWindow._finish_upload_ui(fake, 1, target, reset=True)
        fake._post_upload_dtr_pulse.assert_not_called()
        backend.current_port = "MOCK"
        fake._operation_generation = 2
        MCUMainWindow._finish_upload_ui(fake, 1, target)
        fake._focus_serial_monitor.assert_not_called()
        backend.is_busy = True
        MCUMainWindow._finish_upload_ui(fake, 2, target, reset=True)
        fake._post_upload_dtr_pulse.assert_not_called()

    def test_catalog_partial_does_not_touch_controls_and_pending_is_latest_only(self):
        from main.qt.main_window import MCUMainWindow
        fake = SimpleNamespace(_pending_catalog=None, _apply_pending_catalog=Mock(),
                               _set_status_text=Mock())
        MCUMainWindow._on_catalog_updated(fake, {"partial": True, "batch": {"a": {}}})
        fake._apply_pending_catalog.assert_not_called()
        first, latest = {"boards": {"a": {}}}, {"boards": {"b": {}}}
        MCUMainWindow._on_catalog_updated(fake, first)
        MCUMainWindow._on_catalog_updated(fake, latest)
        self.assertIs(fake._pending_catalog, latest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
