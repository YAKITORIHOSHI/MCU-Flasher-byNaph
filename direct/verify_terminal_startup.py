#!/usr/bin/env python3
"""Verify explicit terminal creation with Qt fixtures and mocked child/IPC work.

No shell, PTY, HTTP service, settings or project journal is started or written.
Run each scale in a fresh process with QT_SCALE_FACTOR when checking native UI.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
os.environ.setdefault('QT_QPA_PLATFORM', 'windows' if sys.platform == 'win32' else 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolButton
from main.qt.terminal_panel import TerminalPanel
from main.qt.theme import build_stylesheet, register_fonts

APP = QApplication.instance() or QApplication([])


def pump():
    for _ in range(5):
        APP.processEvents()
        time.sleep(.015)


class TerminalStartupChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('main.core.config.get_monitor_font_size', return_value=11))
        self.popen = self.stack.enter_context(patch(
            'main.qt.terminal_panel.subprocess.Popen', side_effect=AssertionError('Fixture cannot launch children')))
        self.stack.enter_context(patch('main.qt.terminal_panel.QMessageBox.critical'))
        self.panel = TerminalPanel(SimpleNamespace(sketch_dir_path=''))
        self.addCleanup(self.dispose)
        self.sent = self.stack.enter_context(patch.object(self.panel, '_send_control'))
        self.start = self.stack.enter_context(patch.object(self.panel, '_start_terminal', side_effect=self.start_host))
        # Child embedding/focus cannot act on any native terminal in a fixture.
        self.stack.enter_context(patch.object(self.panel, '_resize_embedded_terminal'))
        self.stack.enter_context(patch.object(self.panel, 'focus_terminal'))
        register_fonts()

    def start_host(self):
        self.panel._is_active = True
        self.panel._is_ready = True

    def dispose(self):
        self.panel.close()
        self.panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.popen.assert_not_called()

    def assert_empty(self):
        self.assertEqual(self.panel._tab_bar.count(), 0)
        self.assertFalse(self.panel._sessions_meta)
        self.assertIsNone(self.panel._active_session_id)
        self.assertIs(self.panel._stack.currentWidget(), self.panel._empty_card)
        self.assertTrue(self.panel._tab_bar.isHidden())
        self.assertFalse(self.panel._btn_clear.isEnabled())
        self.assertFalse(self.panel._btn_kill.isEnabled())
        self.assertEqual(self.panel._status_dot.toolTip(), 'Project Terminal: No active sessions')

    def open_chooser(self, choose=None):
        observations = []
        def complete_menu():
            observations.append(self.panel._add_menu.isVisible())
            if choose:
                next(action for action in self.panel._add_menu.actions()
                     if choose in action.text().lower()).trigger()
            self.panel._add_menu.close()
        QTimer.singleShot(0, complete_menu)
        QTest.mouseClick(self.panel._btn_add_tab, Qt.MouseButton.LeftButton)
        pump()
        self.assertEqual(observations, [True])

    def test_passive_show_reveal_refresh_theme_font_and_project_reset_stay_empty(self):
        self.assert_empty()
        self.panel.resize(800, 220)
        self.panel.show()
        pump()
        for theme in ('default', 'light', 'solarized_dark'):
            APP.setStyleSheet(build_stylesheet(theme))
            self.panel.apply_theme(theme)
            self.panel.set_font_size(13)
            for width in (1920, 480, 320, 800):
                self.panel.resize(width, 100)
                self.panel._on_tab_revealed()
                self.panel.refresh_terminal()
                self.panel.ensure_started()
                self.panel.hide()
                self.panel.show()
                pump()
                self.assert_empty()
        self.panel.reset_for_project(str(ROOT / 'temp/audit/terminal-empty-fixture'))
        pump()
        self.assert_empty()
        self.start.assert_not_called()
        self.sent.assert_not_called()
        self.assertFalse(self.panel._pending_controls)
        self.assertFalse(self.panel._control_queue)
        self.assertEqual(self.panel._current_theme, 'solarized_dark')
        self.assertEqual(self.panel._current_font_size, 13)
        self.assertFalse(self.panel._spin_timer.isActive())
        self.assertFalse(self.panel._embed_poll_timer.isActive())
        self.assertFalse(self.panel._ready_poll_timer.isActive())

    def test_plus_requires_shell_selection_and_last_close_stays_empty(self):
        self.panel.resize(800, 220)
        self.panel.show()
        pump()
        self.assertEqual(self.panel._btn_add_tab.popupMode(), QToolButton.ToolButtonPopupMode.InstantPopup)
        self.open_chooser()
        self.assert_empty()
        self.start.assert_not_called()
        self.open_chooser('pwsh')
        self.start.assert_called_once()
        self.assertEqual(self.panel._tab_bar.count(), 1)
        new_calls = [call for call in self.sent.call_args_list if call.args[0] == 'new']
        self.assertEqual(len(new_calls), 1)
        self.assertEqual(new_calls[0].kwargs['extra']['kind'], 'pwsh')
        self.open_chooser('cmd')
        self.assertEqual(self.panel._tab_bar.count(), 2)
        self.start.assert_called_once()
        while self.panel._tab_bar.count():
            self.panel._on_tab_close_requested(0)
        self.panel._on_tab_revealed()
        self.panel.refresh_terminal()
        self.panel.hide()
        self.panel.show()
        pump()
        self.assert_empty()
        self.start.assert_called_once()
        self.assertEqual(len([call for call in self.sent.call_args_list if call.args[0] == 'new']), 2)

    def test_project_change_clears_existing_sessions_without_replacing_them(self):
        self.panel.resize(800, 220)
        self.panel.show()
        pump()
        QTest.mouseClick(self.panel._btn_open_cmd, Qt.MouseButton.LeftButton)
        pump()
        self.assertEqual(self.panel._tab_bar.count(), 1)
        self.assertEqual(next(iter(self.panel._sessions_meta.values()))['kind'], 'cmd')
        self.start.assert_called_once()
        self.panel.reset_for_project(str(ROOT / 'temp/audit/terminal-empty-next-fixture'))
        self.panel._on_tab_revealed()
        pump()
        self.assert_empty()
        self.start.assert_called_once()
        self.assertEqual(len([call for call in self.sent.call_args_list if call.args[0] == 'new']), 1)

    def native_fixture(self):
        """Replace every foreign HWND operation before running native methods."""
        import main.qt.terminal_panel as terminal
        native = Mock()
        native.IsWindow.return_value = True
        native.GetWindowLong.return_value = 0
        constants = dict(SW_HIDE=0, SW_SHOW=5, GWL_STYLE=-16, GWL_EXSTYLE=-20)
        style_names = ('WS_CLIPCHILDREN', 'WS_CAPTION', 'WS_THICKFRAME', 'WS_MINIMIZEBOX',
                       'WS_MAXIMIZEBOX', 'WS_SYSMENU', 'WS_POPUP', 'WS_BORDER', 'WS_CHILD',
                       'WS_EX_DLGMODALFRAME', 'WS_EX_APPWINDOW', 'WS_EX_WINDOWEDGE',
                       'WS_EX_CLIENTEDGE', 'WS_EX_TOOLWINDOW', 'SWP_FRAMECHANGED',
                       'SWP_NOZORDER', 'SWP_NOACTIVATE', 'SWP_SHOWWINDOW', 'SWP_HIDEWINDOW')
        constants.update({name: 1 << index for index, name in enumerate(style_names)})
        con = SimpleNamespace(**constants)
        windll = SimpleNamespace(user32=Mock(), kernel32=Mock())
        windll.kernel32.GetCurrentThreadId.return_value = 1
        process = Mock()
        process.GetWindowThreadProcessId.return_value = (1, 0)
        self.stack.enter_context(patch.object(terminal, 'win32gui', native))
        self.stack.enter_context(patch.object(terminal, 'win32con', con))
        self.stack.enter_context(patch.object(terminal, 'win32process', process))
        self.stack.enter_context(patch.object(terminal.ctypes, 'windll', windll, create=True))
        self.stack.enter_context(patch.object(
            self.panel, '_resize_embedded_terminal', TerminalPanel._resize_embedded_terminal.__get__(self.panel)))
        self.stack.enter_context(patch.object(
            self.panel, 'focus_terminal', TerminalPanel.focus_terminal.__get__(self.panel)))
        self.panel._is_embedded = True
        self.panel._term_hwnd = 42
        return native, con, windll

    def test_delayed_native_resize_and_focus_obey_closed_hidden_and_unready_state(self):
        self.panel.resize(800, 220)
        self.panel.show()
        pump()
        native, con, windll = self.native_fixture()

        def reset_calls():
            native.reset_mock()
            windll.user32.reset_mock()

        def drain_delayed_callbacks():
            QTest.qWait(320)
            pump()
            # Every late show request must now hide the child. Checking the
            # calls catches accidental focus stealing or flashing an empty HWND.
            self.assertTrue(native.ShowWindow.call_args_list)
            self.assertTrue(all(call.args[1] == con.SW_HIDE for call in native.ShowWindow.call_args_list))
            for call in native.SetWindowPos.call_args_list:
                self.assertFalse(call.args[-1] & con.SWP_SHOWWINDOW)
                self.assertTrue(call.args[-1] & con.SWP_HIDEWINDOW)
            native.SetFocus.assert_not_called()
            windll.user32.SetFocus.assert_not_called()
            windll.user32.SetActiveWindow.assert_not_called()

        self.panel.add_session('pwsh')
        # refresh_terminal scheduled real 30/100ms resize and 250ms focus work.
        reset_calls()
        self.panel._on_tab_close_requested(0)
        drain_delayed_callbacks()
        self.assert_empty()

        self.panel.add_session('cmd')
        reset_calls()
        self.panel.hide()
        drain_delayed_callbacks()
        self.assertEqual(self.panel._tab_bar.count(), 1)

        self.panel.show()
        reset_calls()
        self.panel._is_ready = False
        self.panel.refresh_terminal()
        drain_delayed_callbacks()
        self.assertIs(self.panel._stack.currentWidget(), self.panel._loader_card)

    def test_late_native_embedding_with_no_sessions_keeps_empty_card(self):
        self.panel.resize(800, 220)
        self.panel.show()
        pump()
        native, con, windll = self.native_fixture()
        for ready in (False, True):
            with self.subTest(ready=ready):
                self.panel._is_ready = ready
                native.reset_mock()
                TerminalPanel._embed_terminal_hwnd(self.panel, 42)
                pump()
                self.assert_empty()
                self.assertTrue(native.ShowWindow.call_args_list)
                self.assertTrue(all(call.args[1] == con.SW_HIDE for call in native.ShowWindow.call_args_list))
                native.SetFocus.assert_not_called()
                windll.user32.SetFocus.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
