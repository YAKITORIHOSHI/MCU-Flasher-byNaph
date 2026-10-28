#!/usr/bin/env python3
"""Hardware-free checks for moving the existing action row with the editor."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QPlainTextEdit, QStackedWidget, QToolBar
from main.core.theme import Theme
from main.qt.main_window import MCUMainWindow
from main.qt.theme import build_stylesheet, register_fonts
from main.qt.toolbar import PrimaryToolbar
from verify_runtime import PreviewBackend

APP = QApplication.instance() or QApplication([])
register_fonts()


def pump():
    for _ in range(12):
        APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class EditorFixture(QPlainTextEdit):
    def trigger_save_all(self, callback=None):
        self.save_all_callback = callback

    def has_input_focus(self):
        return self.hasFocus()

    def restore_input_focus(self):
        self.setFocus()

    def force_layout(self):
        pass

    def show_project_search(self):
        pass


class WorkspaceFixture(QMainWindow):
    detach_editor = MCUMainWindow.detach_editor
    attach_editor = MCUMainWindow.attach_editor
    _setup_shortcuts = MCUMainWindow._setup_shortcuts
    _set_action_shortcut_host = MCUMainWindow._set_action_shortcut_host
    _update_native_cli_shortcuts = MCUMainWindow._update_native_cli_shortcuts

    def __init__(self):
        super().__init__()
        self._backend = PreviewBackend()
        self._editor_detached = self._is_attaching_editor = False
        self._editor_pane_visible = True
        self._detached_window = None
        self._active_operation = None
        self.saved = self.saved_all = self.reloaded = self.modified = 0
        self.compiled = self.uploaded = self.opened = 0
        self._ai_panel = self._terminal_panel = SimpleNamespace(has_input_focus=lambda: False)
        self._editor_area = QStackedWidget(self)
        self._editor_panel = EditorFixture(self._editor_area)
        self._editor_panel.setPlainText("// unsaved Unicode — fixture\n")
        self._editor_panel.document().setModified(True)
        self._editor_area.addWidget(self._editor_panel)
        self.setCentralWidget(self._editor_area)
        self._primary_toolbar = PrimaryToolbar(self._backend, self)
        self.addToolBar(self._primary_toolbar)
        self.addToolBarBreak()
        self._controls_toolbar = QToolBar("Controls", self)
        self._controls_toolbar.setObjectName("controls-toolbar")
        self.addToolBar(self._controls_toolbar)
        self._setup_shortcuts()
        self.resize(1280, 680)
        self.show()
        pump()

    def _sync_ai_and_editor_layout(self):
        self._editor_area.setVisible(not self._editor_detached and self._editor_pane_visible)

    def _get_active_screen(self):
        return APP.primaryScreen()

    def _on_notification(self, payload):
        pass

    def _shortcut_save(self):
        self.saved += 1

    def _shortcut_save_all(self):
        self.saved_all += 1

    def _shortcut_reload_file(self):
        self.reloaded += 1

    def _open_modify_files_dialog(self):
        self.modified += 1

    def _shortcut_compile(self):
        self.compiled += 1

    def _shortcut_upload(self):
        self.uploaded += 1

    def _shortcut_open_project(self):
        self.opened += 1

    def _on_cloud_project_pulled(self, path, **kwargs):
        pass


class DetachedActionChecks(unittest.TestCase):
    def setUp(self):
        self.config = patch("main.core.config.load_gui_config", return_value={})
        self.config.start()
        self.addCleanup(self.config.stop)
        self.notifications = patch("src.dbs.dbs_create.add_notification")
        self.notifications.start()
        self.addCleanup(self.notifications.stop)
        self.old_theme = Theme.active_theme
        Theme.apply_theme("default")
        APP.setStyleSheet(build_stylesheet("default"))
        self.window = WorkspaceFixture()
        self.addCleanup(self.dispose)

    def dispose(self):
        if self.window._editor_detached:
            self.window.attach_editor()
        self.window.close()
        self.window.deleteLater()
        pump()
        Theme.apply_theme(self.old_theme)

    def test_same_action_row_moves_with_editor_and_close_restores_it(self):
        toolbar, editor = self.window._primary_toolbar, self.window._editor_panel
        actions = tuple(toolbar.actions())
        shortcuts = tuple(self.window.findChildren(QShortcut))
        self.window.detach_editor()
        pump()
        detached = self.window._detached_window
        self.assertIs(toolbar.window(), detached, "Action row must travel with detached editor")
        self.assertIs(editor.window(), detached)
        self.assertIs(self.window._controls_toolbar.window(), self.window)
        self.assertTrue(toolbar.isVisible())
        self.assertEqual(tuple(toolbar.actions()), actions)
        self.assertEqual(len(shortcuts), 6)
        detached.close()
        pump()
        self.assertFalse(self.window._editor_detached)
        self.assertIs(toolbar.window(), self.window)
        self.assertIs(self.window._editor_area.currentWidget(), editor)
        self.assertTrue(toolbar.isVisible())
        self.assertTrue(self.window.rect().contains(toolbar.geometry()), "Restored action row must fit its host")
        self.assertGreaterEqual(self.window._controls_toolbar.y(), toolbar.geometry().bottom() + 1,
                                "Controls must remain on their own row after attachment")
        self.assertEqual(tuple(self.window.findChildren(QShortcut)), shortcuts)
        self.assertTrue(editor.document().isModified())
        self.assertEqual(editor.toPlainText(), "// unsaved Unicode — fixture\n")

    def test_detached_actions_keep_workspace_callbacks_and_busy_state(self):
        self.window.detach_editor()
        toolbar = self.window._primary_toolbar
        toolbar.btn_save.click()
        toolbar.btn_save_all.click()
        toolbar.btn_reload.click()
        toolbar.btn_modify.click()
        self.assertEqual((self.window.saved, self.window.saved_all, self.window.reloaded,
                          self.window.modified), (1, 1, 1, 1))
        self.window._active_operation = "compile"
        self.assertTrue(toolbar._is_busy(), "Workspace busy state must survive toolbar transfer")
        self.window._active_operation = None
        with patch("main.qt.project_dialog.ProjectDialog") as dialog:
            toolbar._on_new_project()
            self.assertIs(dialog.call_args.kwargs["parent"], self.window)
        with patch("main.qt.cloud_sketch_panel.show_cloud_dialog") as cloud:
            toolbar._on_cloud_sketches()
            self.assertEqual(cloud.call_args.kwargs["on_pulled"], self.window._on_cloud_project_pulled)

    def test_shortcuts_follow_toolbar_and_keep_original_handlers(self):
        self.window.detach_editor()
        detached = self.window._detached_window
        detached.activateWindow()
        self.window._editor_panel.setFocus()
        pump()
        for key, modifiers in ((Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier),
                               (Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier),
                               (Qt.Key.Key_U, Qt.KeyboardModifier.ControlModifier)):
            QTest.keyClick(self.window._editor_panel, key, modifiers)
            pump()
        self.assertEqual((self.window.saved, self.window.compiled, self.window.uploaded), (1, 1, 1))

    def test_detached_compile_saves_own_editor_and_rechecks_busy_state(self):
        self.window.detach_editor()
        backend = self.window._backend
        backend.current_board = "Fixture board"
        backend.compile_sketch = Mock()
        toolbar = self.window._primary_toolbar
        toolbar._do_compile()
        backend.compile_sketch.assert_not_called()
        self.window._editor_panel.save_all_callback()
        backend.compile_sketch.assert_called_once()
        toolbar._do_compile()
        self.window._active_operation = "compile"
        self.window._editor_panel.save_all_callback()
        backend.compile_sketch.assert_called_once()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux native CLI shortcut policy")
    def test_native_cli_suppression_retains_the_moved_shortcuts(self):
        self.window.detach_editor()
        self.window._ai_panel = SimpleNamespace(has_input_focus=lambda: True)
        self.window._update_native_cli_shortcuts()
        self.assertTrue(self.window._workspace_shortcuts)
        self.assertTrue(all(not shortcut.isEnabled() for shortcut in self.window._workspace_shortcuts))
        self.window._ai_panel = SimpleNamespace(has_input_focus=lambda: False)
        self.window._update_native_cli_shortcuts()
        self.assertTrue(all(shortcut.isEnabled() for shortcut in self.window._workspace_shortcuts))

    def test_native_visibility_survives_hidden_row_transfer(self):
        toolbar = self.window._primary_toolbar
        toolbar.hide()
        self.window.detach_editor()
        pump()
        self.assertTrue(toolbar.isHidden())
        self.window.attach_editor()
        pump()
        self.assertTrue(toolbar.isHidden())

    def test_shortcuts_work_when_action_row_is_hidden(self):
        toolbar = self.window._primary_toolbar
        toolbar.hide()
        self.window.detach_editor()
        self.window._detached_window.activateWindow()
        self.window._editor_panel.setFocus()
        pump()
        QTest.keyClick(self.window._editor_panel, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        pump()
        self.assertEqual(self.window.saved, 1)

    def test_ghost_caption_and_save_gate_remain_bound_to_workspace(self):
        toolbar = self.window._primary_toolbar
        self.window._backend.is_ghost_sketch = True
        toolbar.update_sketch_label("/fixture/.random-temporary-workspace")
        self.assertEqual(toolbar.lbl_sketch.text(), "Ghost Sketch")
        self.assertIn("Temporary", toolbar.lbl_sketch.toolTip())
        self.window.detach_editor()
        self.window._ghost_save_in_progress = True
        self.assertTrue(toolbar._is_busy())

    def test_detached_host_owns_short_height_and_closes_popup_on_transfer(self):
        toolbar = self.window._primary_toolbar
        self.window.resize(420, 680)
        pump()
        toolbar._toggle_actions_menu()
        pump()
        self.assertIsNotNone(toolbar._actions_popup)
        self.window.detach_editor()
        pump()
        self.assertIsNone(toolbar._actions_popup)
        detached = self.window._detached_window
        for mode in ("default", "light", "solarized_dark"):
            Theme.apply_theme(mode)
            APP.setStyleSheet(build_stylesheet(mode))
            toolbar.apply_theme(mode)
            for height, short in ((320, True), (680, False)):
                detached.resize(800, height)
                pump()
                self.assertEqual(bool(toolbar.property("shortScreen")), short)
                bottom = toolbar.btn_compile.mapTo(toolbar, toolbar.btn_compile.rect().bottomLeft()).y()
                self.assertGreaterEqual(toolbar.height() - 1 - bottom, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
