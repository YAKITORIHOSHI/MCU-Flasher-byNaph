"""Open real native dropdowns with isolated themes; no settings or hardware."""
from __future__ import annotations
import os
import argparse
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QVBoxLayout, QWidget
from main.core.theme import Theme
from main.qt.theme import build_stylesheet, install_no_focus_lines, register_fonts
from main.qt.toolbar import MarqueeComboBox
from main.qt.owner_ticket_style import PortalComboBox, portal_stylesheet

APP = QApplication.instance() or QApplication([])
register_fonts()
install_no_focus_lines(APP)
OUTPUT = ROOT / "temp/audit/combo-popups"


class ComboPopups(unittest.TestCase):
    def test_all_native_combo_families_have_glass_and_keep_keyboard_selection(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        for mode in ("default", "light", "solarized_dark"):
            Theme.apply_theme(mode)
            APP.setStyleSheet(build_stylesheet(mode))
            for kind in (QComboBox, MarqueeComboBox, PortalComboBox):
                with self.subTest(mode=mode, kind=kind.__name__):
                    host = QWidget()
                    row = QVBoxLayout(host)
                    combo = kind(host)
                    if kind is PortalComboBox:
                        host.setStyleSheet(portal_stylesheet(mode))
                    combo.addItems(["First project", "Second project", "Long project " + "name " * 90])
                    row.addWidget(combo)
                    host.resize(320, 100)
                    host.show()
                    try:
                        APP.processEvents()
                        combo.showPopup()
                        APP.processEvents()
                        material = combo._mcu_glass_popup
                        self.assertTrue(material.backdrop.isVisible())
                        self.assertEqual(material._mode, mode)
                        self.assertTrue(material.popup.screen().availableGeometry().contains(material.popup.geometry()),
                                        str(material.popup.geometry()))
                        QTest.keyClick(combo.view(), Qt.Key.Key_Down)
                        QTest.keyClick(combo.view(), Qt.Key.Key_Return)
                        APP.processEvents()
                        self.assertEqual(combo.currentIndex(), 1)
                        combo.showPopup()
                        APP.processEvents()
                        self.assertTrue(material.popup.grab().save(str(OUTPUT / f"{mode}-{kind.__name__}.png")))
                        self.assertIn("background: transparent", combo.view().styleSheet())
                        self.assertFalse(material.popup.mask().isEmpty())
                    finally:
                        combo.hidePopup()
                        host.close()
                        host.deleteLater()
                        APP.processEvents()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-dir", type=Path, default=OUTPUT)
    args, remaining = parser.parse_known_args()
    OUTPUT = args.render_dir.resolve()
    if not OUTPUT.is_relative_to((ROOT / "temp").resolve()):
        parser.error("Captures must stay inside temp/")
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
