#!/usr/bin/env python3
"""Isolated bootstrap journal visuals; no installers, live settings or launch."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verify_controls import APP, ControlChecks, bootstrap_fixture
from main.core.theme import Theme
from PySide6.QtGui import QTextCursor, QPalette
from PySide6.QtCore import QPoint
from src.modules.ui_palette import contrast_ratio

RENDER_DIR = None


class BootstrapHeaderChecks(ControlChecks):
    def dialog(self, mode="default"):
        dialog, _, _ = bootstrap_fixture(mode)
        self.own(dialog)
        dialog.resize(820, 580)
        dialog.show()
        APP.processEvents()
        return dialog

    def test_stage_dividers_render_in_all_themes_without_changing_copied_text(self):
        for mode in Theme.PALETTES:
            dialog = self.dialog(mode)
            for title, result in (
                ("Checking Arduino-CLI", "Arduino-CLI is already installed"),
                ("Checking CP210x Driver", "USB serial driver is installed or staged"),
                ("Checking OpenCode AI Assistant", "OpenCode AI Assistant is verified and ready"),
                ("Checking for updates", "All utilities are up to date"),
            ):
                dialog._on_log(title, "section")
                dialog._on_log(result, "ok")
            APP.processEvents()
            view = dialog.summary_edit
            rules = list(view.heading_dividers())
            self.assertEqual(len(rules), 3)
            for _, y, right, color, _ in rules:
                self.assertGreaterEqual(y, 0)
                self.assertLess(right, view.viewport().width())
                self.assertEqual(color.name(), dialog._theme_pal["T_CYAN"])
            raw = view.toPlainText()
            self.assertEqual(raw.count("Checking for updates"), 1)
            self.assertNotIn("──", raw, "Decorative rules must not pollute copying")
            cursor = view.textCursor()
            cursor.select(QTextCursor.SelectionType.Document)
            view.setTextCursor(cursor)
            selected = cursor.selectedText()
            capture = dialog.grab()
            self.assertFalse(capture.isNull())
            self.assertEqual(view.textCursor().selectedText(), selected)
            view.setTextCursor(QTextCursor(view.document()))
            if RENDER_DIR:
                self.assertTrue(dialog.grab().save(str(RENDER_DIR / f"headers-{mode}.png")))
            dialog.hide()

    def test_nested_headers_and_failed_stage_keep_readable_hierarchy(self):
        dialog = self.dialog("light")
        dialog._on_log("Earlier successful stage", "section")
        dialog._on_log("Earlier result", "ok")
        dialog._on_log("Preparing board tools", "section")
        dialog._on_log("Preparing a nested framework", "subsection")
        dialog._on_log("Package download denied", "fail")
        APP.processEvents()
        view = dialog.summary_edit
        rules = list(view.heading_dividers())
        self.assertEqual(len(rules), 2)
        self.assertTrue(all(color.name() == dialog._theme_pal["T_RED"] for _, _, _, color, _ in rules))
        self.assertEqual(view.document().find("Earlier result").charFormat().foreground().color().name(),
                         dialog._theme_pal["T_GREEN"])
        dialog._on_log("Next independent stage", "section")
        APP.processEvents()
        self.assertEqual(list(view.heading_dividers())[-1][3].name(), dialog._theme_pal["T_CYAN"])

    def test_actual_journal_inks_contrast_with_rendered_glass_and_selection(self):
        tags = ("normal", "dim", "section", "subsection", "ok", "warn", "update", "pip_row", "fail", "failed_step")
        for mode in Theme.PALETTES:
            dialog = self.dialog(mode)
            original = {"T_" + key: value for key, value in Theme.PALETTES[mode].items()}
            for expanded in (True, False):
                dialog._set_details_expanded(expanded)
                APP.processEvents()
                view = dialog.log_edit if expanded else dialog.summary_edit
                view.clear()
                APP.processEvents()
                # Capture the actual empty reading surface, including the card's
                # gradient/reflections beneath this transparent viewport.
                empty = dialog.grab().toImage()
                origin = view.viewport().mapTo(dialog, QPoint())
                dpr = empty.devicePixelRatio()
                backgrounds = {empty.pixelColor(round((origin.x() + x) * dpr),
                                                round((origin.y() + y) * dpr)).name()
                               for x in range(3, view.viewport().width() - 3, 13)
                               for y in range(3, view.viewport().height() - 3, 13)}
                self.assertGreater(len(backgrounds), 2, "Capture must include the painted glass")
                for tag in tags:
                    with self.subTest(theme=mode, expanded=expanded, tag=tag):
                        label = f"Readable {tag} text"
                        if expanded:
                            dialog._on_log(label, tag)
                        else:
                            dialog._restore_summary({"presentation": {}, "summary": {
                                "text": label + "\n", "runs": [{"text": label + "\n", "tags": [tag]}]}})
                        cursor = view.document().find(label)
                        self.assertFalse(cursor.isNull())
                        ink = cursor.charFormat().foreground().color().name()
                        self.assertGreaterEqual(min(contrast_ratio(ink, bg) for bg in backgrounds), 4.5,
                                                (mode, expanded, tag, ink))
                        view.setTextCursor(cursor)
                        APP.processEvents()
                        selected = view.palette().color(QPalette.ColorRole.HighlightedText).name()
                        selection = view.palette().color(QPalette.ColorRole.Highlight).name()
                        self.assertGreaterEqual(contrast_ratio(selected, selection), 4.5)
                        self.assertEqual(view.textCursor().selectedText(), label)
                        self.assertFalse(view.viewport().grab().isNull())
                if RENDER_DIR:
                    self.assertTrue(dialog.grab().save(str(RENDER_DIR / f"readable-{mode}-{expanded}.png")))
            self.assertEqual(dialog._context.palette, original, "Readability must not mutate shared theme tokens")
            dialog.hide()

    def test_native_log_tags_contrast_without_importing_qt(self):
        code = '''
import sys
from pathlib import Path
sys.path.insert(0, str(Path("direct").resolve()))
from verify_bootstrap_native import fixture, Theme
from src.modules.ui_palette import contrast_ratio
for mode in Theme.PALETTES:
    gui, _ = fixture(mode)
    try:
        for view in (gui._native_window.log_edit, gui._native_window.summary_edit):
            for tag in ("normal", "dim", "section", "subsection", "ok", "warn", "fail", "update", "pip_row", "failed_step"):
                ink = view.tag_cget(tag, "foreground")
                assert contrast_ratio(ink, view.cget("bg")) >= 4.5, (mode, tag, ink)
            assert contrast_ratio(view.cget("selectforeground"), view.cget("selectbackground")) >= 4.5
    finally:
        gui.close()
assert not any(name == "PySide6" or name.startswith("PySide6.") for name in sys.modules)
print("Native setup semantic tags and selection: all three themes readable without Qt")
'''
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_long_headers_reflow_and_keep_selection_at_compact_widths(self):
        dialog = self.dialog()
        title = "Preparing board frameworks and upload tools for a long selected package name"
        dialog._on_log("Previous result", "ok")
        dialog._on_log(title, "section")
        dialog._on_log("Selected diagnostic remains readable", "normal")
        view = dialog.summary_edit
        view.setTextCursor(view.document().find("Selected diagnostic"))
        raw, selected = view.toPlainText(), view.textCursor().selectedText()
        for width in (420, 1040, 420):
            dialog.resize(width, 440)
            APP.processEvents()
            self.assertEqual(view.toPlainText(), raw)
            self.assertEqual(view.textCursor().selectedText(), selected)
            for _, _, right, _, _ in view.heading_dividers():
                self.assertLess(right, view.viewport().width())
            heading = view.document().find(title).block()
            self.assertGreater(heading.layout().lineCount(), 1 if width == 420 else 0)
            self.assertFalse(dialog.grab().isNull())
        if RENDER_DIR:
            self.assertTrue(dialog.grab().save(str(RENDER_DIR / "headers-compact.png")))

    def test_restored_native_summary_retains_header_rules_and_selection(self):
        dialog = self.dialog()
        raw = "Earlier result\n\nChecking Arduino-CLI\n  Arduino-CLI is ready\n"
        start = raw.index("Arduino-CLI is ready")
        dialog._restore_summary({"presentation": {}, "auto_scroll": False, "summary": {
            "text": raw, "runs": [
                {"text": "Earlier result\n\n", "tags": ["normal"]},
                {"text": "Checking Arduino-CLI\n", "tags": ["section"]},
                {"text": "  Arduino-CLI is ready\n", "tags": ["ok"]},
            ], "selection": (start, start + len("Arduino-CLI is ready")),
        }})
        APP.processEvents()
        self.assertEqual(dialog.summary_edit.toPlainText(), raw)
        self.assertEqual(dialog.summary_edit.textCursor().selectedText(), "Arduino-CLI is ready")
        self.assertEqual(len(list(dialog.summary_edit.heading_dividers())), 1)


def main():
    global RENDER_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-dir", type=Path)
    args = parser.parse_args()
    RENDER_DIR = args.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    suite = unittest.TestSuite(BootstrapHeaderChecks(name) for name in BootstrapHeaderChecks.__dict__
                               if name.startswith("test_"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
