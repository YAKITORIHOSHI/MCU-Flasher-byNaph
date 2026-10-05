"""Hardware-free checks for readable, palette-aware workspace log text."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtWidgets import QApplication, QLabel
from main.core.theme import Theme
from main.qt.console_panel import ConsolePanel
from main.qt.log_colors import contrast_ratio, themed_ansi_colors, themed_log_colors
from main.qt.main_window import MCUMainWindow
from main.qt.serial_panel import SerialOutputView, SerialPanel
from main.qt.theme import get_palette
from direct.verify_runtime import PreviewBackend

APP = QApplication.instance() or QApplication([])


def rendered_color(widget, text: str) -> str:
    cursor = widget.document().find(text)
    if cursor.isNull():
        raise AssertionError(f"Could not find rendered text {text!r}")
    return cursor.charFormat().foreground().color().name()


class ThemeReadabilityChecks(unittest.TestCase):
    def make_widget(self, widget):
        self.addCleanup(widget.deleteLater)
        return widget

    def test_log_and_ansi_foregrounds_meet_readability_contrast(self):
        for mode in ("default", "light", "solarized_dark"):
            background = get_palette(mode)["BG_DARKEST"]
            for name, color in themed_log_colors(mode).items():
                self.assertGreaterEqual(contrast_ratio(color, background), 4.5,
                                        f"{mode} log color {name}={color} is too faint")
            for code, color in themed_ansi_colors(mode).items():
                self.assertGreaterEqual(contrast_ratio(color, background), 4.5,
                                        f"{mode} ANSI color {code}={color} is too faint")

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
        original = Theme.active_theme
        self.addCleanup(Theme.apply_theme, original)
        label = self.make_widget(QLabel())
        probe = SimpleNamespace(_status_label=label)
        probe._apply_notification_color = lambda kind: MCUMainWindow._apply_notification_color(probe, kind)
        probe._clear_notification_status = lambda: MCUMainWindow._clear_notification_status(probe)
        for mode in ("default", "light", "solarized_dark"):
            Theme.apply_theme(mode)
            colors = themed_log_colors(mode)
            MCUMainWindow._on_notification(probe, {"type": "success", "message": "Preferences updated"})
            self.assertIn(colors["success"], label.styleSheet())
            probe._apply_notification_color(probe._notification_type)
            self.assertIn(colors["success"], label.styleSheet())
            probe._clear_notification_status()
            self.assertEqual(label.text(), "Ready")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ThemeReadabilityChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
