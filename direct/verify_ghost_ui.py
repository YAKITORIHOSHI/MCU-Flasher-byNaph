#!/usr/bin/env python3
"""Isolated Ghost Sketch save/prompt/autosave checks; no live project IO."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = ROOT / "temp/audit/ghost-ui-fixtures"
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QWidget, QMainWindow, QMessageBox
from main.qt.ghost_sketch import GhostSketchController

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def fixture_path(name=""):
    """Return native absolute paths without touching a live or synthetic project."""
    return str(FIXTURE_ROOT / name)


class GhostUIChecks(unittest.TestCase):
    def setUp(self):
        self.window = QWidget()
        self.window._backend = SimpleNamespace(
            is_ghost_sketch=True, sketch_dir_path=Path(fixture_path("GhostSketch")),
            save_ghost_project=Mock(return_value={"success": True, "project": {
                "path": fixture_path("SavedSketch"), "old_path": fixture_path("GhostSketch"),
                "operation": "ghost_saved", "preserve_buffers": True,
                "path_mapping": {fixture_path("GhostSketch/GhostSketch.ino"): fixture_path("SavedSketch/GhostSketch.ino")}}}),
            discard_ghost_project=Mock(return_value={"success": True}),
            release_saved_ghost=Mock(return_value={"success": True}),
            get_default_project_parent=lambda: fixture_path(), emit=Mock())
        self.window._is_busy = lambda: False
        self.editor = QWidget(self.window)
        self.editor._view = SimpleNamespace(page=lambda: SimpleNamespace(runJavaScript=Mock()))
        self.flush = []
        self.editor.trigger_save_all = lambda **kwargs: self.flush.append(kwargs)
        self.promotions = []
        self.editor.promote_ghost_paths = lambda payload, **kwargs: self.promotions.append((payload, kwargs))
        self.window._editor_panel = self.editor
        self.window._detached_window = None
        self.controller = GhostSketchController(self.window)
        self.addCleanup(self.window.deleteLater)

    def pump_until(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.005)
        self.assertTrue(condition())

    def test_cancel_location_keeps_project_and_buffers(self):
        with patch("main.qt.ghost_sketch.QFileDialog.getExistingDirectory", return_value=""):
            self.controller.request_save()
        self.assertEqual(self.flush, [])
        self.window._backend.save_ghost_project.assert_not_called()
        self.assertTrue(self.window.isEnabled())
        self.assertFalse(self.controller.is_saving)

    def test_acknowledged_flush_precedes_copy_and_renderer_ack_precedes_completion(self):
        done = Mock()
        with patch("main.qt.ghost_sketch.QFileDialog.getExistingDirectory", return_value=fixture_path("SavedSketch")):
            self.controller.request_save(callback=done)
        self.assertFalse(self.window.isEnabled())
        self.window._backend.save_ghost_project.assert_not_called()
        self.flush[0]["callback"]()
        self.pump_until(lambda: bool(self.promotions))
        done.assert_not_called()
        self.assertTrue(self.controller.is_saving)
        self.promotions[0][1]["callback"]()
        done.assert_called_once()
        self.assertTrue(self.window.isEnabled())
        self.assertFalse(self.controller.is_saving)
        self.window._backend.release_saved_ghost.assert_called_once_with(fixture_path("GhostSketch"))
        self.window._backend.discard_ghost_project.assert_not_called()

    def test_saved_ghost_release_reports_retained_ai_recovery_without_discarding(self):
        self.window._backend.release_saved_ghost.return_value = {
            'success':True, 'retained':True, 'recovery_path':fixture_path('OriginalGhost'),
            'warning':'Saved. Original Ghost Sketch retained with its AI edit recovery history.'}
        self.controller._release_project(fixture_path('OriginalGhost'), preserve_recovery=True)
        self.pump_until(lambda: bool(self.window._backend.emit.call_args_list))
        self.window._backend.release_saved_ghost.assert_called_once_with(fixture_path('OriginalGhost'))
        self.window._backend.discard_ghost_project.assert_not_called()
        event, payload = self.window._backend.emit.call_args.args
        self.assertEqual(event, 'notification')
        self.assertEqual(payload['type'], 'warning')
        self.assertIn('retained', payload['message'])

    def test_flush_failure_keeps_ghost_without_copy(self):
        failed = Mock()
        with patch("main.qt.ghost_sketch.QFileDialog.getExistingDirectory", return_value=fixture_path("SavedSketch")), \
                patch("main.qt.ghost_sketch.QMessageBox.warning"):
            self.controller.request_save(failure_callback=failed)
            self.flush[0]["failure_callback"]()
        self.window._backend.save_ghost_project.assert_not_called()
        self.assertTrue(self.window.isEnabled())
        failed.assert_called_once()

    def test_saved_project_warning_is_reported_after_ack_without_vetoing_success(self):
        done = Mock()
        self.window._backend.save_ghost_project.return_value['warning'] = 'Saved, but recent-project preferences could not be updated.'
        with patch('main.qt.ghost_sketch.QFileDialog.getExistingDirectory', return_value=fixture_path('SavedSketch')):
            self.controller.request_save(callback=done)
        self.flush[0]['callback']()
        self.pump_until(lambda: bool(self.promotions))
        self.window._backend.emit.assert_not_called()
        self.promotions[0][1]['callback']()
        done.assert_called_once()
        self.assertEqual(self.window._backend.emit.call_args.args, ('notification', {
            'title':'Ghost Sketch saved',
            'message':'Saved, but recent-project preferences could not be updated.',
            'type':'warning'}))

    def test_failed_promotion_locks_editor_and_explicit_save_retries_without_copy(self):
        failed, done = Mock(), Mock()
        with patch("main.qt.ghost_sketch.QFileDialog.getExistingDirectory", return_value=fixture_path("SavedSketch")):
            self.controller.request_save(failure_callback=failed)
        self.flush[0]["callback"]()
        self.pump_until(lambda: bool(self.promotions))
        self.window._backend.is_ghost_sketch = False
        self.window._backend.sketch_dir_path = Path(fixture_path('SavedSketch'))
        with patch("main.qt.ghost_sketch.QMessageBox.warning"):
            self.promotions[0][1]["failure_callback"]()
        failed.assert_called_once()
        self.assertTrue(self.window.isEnabled())
        self.assertFalse(self.editor.isEnabled())
        self.assertTrue(self.controller.needs_recovery)
        self.assertFalse(self.controller.is_saving)
        with patch("main.qt.ghost_sketch.QFileDialog.getExistingDirectory") as picker:
            self.controller.request_save(callback=done)
        picker.assert_not_called()
        self.assertEqual(self.window._backend.save_ghost_project.call_count, 1)
        self.assertEqual(len(self.promotions), 2)
        done.assert_not_called()
        self.promotions[1][1]['callback']()
        done.assert_called_once()
        self.assertFalse(self.controller.needs_recovery)
        self.assertTrue(self.editor.isEnabled())

    def test_copy_worker_error_notifies_before_failure_callback_once(self):
        self.window._backend.save_ghost_project.side_effect = OSError('owned fixture refused copy')
        notified = []
        with patch("main.qt.ghost_sketch.QFileDialog.getExistingDirectory", return_value=fixture_path("SavedSketch")), \
                patch("main.qt.ghost_sketch.QMessageBox.warning") as warning:
            self.controller.request_save(failure_callback=lambda: notified.append(warning.call_count))
            self.flush[0]['callback']()
            self.pump_until(lambda: bool(notified))
        self.assertEqual(notified, [1])
        self.assertFalse(self.controller.is_saving)
        self.assertTrue(self.editor.isEnabled())
        self.assertTrue(self.window.isEnabled())
        self.assertEqual(self.promotions, [])

    def test_normal_process_exit_finishes_owned_temporary_project_cleanup(self):
        (ROOT / 'temp/audit').mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='ghost-cleanup-exit-', dir=ROOT / 'temp/audit') as directory:
            sentinel = Path(directory) / 'cleanup-complete.txt'
            temporary_project = str(Path(directory) / 'owned/temporary/GhostSketch')
            program = '''
import sys, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QObject
from main.qt.ghost_sketch import GhostSketchController
def discard(path):
    time.sleep(.4)
    Path(sys.argv[2]).write_text(path, encoding='utf-8')
    return {'success':True}
workspace = QObject()
workspace._backend = SimpleNamespace(discard_ghost_project=discard, emit=lambda *args:None)
controller = GhostSketchController(workspace)
controller._release_project(sys.argv[3])
'''
            child = subprocess.run([sys.executable, '-B', '-c', program, str(ROOT), str(sentinel), temporary_project],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertTrue(sentinel.exists(), 'Normal process exit abandoned owned Ghost Sketch cleanup')
            self.assertEqual(sentinel.read_text(encoding='utf-8'), temporary_project)

    def close_probe(self):
        from main.qt.main_window import MCUMainWindow

        class Probe(QMainWindow):
            closeEvent = MCUMainWindow.closeEvent
            _operation_children_alive = lambda self: False

        probe = Probe()
        self.addCleanup(probe.deleteLater)
        probe._backend = self.window._backend
        probe._backend.is_busy = False
        probe._backend.active_operation = None
        probe._backend.stop_services = Mock()
        probe._save_geometry = Mock()
        probe._is_busy = lambda: False
        probe._ghost_sketch = self.controller
        probe._editor_panel = self.editor
        probe._detached_window = None
        self.controller.workspace = probe
        self.controller.setParent(probe)
        self.editor.setParent(probe)
        probe.show()
        return probe

    def test_actual_workspace_close_cancel_discard_and_save_acknowledgement(self):
        probe = self.close_probe()
        with patch('main.core.config.clean_instance_config'), \
                patch('src.modules.ubuntu_download_manager.quit_if_last_workspace'), \
                patch.object(self.controller, '_release_project') as release:
            with patch('main.qt.ghost_sketch.QMessageBox.question', return_value=QMessageBox.StandardButton.Cancel):
                event = QCloseEvent()
                probe.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertTrue(probe.isVisible())
            probe._backend.stop_services.assert_not_called()
            with patch('main.qt.ghost_sketch.QMessageBox.question', return_value=QMessageBox.StandardButton.Save), \
                    patch('main.qt.ghost_sketch.QFileDialog.getExistingDirectory', return_value=fixture_path('SavedSketch')):
                event = QCloseEvent()
                probe.closeEvent(event)
            self.assertFalse(event.isAccepted())
            probe._backend.stop_services.assert_not_called()
            self.flush[0]['callback']()
            self.pump_until(lambda: bool(self.promotions))
            probe._backend.is_ghost_sketch = False
            probe._backend.sketch_dir_path = Path(fixture_path('SavedSketch'))
            self.promotions[0][1]['callback']()
            self.pump_until(lambda: not probe.isVisible())
            probe._backend.stop_services.assert_called_once()
            release.assert_called_once_with(fixture_path('GhostSketch'), preserve_recovery=True)

    def test_actual_workspace_close_discard_requires_no_permanent_save(self):
        probe = self.close_probe()
        with patch('main.core.config.clean_instance_config'), \
                patch('src.modules.ubuntu_download_manager.quit_if_last_workspace'), \
                patch('main.qt.ghost_sketch.QMessageBox.question', return_value=QMessageBox.StandardButton.Discard), \
                patch.object(self.controller, '_release_project') as release:
            event = QCloseEvent()
            probe.closeEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertFalse(probe.isVisible())
        probe._backend.stop_services.assert_called_once()
        probe._backend.save_ghost_project.assert_not_called()
        release.assert_called_once_with(fixture_path('GhostSketch'))

    def test_actual_workspace_close_recovery_cancel_and_discard(self):
        probe = self.close_probe()
        probe._backend.is_ghost_sketch = False
        probe._backend.sketch_dir_path = Path(fixture_path('SavedSketch'))
        self.controller._promotion_recovery = {'old_path': fixture_path('GhostSketch'), 'path': fixture_path('SavedSketch')}
        with patch('main.core.config.clean_instance_config'), \
                patch('src.modules.ubuntu_download_manager.quit_if_last_workspace'), \
                patch.object(self.controller, '_release_project') as release:
            with patch('main.qt.ghost_sketch.QMessageBox.question', return_value=QMessageBox.StandardButton.Cancel):
                event = QCloseEvent()
                probe.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertTrue(probe.isVisible())
            probe._backend.stop_services.assert_not_called()
            release.assert_not_called()
            with patch('main.qt.ghost_sketch.QMessageBox.question', return_value=QMessageBox.StandardButton.Discard):
                event = QCloseEvent()
                probe.closeEvent(event)
            self.assertTrue(event.isAccepted())
            release.assert_called_once_with(fixture_path('GhostSketch'))

    def test_actual_workspace_close_recovery_save_retries_before_accepting(self):
        probe = self.close_probe()
        probe._backend.is_ghost_sketch = False
        probe._backend.sketch_dir_path = Path(fixture_path('SavedSketch'))
        self.controller._promotion_recovery = {'old_path': fixture_path('GhostSketch'), 'path': fixture_path('SavedSketch')}
        with patch('main.core.config.clean_instance_config'), \
                patch('src.modules.ubuntu_download_manager.quit_if_last_workspace'), \
                patch('main.qt.ghost_sketch.QMessageBox.question', return_value=QMessageBox.StandardButton.Save), \
                patch('main.qt.ghost_sketch.QFileDialog.getExistingDirectory') as picker, \
                patch.object(self.controller, '_release_project') as release:
            event = QCloseEvent()
            probe.closeEvent(event)
            self.assertFalse(event.isAccepted())
            picker.assert_not_called()
            probe._backend.stop_services.assert_not_called()
            self.promotions[0][1]['callback']()
            self.pump_until(lambda: not probe.isVisible())
            probe._backend.stop_services.assert_called_once()
            release.assert_called_once_with(fixture_path('GhostSketch'), preserve_recovery=True)

    def test_actual_workspace_close_save_with_cancelled_chooser_stays_open(self):
        probe = self.close_probe()
        with patch('main.qt.ghost_sketch.QMessageBox.question', return_value=QMessageBox.StandardButton.Save), \
                patch('main.qt.ghost_sketch.QFileDialog.getExistingDirectory', return_value=''):
            event = QCloseEvent()
            probe.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(probe.isVisible())
        probe._backend.stop_services.assert_not_called()
        self.assertEqual(self.flush, [])

    def test_close_or_switch_prompt_even_when_cache_files_are_clean(self):
        done = Mock()
        with patch("main.qt.ghost_sketch.QMessageBox.question", return_value=QMessageBox.StandardButton.Cancel):
            self.controller.confirm_leave(done)
        done.assert_called_once_with(False)
        done.reset_mock()
        with patch("main.qt.ghost_sketch.QMessageBox.question", return_value=QMessageBox.StandardButton.Discard):
            self.controller.confirm_leave(done)
        done.assert_called_once_with(True)
        self.window._backend.discard_ghost_project.assert_not_called()

    def test_pending_promotion_refuses_close_or_repeat_save(self):
        with patch("main.qt.ghost_sketch.QFileDialog.getExistingDirectory", return_value=fixture_path("SavedSketch")) as picker:
            self.controller.request_save()
            self.controller.request_save()
        picker.assert_called_once()
        done = Mock()
        self.controller.confirm_leave(done)
        done.assert_called_once_with(False)

    def test_explicit_workspace_save_uses_permanent_save_controller(self):
        from main.qt.main_window import MCUMainWindow
        self.window._ghost_sketch = self.controller
        with patch.object(self.controller, "request_save") as save:
            MCUMainWindow._shortcut_save(self.window)
            MCUMainWindow._shortcut_save_all(self.window)
        self.assertEqual(save.call_count, 2)
        self.assertEqual(self.flush, [])

    def test_autosave_cannot_flush_an_unsaved_ghost(self):
        from main.qt.editor_panel import MonacoEditorPanel
        panel = SimpleNamespace(_backend=self.window._backend, _autosave_enabled=True,
                                _autosave_timer=Mock(), trigger_save_all=Mock())
        MonacoEditorPanel._schedule_autosave(panel)
        MonacoEditorPanel._on_autosave_timeout(panel)
        panel._autosave_timer.start.assert_not_called()
        panel.trigger_save_all.assert_not_called()

    def test_settings_autosave_controls_are_disabled_for_ghost(self):
        from verify_controls import ControlChecks
        fixture = ControlChecks()
        fixture.setUp()
        try:
            fixture.backend.is_ghost_sketch = True
            dialog = fixture.settings()
            self.assertFalse(dialog.cb_autosave.isEnabled())
            self.assertFalse(dialog.autosave_spin.isEnabled())
        finally:
            fixture.doCleanups()


class GhostProjectSelectorChecks(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack
        from direct.verify_projects import InertLayoutCloudService
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('main.core.config.get_theme_mode', return_value='default'))
        self.stack.enter_context(patch('main.core.config.get_open_projects', return_value=[]))
        self.stack.enter_context(patch('main.qt.cloud_sketch_panel.CloudSketchService', return_value=InertLayoutCloudService()))
        self.parent = QWidget()
        self.addCleanup(self.parent.deleteLater)
        self.parent._active_operation = None
        self.confirmed = []
        self.parent._ghost_sketch = SimpleNamespace(
            confirm_leave=lambda callback: self.confirmed.append(callback),
            release_after_switch=Mock(), needs_recovery=False)
        self.backend = SimpleNamespace(is_busy=False, active_operation=None, _current_op_phase=None,
            sketch_dir_path=Path(fixture_path('OldGhost')), is_ghost_sketch=True,
            get_default_project_parent=lambda:fixture_path(), get_recent_projects=lambda:[],
            create_ghost_project=Mock(return_value={'success':True,'path':fixture_path('NewGhost')}))

    def dialog(self, startup=False):
        from main.qt.project_dialog import ProjectDialog
        dialog = ProjectDialog(None if startup else self.backend,
                               parent=None if startup else self.parent,
                               open_in_new_window=not startup)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.reject)
        return dialog

    def test_startup_ghost_button_selects_its_cache_project_without_window_prompt(self):
        dialog = self.dialog(startup=True)
        with patch('main.core.ghost_sketch.create_ghost_project_path', return_value=Path(fixture_path('StartupGhost'))) as create, \
                patch('main.qt.project_dialog._ProjectPrompt.exec') as prompt:
            dialog._btn_ghost_sketch.click()
        prompt.assert_not_called()
        create.assert_called_once()
        self.assertEqual(dialog.selected_project, Path(fixture_path('StartupGhost')))

    def test_new_window_ghost_creation_preserves_current_workspace(self):
        dialog = self.dialog()
        with patch.object(dialog, '_choose_project_window', return_value=True):
            dialog._btn_ghost_sketch.click()
        self.backend.create_ghost_project.assert_called_once_with(open_in_new_window=True)
        self.assertEqual(self.confirmed, [])
        self.parent._ghost_sketch.release_after_switch.assert_not_called()
        self.assertEqual(self.backend.sketch_dir_path, Path(fixture_path('OldGhost')))

    def test_current_window_waits_for_save_or_discard_decision_and_honors_cancel(self):
        dialog = self.dialog()
        with patch.object(dialog, '_choose_project_window', return_value=False):
            dialog._btn_ghost_sketch.click()
        self.backend.create_ghost_project.assert_not_called()
        self.assertEqual(len(self.confirmed), 1)
        self.confirmed.pop()(False)
        self.backend.create_ghost_project.assert_not_called()
        self.parent._ghost_sketch.release_after_switch.assert_not_called()
        self.assertIsNone(dialog.selected_project)

    def test_current_window_creates_only_after_async_save_and_cleans_replaced_ghost(self):
        dialog = self.dialog()
        def created(**kwargs):
            self.backend.sketch_dir_path = Path(fixture_path('NewGhost'))
            return {'success':True,'path':fixture_path('NewGhost')}
        self.backend.create_ghost_project.side_effect = created
        with patch.object(dialog, '_choose_project_window', return_value=False):
            dialog._btn_ghost_sketch.click()
        self.backend.create_ghost_project.assert_not_called()
        self.confirmed.pop()(True)
        self.backend.create_ghost_project.assert_called_once_with(open_in_new_window=False)
        self.assertEqual(dialog.selected_project, Path(fixture_path('NewGhost')))
        self.parent._ghost_sketch.release_after_switch.assert_called_once_with(fixture_path('OldGhost'))

    def test_settings_do_not_replace_global_autosave_when_ghost_controls_change(self):
        from verify_controls import ControlChecks
        fixture = ControlChecks()
        fixture.setUp()
        try:
            fixture.backend.is_ghost_sketch = True
            with patch('main.qt.settings_dialog.get_autosave_settings', return_value=(True, 2400)):
                dialog = fixture.settings()
                dialog.cb_autosave.setChecked(False)
                dialog.autosave_spin.setValue(500)
                dialog._save_and_apply()
            data = fixture.save.call_args.args[0]
            self.assertIs(data['shared']['autosave_enabled'], True)
            self.assertEqual(data['shared']['autosave_delay_ms'], 2400)
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main(verbosity=2)
