#!/usr/bin/env python3
"""ANSI SGR in the Serial Monitor and Build Console; hardware and persistence mocked.

Covers 16/256/truecolor foregrounds and backgrounds in semicolon and colon forms,
bold, dim, italic, underline, inverse and strikethrough, sequences split across
reads, readable contrast in every theme, copy fidelity and recoloring on theme change.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from main.core.ansi_sgr import PLAIN, SgrStream, SgrStyle, apply_sgr, resolve_colors, xterm_rgb
from main.core.console_text import ConsoleTextCleaner
from main.core.log_colors import contrast_ratio, themed_ansi_colors
from main.core.theme import glass_worst

THEMES = ("default", "light", "solarized_dark")


class SgrParsingChecks(unittest.TestCase):
    def test_attributes_set_and_reset(self):
        style = apply_sgr(PLAIN, "1;3;4;7;9")
        self.assertEqual((style.bold, style.italic, style.underline, style.inverse, style.strike),
                         (True, True, True, True, True))
        self.assertEqual(apply_sgr(style, "22;23;24;27;29"), PLAIN)
        self.assertEqual(apply_sgr(apply_sgr(PLAIN, "2"), "22"), PLAIN)
        self.assertTrue(apply_sgr(PLAIN, "21").underline)
        self.assertEqual(apply_sgr(style, "0"), PLAIN)
        self.assertEqual(apply_sgr(style, ""), PLAIN, "ESC[m is a full reset")

    def test_sixteen_colors_foreground_and_background(self):
        self.assertEqual(apply_sgr(PLAIN, "31").fg, ("i", 1))
        self.assertEqual(apply_sgr(PLAIN, "97").fg, ("i", 15))
        self.assertEqual(apply_sgr(PLAIN, "44").bg, ("i", 4))
        self.assertEqual(apply_sgr(PLAIN, "103").bg, ("i", 11))
        both = apply_sgr(PLAIN, "31;42")
        self.assertEqual(apply_sgr(both, "39"), SgrStyle(bg=("i", 2)))
        self.assertEqual(apply_sgr(both, "49"), SgrStyle(fg=("i", 1)))

    def test_256_and_truecolor_in_semicolon_and_colon_forms(self):
        self.assertEqual(apply_sgr(PLAIN, "38;5;208").fg, ("i", 208))
        self.assertEqual(apply_sgr(PLAIN, "48;5;17").bg, ("i", 17))
        self.assertEqual(apply_sgr(PLAIN, "38;2;255;128;0").fg, ("rgb", 255, 128, 0))
        self.assertEqual(apply_sgr(PLAIN, "48;2;1;2;3").bg, ("rgb", 1, 2, 3))
        self.assertEqual(apply_sgr(PLAIN, "38:5:208").fg, ("i", 208))
        self.assertEqual(apply_sgr(PLAIN, "38:2::255:128:0").fg, ("rgb", 255, 128, 0))
        self.assertEqual(apply_sgr(PLAIN, "38:2:255:128:0").fg, ("rgb", 255, 128, 0))
        combined = apply_sgr(PLAIN, "1;38;5;196;48;2;0;0;64;4")
        self.assertEqual((combined.bold, combined.fg, combined.bg, combined.underline),
                         (True, ("i", 196), ("rgb", 0, 0, 64), True))

    def test_malformed_parameters_are_skipped_never_thrown(self):
        for parameters in ("38;5", "38;5;999", "38;2;1;2", "48;9;1", "x;1", "1;;4", "38:2", "4:x",
                           ";" * 500, "9" * 50, "38;2;300;0;0"):
            apply_sgr(PLAIN, parameters)
        self.assertFalse(apply_sgr(PLAIN, "1;;4").bold, "an empty field is a reset, as in xterm")
        self.assertTrue(apply_sgr(PLAIN, "1;;4").underline, "then 4 applies")
        self.assertIsNone(apply_sgr(PLAIN, "38;5;999").fg)

    def test_xterm_table(self):
        self.assertEqual(xterm_rgb(16), (0, 0, 0))
        self.assertEqual(xterm_rgb(21), (0, 0, 255))
        self.assertEqual(xterm_rgb(196), (255, 0, 0))
        self.assertEqual(xterm_rgb(231), (255, 255, 255))
        self.assertEqual(xterm_rgb(232), (8, 8, 8))
        self.assertEqual(xterm_rgb(255), (238, 238, 238))

    def test_every_painted_foreground_reads_on_what_it_is_painted_on(self):
        samples = [SgrStyle(fg=("i", index)) for index in range(256)]
        samples += [SgrStyle(fg=("rgb", r, g, b)) for r in (0, 64, 128, 255) for g in (0, 128, 255) for b in (0, 255)]
        samples += [SgrStyle(fg=("i", 4), bg=("i", 4)), SgrStyle(fg=("rgb", 10, 10, 10), bg=("rgb", 0, 0, 0)),
                    SgrStyle(inverse=True), SgrStyle(fg=("i", 3), inverse=True), SgrStyle(bg=("i", 15))]
        for theme in THEMES:
            surface = glass_worst(theme)["reading"]
            for style in samples:
                foreground, background = resolve_colors(style, theme, "#e3edf6" if theme != "light" else "#24292f", surface)
                self.assertGreaterEqual(contrast_ratio(foreground, background or surface), 4.5, (theme, style))
            dim, _ = resolve_colors(SgrStyle(dim=True), theme, themed_ansi_colors(theme)[37], surface)
            self.assertGreaterEqual(contrast_ratio(dim, surface), 3.0, theme)
            # The 16 system colors are the terminals' own readable stand-ins.
            red, _ = resolve_colors(SgrStyle(fg=("i", 1)), theme, "#000000", surface)
            self.assertEqual(red, themed_ansi_colors(theme)[31])


class StreamChecks(unittest.TestCase):
    def test_sequences_split_across_reads_keep_text_and_style(self):
        stream = SgrStream()
        runs = []
        for part in ("plain \x1b[", "1;3", "1mred", " bold\x1b", "[0m done", "\x1b]0;title\x07!"):
            runs += stream.feed(part)
        text = "".join(piece for piece, _ in runs)
        self.assertEqual(text, "plain red bold done!")
        styled = [(piece, style) for piece, style in runs if not style.plain]
        self.assertEqual("".join(piece for piece, _ in styled), "red bold")
        self.assertTrue(all(style.bold and style.fg == ("i", 1) for _, style in styled))

    def test_runaway_escape_is_released_as_text(self):
        stream = SgrStream()
        runs = stream.feed("\x1b[" + "1" * 300 + "after")
        self.assertIn("after", "".join(piece for piece, _ in runs))

    def test_console_cleaner_keeps_clean_text_and_reports_runs(self):
        cleaner = ConsoleTextCleaner()
        self.assertEqual(cleaner.clean("a\x1b[2Jb\x1b[1;32mc\x1b[0m\td\x07e"), "abc\tde")
        cleaner.reset()
        text, runs = cleaner.clean_styled("x \x1b[31merror\x1b[0m: \x1b[1;4mhere")
        self.assertEqual(text, "x error: here")
        self.assertEqual([(start, length) for start, length, _ in runs], [(2, 5), (9, 4)])
        self.assertEqual(runs[0][2].fg, ("i", 1))
        self.assertTrue(runs[1][2].bold and runs[1][2].underline)
        text, runs = cleaner.clean_styled(" continued")
        self.assertEqual(runs, ((0, 10, apply_sgr(PLAIN, "1;4")),), "a style lasts into the next event")
        cleaner.reset()
        self.assertEqual(cleaner.clean_styled(" after clear"), (" after clear", ()))


class ViewChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from main.core import config
        self.stack = [patch.object(config, "load_gui_config", return_value={}),
                      patch.object(config, "get_theme_mode", return_value="default")]
        for item in self.stack:
            item.start()
            self.addCleanup(item.stop)

    def format_at(self, view, needle):
        from PySide6.QtGui import QTextCursor
        cursor = view.document().find(needle)
        self.assertFalse(cursor.isNull(), needle)
        probe = QTextCursor(view.document())
        probe.setPosition(cursor.selectionStart() + 1)
        return probe.charFormat()

    def test_serial_monitor_paints_styles_and_copies_plain_text(self):
        from main.qt.serial_panel import SerialOutputView
        view = SerialOutputView()
        self.addCleanup(view.deleteLater)
        for chunk in ("boot \x1b[1;3", "1mFAULT\x1b[0m ok \x1b[38;5;208mamber\x1b[39m ",
                      "\x1b[48;2;0;0;96;4mpanel\x1b[0m \x1b[7minv\x1b[27m \x1b[9mold\x1b[29m", "\n"):
            view.append_log({"text": chunk, "newline": False, "generation": 1})
        view._flush_queue()
        self.assertEqual(view.toPlainText(), "boot FAULT ok amber panel inv old\n")
        self.assertEqual(view.get_content_for_clipboard(False), "boot FAULT ok amber panel inv old\n")
        for theme in THEMES:
            view.apply_theme(theme)
            surface = glass_worst(theme)["reading"]
            fault = self.format_at(view, "FAULT")
            self.assertEqual(fault.foreground().color().name(), themed_ansi_colors(theme)[31])
            self.assertGreaterEqual(fault.fontWeight(), 700)
            amber = self.format_at(view, "amber").foreground().color().name()
            self.assertGreaterEqual(contrast_ratio(amber, surface), 4.5, theme)
            panel = self.format_at(view, "panel")
            self.assertEqual(panel.background().color().name(), "#000060")
            self.assertTrue(panel.fontUnderline())
            self.assertGreaterEqual(contrast_ratio(panel.foreground().color().name(), "#000060"), 4.5)
            self.assertTrue(self.format_at(view, "inv").background().style() != 0)
            self.assertTrue(self.format_at(view, "old").fontStrikeOut())
            self.assertFalse(self.format_at(view, "ok").fontWeight() >= 700)
        # Erase-screen still clears, and a new connection starts unstyled.
        view.append_log({"text": "\x1b[1;32mstill green", "newline": True, "generation": 1})
        view.append_log({"text": "fresh", "newline": True, "generation": 2})
        view._flush_queue()
        self.assertLess(self.format_at(view, "fresh").fontWeight(), 700)
        view.append_log({"text": "\x1b[2Jafter", "newline": True, "generation": 2})
        view._flush_queue()
        self.assertEqual(view.toPlainText(), "after\n")

    def test_build_console_paints_compiler_colors_and_keeps_diagnostics(self):
        from main.qt.console_panel import ConsolePanel
        view = ConsolePanel()
        self.addCleanup(view.deleteLater)
        view.append_log({"text": "src/main.cpp:3:1: \x1b[1;31merror:\x1b[0m expected ';'", "tag": "error",
                         "timestamp": "[10:00:00]"})
        view.append_log({"text": "[10:00:01] \x1b[32mSUCCESS\x1b[0m done", "tag": "normal"})
        view._flush_queue()
        self.assertIn("src/main.cpp:3:1: error: expected ';'", view.toPlainText())
        self.assertIn("SUCCESS done", view.toPlainText())
        self.assertNotIn("\x1b", view.toPlainText())
        for theme in THEMES:
            view.apply_theme(theme)
            error = self.format_at(view, "error:")
            self.assertEqual(error.foreground().color().name(), themed_ansi_colors(theme)[31])
            self.assertGreaterEqual(error.fontWeight(), 700)
            self.assertEqual(self.format_at(view, "SUCCESS").foreground().color().name(), themed_ansi_colors(theme)[32])
            # Unstyled text in a diagnostic keeps its semantic color.
            semantic = self.format_at(view, "expected").foreground().color().name()
            from main.core.log_colors import themed_log_colors
            self.assertEqual(semantic, themed_log_colors(theme)["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
