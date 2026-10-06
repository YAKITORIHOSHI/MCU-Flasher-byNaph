"""Hardware-free checks for readable, palette-aware workspace log text."""
from __future__ import annotations

import os
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from main.core import config
from main.core.theme import Theme
from main.qt.console_panel import ConsolePanel
from main.qt.log_colors import contrast_ratio, themed_ansi_colors, themed_log_colors, themed_terminal_colors
from main.qt.main_window import MCUMainWindow
from main.qt.serial_panel import SerialOutputView, SerialPanel
from main.qt.theme import build_stylesheet, get_palette, install_checkbox_focus_style
from direct.verify_runtime import PreviewBackend

APP = QApplication.instance() or QApplication([])


def rendered_color(widget, text: str) -> str:
    cursor = widget.document().find(text)
    if cursor.isNull():
        raise AssertionError(f"Could not find rendered text {text!r}")
    return cursor.charFormat().foreground().color().name()


class ThemeReadabilityChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config, "_load_raw_config", return_value={"shared": {}, "instances": {}}))
        self.stack.enter_context(patch.object(config, "_save_raw_config"))
        self.stack.enter_context(patch.object(config, "save_gui_config"))
        self.stack.enter_context(patch.object(config, "get_theme_mode", return_value="default"))
        self.addCleanup(Theme.apply_theme, Theme.active_theme)
        self.addCleanup(APP.setStyleSheet, APP.styleSheet())
        self.addCleanup(APP.setProperty, "mcuAppliedTheme", APP.property("mcuAppliedTheme"))

    def make_widget(self, widget):
        def dispose():
            widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)
        return widget

    def status_window(self):
        with ExitStack() as stack:
            for method in ("_setup_window", "_build_ui", "_connect_signals", "_restore_geometry"):
                stack.enter_context(patch.object(MCUMainWindow, method))
            stack.enter_context(patch("main.qt.main_window.ScreenWatcher"))
            window = self.make_widget(MCUMainWindow(PreviewBackend()))
        window._build_status_bar()
        return window

    def test_log_and_ansi_foregrounds_meet_readability_contrast(self):
        for mode in ("default", "light", "solarized_dark"):
            background = get_palette(mode)["BG_DARKEST"]
            for name, color in themed_log_colors(mode).items():
                self.assertGreaterEqual(contrast_ratio(color, background), 4.5,
                                        f"{mode} log color {name}={color} is too faint")
            for code, color in themed_ansi_colors(mode).items():
                self.assertGreaterEqual(contrast_ratio(color, background), 4.5,
                                        f"{mode} ANSI color {code}={color} is too faint")
            for surface in ("BG_DARK", "BG_MID", "BG_HOVER"):
                for name, color in themed_log_colors(mode, background=get_palette(mode)[surface]).items():
                    self.assertGreaterEqual(contrast_ratio(color, get_palette(mode)[surface]), 4.5,
                                            f"{mode} {surface} color {name} is too faint")
            terminal = themed_terminal_colors(mode)
            self.assertGreaterEqual(contrast_ratio(terminal["selectionForeground"], terminal["selectionBackground"]), 4.5)
            for name, color in terminal.items():
                if name not in ("background", "cursorAccent", "selectionBackground", "selectionForeground"):
                    self.assertGreaterEqual(contrast_ratio(color, terminal["background"]), 4.5,
                                            f"{mode} terminal {name} is too faint")

    def test_checkbox_focus_is_confined_to_indicator_in_every_theme(self):
        install_checkbox_focus_style(APP)
        self.assertTrue(APP.property("mcuCheckboxFocusStyleInstalled"))
        for mode in ("default", "light", "solarized_dark"):
            stylesheet = build_stylesheet(mode)
            checkbox_focus = stylesheet.split("QCheckBox:focus {", 1)[1].split("}", 1)[0]
            self.assertIn("border: none;", checkbox_focus, mode)
            self.assertIn("QCheckBox::indicator:focus {", stylesheet, mode)
            self.assertNotIn("QCheckBox:focus::indicator", stylesheet, mode)
            self.assertNotIn("QCheckBox:focus,", stylesheet, mode)

    def test_existing_serial_and_build_output_recolors_with_theme(self):
        serial = self.make_widget(SerialOutputView())
        console = self.make_widget(ConsolePanel())
        for widget in (serial, console):
            widget.append_log({"text": "Palette readable fixture", "tag": "normal"})
            widget.append_log({"text": "Palette system fixture", "tag": "system"})
            widget._flush_queue()

        for mode in ("default", "light", "solarized_dark"):
            colors = themed_log_colors(mode)
            serial.apply_theme(mode)
            console.apply_theme(mode)
            serial_style = serial.styleSheet()
            self.assertIn("QPlainTextEdit#serial-console", serial_style)
            self.assertIn(f"color: {get_palette(mode)['TEXT']}", serial_style)
            self.assertEqual(serial.palette().color(serial.palette().ColorRole.Text).name(),
                             get_palette(mode)["TEXT"])
            for widget in (serial, console):
                self.assertEqual(rendered_color(widget, "Palette readable fixture"), colors["normal"])
                self.assertEqual(rendered_color(widget, "Palette system fixture"), colors["system"])

    def test_serial_connection_states_follow_active_palette(self):
        panel = self.make_widget(SerialPanel(PreviewBackend()))
        for mode in ("default", "light", "solarized_dark"):
            colors = themed_log_colors(mode)
            panel.update_status({"connected": True})
            panel.apply_theme(mode)
            self.assertIn(colors["success"], panel.lbl_status.styleSheet())
            panel.update_status({"connected": False, "state": "reconnecting"})
            self.assertIn(colors["warning"], panel.lbl_status.styleSheet())

    def test_status_bar_notifications_follow_theme_colors(self):
        window = self.status_window()
        label = window._status_label
        for mode in ("default", "light", "solarized_dark"):
            Theme.apply_theme(mode)
            colors = themed_log_colors(mode, background=get_palette(mode)["BG_DARK"])
            window._on_notification({"type": "success", "message": "Preferences updated"})
            self.assertIn(colors["success"], label.styleSheet())
            window._apply_notification_color(window._notification_type)
            self.assertIn(colors["success"], label.styleSheet())
            window._clear_notification_status()
            self.assertEqual(label.text(), "Ready")

    def test_notification_expiry_preserves_new_actions_and_restarts_for_latest_message(self):
        window = self.status_window()
        window._notification_timer.setInterval(100)
        window._on_notification({"message": "First notice"})
        QTest.qWait(65)
        window._on_notification({"message": "Latest notice"})
        QTest.qWait(65)
        self.assertEqual(window._status_label.text(), "Latest notice")
        QTest.qWait(65)
        self.assertEqual(window._status_label.text(), "Ready")

        window._on_notification({"type": "error", "message": "Previous action failed"})
        window._active_operation = "compile"
        window._on_console_progress({"action": "Compiling"})
        QTest.qWait(120)
        self.assertEqual(window._status_label.text(), "⚙ Compiling…")
        self.assertEqual(window._status_label.styleSheet(), "")
        window._on_notification({"message": "Compiling diagnostic"})
        deadline = time.monotonic() + 1.0
        while window._status_label.text() != "⚙ Compiling…" and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertEqual(window._status_label.text(), "⚙ Compiling…")

    def test_temporary_action_cannot_clear_a_new_notification(self):
        window = self.status_window()
        window._trigger_temporary_action("Saving", duration_ms=30)
        window._on_notification({"type": "success", "message": "Saved all files"})
        QTest.qWait(60)
        self.assertEqual(window._status_label.text(), "Saved all files")
        self.assertFalse(window._progress_bar.isVisible())

    def test_workspace_theme_signal_rebuilds_each_log_once(self):
        from main.core import file_utils
        from main.qt.signals import signals
        with patch.object(file_utils, "hide_internal_project_metadata"):
            window = self.make_widget(MCUMainWindow(PreviewBackend()))
        serial = window._serial_panel._output
        console = window._console_container.console
        for view in (serial, console):
            view.append_log({"text": "Retained output"})
            view._flush_queue()
        with patch.object(serial, "_rebuild_document", wraps=serial._rebuild_document) as serial_rebuild, \
                patch.object(console, "_rebuild_document", wraps=console._rebuild_document) as console_rebuild:
            signals.theme_changed.emit("light")
            serial_rebuild.assert_called_once()
            console_rebuild.assert_called_once()
        self.assertEqual(APP.property("mcuAppliedTheme"), "light")
        for view in (serial, console):
            self.assertEqual(view.toPlainText().strip(), "Retained output")
            self.assertEqual(rendered_color(view, "Retained output"), themed_log_colors("light")["normal"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ThemeReadabilityChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
