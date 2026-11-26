#!/usr/bin/env python3
"""Every tooltip is the theme's glass, whatever sheet the widget under the mouse carries.

Offscreen only: no window reaches the desktop.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for name in ("DISPLAY", "WAYLAND_DISPLAY"):
    os.environ.pop(name, None)
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer
from PySide6.QtWidgets import QApplication, QPushButton, QToolTip, QWidget

APP = QApplication.instance() or QApplication([])

from main.core.theme import Theme, get_palette
from main.qt.theme import build_stylesheet, install_no_focus_lines, tooltip_sheet
from src.modules.ui_palette import contrast_ratio

install_no_focus_lines(APP)
MODES = ("light", "default", "solarized_dark")


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class Tooltips(unittest.TestCase):
    def tearDown(self):
        QToolTip.hideText()
        pump(20)

    def shown_tip(self, mode: str, panel_sheet: str = ""):
        Theme.apply_theme(mode)
        APP.setStyleSheet(build_stylesheet(mode))
        host = QWidget()
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        host.setStyleSheet(panel_sheet)
        host.resize(300, 120)
        button = QPushButton("Upload", host)
        host.show()
        pump(50)
        QToolTip.showText(button.mapToGlobal(QPoint(8, 30)), "Upload to the selected board", button)
        pump(150)
        tip = next(widget for widget in APP.topLevelWidgets()
                   if widget.metaObject().className() == "QTipLabel" and widget.isVisible())
        return tip

    def test_a_panel_that_clears_label_backgrounds_cannot_strip_the_tooltip(self):
        hostile = "QLabel { background: transparent; color: black; border: none; }"
        for mode in MODES:
            with self.subTest(mode=mode):
                tip = self.shown_tip(mode, hostile)
                self.assertEqual(tip.styleSheet(), tooltip_sheet(mode))
                image = tip.grab().toImage()
                fill = image.pixelColor(5, image.height() // 2)  # Padding, clear of the text.
                self.assertGreater(fill.alpha(), 230, "a tooltip floats over anything, so it is nearly opaque")
                ink = get_palette(mode)["TEXT_BRIGHT"]
                self.assertGreaterEqual(contrast_ratio(ink, fill.name()), 7.0, mode)
                self.assertEqual(image.pixelColor(0, 0).alpha(), 0, "rounded, transparent corners")
                self.assertTrue(tip.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))

    def test_each_theme_has_its_own_tooltip(self):
        sheets = {mode: tooltip_sheet(mode) for mode in MODES}
        self.assertEqual(len(set(sheets.values())), len(MODES))
        for mode in MODES:
            self.assertIn(tooltip_sheet(mode).replace("QToolTip, QLabel", "QToolTip"), build_stylesheet(mode))


if __name__ == "__main__":
    unittest.main(verbosity=2)
