"""Hardware-free Linux terminal lifecycle checks; never start a real PTY."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget
from main.qt import posix_terminal_panel as terminal
from main.qt.garbage_collection import install_gui_garbage_collector

APP = QApplication.instance() or QApplication([])
COLLECTOR = install_gui_garbage_collector(APP)
PANELS = []


class FakeView(QWidget):
    loadFinished = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page = Mock()
        self.setUrl = Mock()

    def page(self):
        return self._page


class FakeSession(QObject):
    output = Signal(str)
    ended = Signal(str)

    def __init__(self, cwd, argv, parent=None):
        super().__init__(parent)
        self.cwd, self.argv = cwd, argv
        self.start = Mock()
        self.close = Mock()


class FakeChannel:
    def __init__(self, parent):
        self.registerObject = Mock()


class PosixTerminalStateChecks(unittest.TestCase):
    def setUp(self):
        for name, value in (("QWebEngineView", FakeView), ("QWebChannel", FakeChannel), ("PtySession", FakeSession)):
            patcher = patch.object(terminal, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        font = patch("main.core.config.get_monitor_font_size", return_value=12)
        font.start()
        self.addCleanup(font.stop)
        shell = patch.object(terminal.shutil, "which", side_effect=lambda name: "/usr/bin/" + name)
        shell.start()
        self.addCleanup(shell.stop)
        self.backend = SimpleNamespace(sketch_dir_path=str(ROOT / "temp" / "fixture-sketch"))

    def panel(self, cls=terminal.PosixTerminalPanel):
        panel = cls(self.backend)
        PANELS.append(panel)
        self.addCleanup(panel.close)
        return panel

    def assert_empty(self, panel):
        self.assertEqual(panel._tabs.count(), 0)
        self.assertFalse(panel._sessions)
        self.assertFalse(panel._is_active)
        self.assertIs(panel._stack.currentWidget(), panel._empty)
        self.assertTrue(all(not button.isEnabled() for button in panel._session_actions))

    def test_initial_reveal_theme_and_visible_reset_never_create_shell(self):
        panel = self.panel()
        self.assert_empty(panel)
        panel.show()
        APP.processEvents()
        with patch.object(panel, "add_session") as create:
            panel._on_tab_revealed()
            panel._on_tab_hidden()
            panel._on_tab_revealed()
            panel.apply_theme("solarized")
            panel.set_font_size(18)
            panel.set_responsive_width(500)
            panel.reset_for_project(str(ROOT / "temp" / "another-fixture"))
            create.assert_not_called()
        self.assert_empty(panel)

    def test_new_bash_is_explicit_and_last_close_stays_empty(self):
        panel = self.panel()
        panel.add_session()
        self.assertEqual(panel._tabs.count(), 1)
        self.assertIs(panel._stack.currentWidget(), panel._tabs)
        self.assertTrue(all(button.isEnabled() for button in panel._session_actions))
        view = panel._tabs.currentWidget()
        session, channel = panel._sessions[view]
        self.assertEqual(session.argv, ["/usr/bin/bash", "-i"])
        self.assertEqual(session.cwd, self.backend.sketch_dir_path)
        session.start.assert_not_called()  # Only the local renderer's bridge starts it.
        view.loadFinished.emit(True)
        self.assertTrue(view.page().runJavaScript.called)
        panel._clear()
        self.assertEqual(view.page().runJavaScript.call_args.args[0], "window.clearTerminal?.()")
        panel._end_current()
        session.close.assert_called_once()
        self.assert_empty(panel)
        with patch.object(panel, "add_session") as create:
            panel._on_tab_revealed()
            create.assert_not_called()
        self.assert_empty(panel)

    def test_project_reset_closes_sessions_and_returns_to_empty(self):
        panel = self.panel()
        panel.show()
        panel.add_session()
        panel.add_session()
        sessions = [entry[0] for entry in panel._sessions.values()]
        destination = str(ROOT / "temp" / "next-fixture")
        panel.reset_for_project(destination)
        for session in sessions:
            session.close.assert_called_once()
        self.assert_empty(panel)
        panel.add_session()
        self.assertEqual(next(iter(panel._sessions.values()))[0].cwd, destination)

    def test_missing_shell_keeps_empty_without_browser_or_pty(self):
        panel = self.panel()
        with patch.object(terminal.shutil, "which", return_value=None), patch.object(terminal, "QWebEngineView") as view:
            panel.add_session()
            view.assert_not_called()
        self.assert_empty(panel)
        self.assertIn("Install Bash", panel._label.text())

    def test_assistant_reveal_behavior_is_preserved(self):
        panel = self.panel(terminal.PosixAIPanel)
        self.assert_empty(panel)
        panel._on_tab_revealed()
        self.assertEqual(panel._tabs.count(), 1)
        self.assertEqual(next(iter(panel._sessions.values()))[0].argv, ["/usr/bin/opencode"])
        panel._on_tab_revealed()
        self.assertEqual(panel._tabs.count(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
