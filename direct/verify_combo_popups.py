"""Open real native dropdowns with isolated themes; no settings or hardware."""
from __future__ import annotations
import os
import argparse
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt, QPoint, QRect
from PySide6.QtGui import QScreen
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
    def test_long_list_shortens_without_covering_its_field_and_keeps_scrolling(self):
        host = QWidget()
        host.resize(340, 220)
        combo = QComboBox(host)
        combo.setGeometry(12, 80, 180, 28)
        combo.addItems([f"Package {i}" for i in range(30)])
        host.show()
        APP.processEvents()
        field = combo.mapToGlobal(QPoint(0, 0))
        area = QRect(field.x()-10, field.y()-70, 310, 160)
        try:
            with patch.object(QScreen, "availableGeometry", return_value=area.adjusted(-4, -4, 4, 4)):
                combo.showPopup()
                APP.processEvents()
                popup = combo._mcu_glass_popup.popup.geometry()
                self.assertTrue(area.contains(popup), str(popup))
                self.assertTrue(popup.bottom() < field.y() or popup.top() >= field.y()+combo.height(), str(popup))
                self.assertGreater(combo.view().verticalScrollBar().maximum(), 0)
                QTest.keyClick(combo.view(), Qt.Key.Key_End)
                QTest.keyClick(combo.view(), Qt.Key.Key_Return)
                self.assertEqual(combo.currentIndex(), 29)
        finally:
            combo.hidePopup()
            host.close()
            host.deleteLater()
            APP.processEvents()

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
                        self.check_alpha_corners(material.popup)
                    finally:
                        combo.hidePopup()
                        host.close()
                        host.deleteLater()
                        APP.processEvents()

    def check_alpha_corners(self, popup):
        self.assertTrue(popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        image = popup.grab().toImage()
        for x, y in ((0, 0), (image.width()-1, 0), (0, image.height()-1),
                     (image.width()-1, image.height()-1)):
            self.assertEqual(image.pixelColor(x, y).alpha(), 0, (x, y))
        self.assertGreater(image.pixelColor(image.width()//2, image.height()//2).alpha(), 240)

    def test_compact_actions_and_options_use_rounded_cached_material(self):
        from main.qt.toolbar import CompactDropdownPopup
        for mode in ("default", "light", "solarized_dark"):
            Theme.apply_theme(mode)
            APP.setStyleSheet(build_stylesheet(mode))
            host = QWidget()
            host.resize(320, 100)
            host.show()
            popup = CompactDropdownPopup(host)
            for text in ("Save All", "Settings", "AI Assistant"):
                popup.add_button(text, lambda: None, "#000000", "#000000")
            popup.show_below(host, min_width=150)
            try:
                APP.processEvents()
                self.check_alpha_corners(popup)
                self.assertTrue(popup._backdrop.isVisible())
                first = popup._backdrop._image.cacheKey()
                popup.update()
                APP.processEvents()
                self.assertEqual(first, popup._backdrop._image.cacheKey())
                self.assertTrue(popup.grab().save(str(OUTPUT / f"{mode}-compact.png")))
            finally:
                popup.close()
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
