#!/usr/bin/env python3
"""Hardware-free integration checks for Build Console display and retained logs.

Uses real offscreen Qt widgets with persistence mocked. Starts no backend,
services, compiler, installer, serial connection, or live metadata generation.
"""
from __future__ import annotations

import os
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from main.core import config
from main.qt.console_panel import ConsolePanel, ConsolePanelContainer
from main.qt.log_colors import themed_log_colors
from src.modules.runtime_resources import performance_profile

APP = QApplication.instance() or QApplication([])


def flush(console):
    while console._queue:
        console._flush_queue()


class BuildConsoleChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config, "_load_raw_config", return_value={"shared": {}, "instances": {}}))
        self.stack.enter_context(patch.object(config, "load_gui_config", return_value={}))
        self.stack.enter_context(patch.object(config, "_save_raw_config", side_effect=AssertionError("live persistence write")))
        self.stack.enter_context(patch.object(config, "save_gui_config", side_effect=AssertionError("live settings write")))
        self.stack.enter_context(patch.object(config, "get_theme_mode", return_value="default"))
        self.stack.enter_context(patch.object(config, "get_monitor_font_size", return_value=11))
        self.stack.enter_context(patch.object(config, "get_hide_build_console_warnings", return_value=False))

    def widget(self, container=False):
        widget = ConsolePanelContainer() if container else ConsolePanel()
        widget.resize(960, 220)
        def dispose():
            widget.close()
            widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)
        return widget

    def add_unit(self, console, number):
        console.append_log({"text": f"  ⚙ Compiling source_{number:03d}.cpp.o...", "tag": "info", "timestamp": "[01:02:03]"})

    def test_default_activity_aggregates_units_and_details_copy_keep_raw(self):
        panel = self.widget(container=True)
        console = panel.console
        for number in range(32):
            self.add_unit(console, number)
        flush(console)
        self.assertFalse(console._details_visible)
        text = console.toPlainText()
        self.assertEqual(text.count("Compiling source units"), 1)
        self.assertIn("32 processed", text)
        self.assertIn("source_031.cpp.o", text)
        self.assertNotIn("source_000.cpp.o", text)
        raw = console.get_content_for_clipboard(False)
        self.assertIn("⚙ Compiling source_000.cpp.o...", raw)
        self.assertIn("⚙ Compiling source_031.cpp.o...", raw)
        self.assertNotIn("Compiling source units", raw)
        panel.header._btn_details.click()
        self.assertTrue(console._details_visible)
        self.assertTrue(panel.header._btn_details.isChecked())
        self.assertEqual(console.toPlainText(), raw)
        with patch("main.qt.console_panel.QTimer.singleShot"):
            panel.header._copy_console()
        self.assertEqual(APP.clipboard().text(), raw)
        panel.header._btn_details.click()
        self.assertIn("32 processed", console.toPlainText())

    def test_diagnostics_source_carets_and_unknown_rows_are_run_barriers(self):
        console = self.widget()
        self.add_unit(console, 0)
        self.add_unit(console, 1)
        preserved = [
            ("src/main.cpp:12: error: call is ambiguous", "error"),
            ('  12 | compile("first candidate");', "normal"),
            ("     | ^~~~~~~", "dim"),
            ("src/main.cpp:3: note: candidate declared here", "info"),
            ("Compiling failed to create target .o", "normal"),
        ]
        for text, tag in preserved:
            console.append_log({"text": text, "tag": tag})
        self.add_unit(console, 2)
        flush(console)
        displayed = console.toPlainText()
        self.assertEqual(displayed.count("Compiling source units"), 2)
        for text, _ in preserved:
            self.assertIn(text, displayed)
            self.assertIn(text, console.get_content_for_clipboard(False))
        self.assertLess(displayed.index("2 processed"), displayed.index(preserved[0][0]))
        self.assertLess(displayed.index(preserved[-1][0]), displayed.index("1 processed", displayed.index(preserved[0][0])))

    def test_hidden_warnings_stay_in_details_history_and_copy(self):
        console = self.widget()
        console.set_hide_warnings(True)
        console.append_log({"text": "driver.cpp:7: warning: unused value", "tag": "warning"})
        console.append_log({"text": "fatal error context", "tag": "error"})
        console.append_log({"text": "[Display backlog: 2 older entries omitted to protect memory]", "tag": "system"})
        flush(console)
        self.assertNotIn("unused value", console.toPlainText())
        self.assertIn("fatal error context", console.toPlainText())
        self.assertIn("older entries omitted", console.toPlainText())
        self.assertIn("unused value", console.get_content_for_clipboard(False))
        console.set_details_visible(True)
        self.assertNotIn("unused value", console.toPlainText())
        console.set_hide_warnings(False)
        self.assertIn("unused value", console.toPlainText())
        self.assertEqual(len(console._entries), 3)

    def test_copy_flushes_pending_messages_without_waiting_for_timer(self):
        console = self.widget()
        console.append_log({"text": "pending compiler diagnostic", "tag": "error"})
        self.assertTrue(console._flush_timer.isActive())
        self.assertEqual(console.get_content_for_clipboard(False), "pending compiler diagnostic")
        self.assertFalse(console._queue)
        self.assertFalse(console._flush_timer.isActive())

    def test_compact_options_follow_external_preference_changes_without_writes(self):
        panel = self.widget(container=True)
        panel.header.sync_clear_on_action(False)
        panel.header.sync_clear_serial_on_action(True)
        actions = panel.header._btn_options.menu().actions()
        self.assertFalse(actions[0].isChecked())
        self.assertTrue(actions[1].isChecked())

    def test_unknown_tags_and_progress_patterns_keep_caches_bounded(self):
        console = self.widget()
        for start in range(0, 320, 64):
            for number in range(start, start + 64):
                console.append_log({"text": f"unknown event {number}", "tag": f"unknown-{number}"})
            flush(console)
        self.assertLessEqual(len(console._formats), len(console._tag_colors) + 1)
        for start in range(0, 320, 64):
            for number in range(start, start + 64):
                console.append_log({"text": f"progress {number}", "tag": "info", "replace_pattern": rf"progress {number}\b"})
            flush(console)
        self.assertLessEqual(len(console._patterns), 128)
        self.assertLessEqual(len(console._progress_blocks), 128)
        # A cache-evicted progress row inside the bounded lookup horizon must
        # update by identity rather than duplicate itself in the document.
        console.append_log({"text": "progress 100 updated", "tag": "info", "replace_pattern": r"progress 100\b"})
        flush(console)
        self.assertEqual(console.toPlainText().count("progress 100"), 1)
        console.clear()
        self.assertFalse(console._patterns)
        self.assertFalse(console._progress_blocks)

    def test_producer_timestamps_and_multiline_content_survive_toggle(self):
        console = self.widget()
        console.append_log({"text": "first\n  3 | call();\n    ^~~~~", "timestamp": "[07:08:09]"})
        console.append_log({"text": "[01:02:03.125] diagnostic\n  9 | source();", "timestamp": "[10:11:12]"})
        flush(console)
        raw = "first\n  3 | call();\n    ^~~~~\ndiagnostic\n  9 | source();"
        self.assertEqual(console.get_content_for_clipboard(False), raw)
        console.set_timestamp_enabled(True)
        stamped = console.get_content_for_clipboard(True)
        self.assertEqual(stamped, "[07:08:09] first\n  3 | call();\n    ^~~~~\n[01:02:03.125] diagnostic\n  9 | source();")
        self.assertEqual(console.toPlainText(), stamped)
        console.set_timestamp_enabled(False)
        self.assertEqual(console.toPlainText(), raw)

    def test_fragmented_ansi_and_osc_controls_are_cleaned_and_clear_resets(self):
        console = self.widget()
        for text in ("\x1b[3", "1mred", "\x1b[0m normal", "\x1b]0;invisible title", "\x1b", "\\ visible"):
            console.append_log({"text": text, "newline": False})
        flush(console)
        self.assertEqual(console.toPlainText(), "red normal visible")
        self.assertEqual(console.get_content_for_clipboard(False), "red normal visible")
        console.append_log({"text": "\x1b[", "newline": False})
        console.clear()
        console.append_log({"text": "m new event"})
        flush(console)
        self.assertEqual(console.toPlainText(), "m new event")
        self.assertNotIn("red", console.get_content_for_clipboard(False))

    def test_progress_final_values_respect_diagnostic_barriers(self):
        console = self.widget()
        pattern = r"Writing \[firmware\.bin\]"
        for percent in range(100):
            console.append_log({"text": f"Writing [firmware.bin] {percent}%", "tag": "info", "replace_pattern": pattern})
        flush(console)
        self.assertEqual(console.toPlainText(), "Writing [firmware.bin] 99%")
        console.append_log({"text": "flash warning; preserve context", "tag": "warning"})
        console.append_log({"text": "  8 | Writing [firmware.bin] appears in source", "tag": "normal"})
        console.append_log({"text": "    ^~~~~", "tag": "dim"})
        console.append_log({"text": "Writing [firmware.bin] 100%", "tag": "success", "replace_pattern": pattern})
        flush(console)
        displayed = console.toPlainText()
        self.assertEqual(displayed.count("Writing [firmware.bin]"), 3)
        self.assertIn("Writing [firmware.bin] 99%", displayed)
        self.assertIn("appears in source", displayed)
        self.assertIn("    ^~~~~", displayed)
        self.assertTrue(displayed.endswith("Writing [firmware.bin] 100%"))
        console.set_details_visible(True)
        self.assertIn("appears in source", console.toPlainText())

    def test_invalid_patterns_fall_back_to_individual_messages(self):
        console = self.widget()
        for text in ("first fallback diagnostic", "second fallback diagnostic"):
            console.append_log({"text": text, "replace_pattern": "[invalid"})
        flush(console)
        self.assertEqual(console.toPlainText(), "first fallback diagnostic\nsecond fallback diagnostic")
        self.assertEqual(console.get_content_for_clipboard(False), console.toPlainText())

    def test_progress_never_moves_across_notes_or_ordinary_output(self):
        console = self.widget()
        for details in (False, True):
            console.clear()
            console.set_details_visible(details)
            pattern = r"Writing firmware"
            console.append_log({"text": "Writing firmware 50%", "replace_pattern": pattern})
            console.append_log({"text": "candidate note: preserve its position", "tag": "info"})
            console.append_log({"text": "Writing firmware 100%", "replace_pattern": pattern})
            flush(console)
            expected = "Writing firmware 50%\ncandidate note: preserve its position\nWriting firmware 100%"
            self.assertEqual(console.toPlainText(), expected)
            self.assertEqual(console.get_content_for_clipboard(False), expected)

    def test_drain_and_clear_stop_idle_timer_and_never_resurrect_output(self):
        console = self.widget()
        self.assertFalse(console._flush_timer.isActive())
        console.append_log({"text": "queued build message"})
        self.assertTrue(console._flush_timer.isActive())
        flush(console)
        self.assertFalse(console._flush_timer.isActive())
        console.append_log({"text": "pending build message"})
        console.clear()
        self.assertFalse(console._flush_timer.isActive())
        self.assertFalse(console._queue)
        self.assertFalse(console._entries)
        self.assertFalse(console._activity_entries)
        console.set_details_visible(True)
        console.set_timestamp_enabled(True)
        console.apply_theme("light")
        self.assertEqual(console.toPlainText(), "")
        self.assertEqual(console.get_content_for_clipboard(False), "")

    def test_queue_history_and_no_newline_streams_stay_bounded(self):
        for cores in (4, 6, 8):
            with self.subTest(cores=cores), patch("src.modules.runtime_resources.performance_profile", return_value=performance_profile(cores, 8)):
                console = self.widget()
                console.set_hide_warnings(True)
                console.append_log({"text": "early fatal diagnostic", "tag": "error"})
                for number in range(6000):
                    console.append_log({"text": f"ordinary {number}: " + "x" * 180, "newline": False})
                self.assertLessEqual(console._queue.chars, console._queue.max_chars)
                self.assertLessEqual(len(console._queue), console._queue.max_items)
                flush(console)
                self.assertIn("early fatal diagnostic", console.get_content_for_clipboard(False))
                self.assertIn("older entries omitted", console.get_content_for_clipboard(False))
                self.assertLessEqual(console.document().characterCount(), console._history_limit + 1)
                self.assertLessEqual(console.document().lastBlock().length(), 16384)
                for history in (console._entries, console._activity_entries):
                    self.assertLessEqual(history.chars, history.max_chars)
                    self.assertLessEqual(len(history), history.max_items)
                self.assertFalse(console._flush_timer.isActive())

    def test_theme_timestamp_changes_preserve_selection_focus_and_reading_anchor(self):
        for details in (False, True):
            with self.subTest(details=details):
                console = self.widget()
                console.set_details_visible(details)
                console.show()
                APP.processEvents()
                for number in range(100):
                    console.append_log({"text": f"Row {number:03d} " + "x" * 120, "timestamp": "[01:02:03]"})
                flush(console)
                APP.processEvents()
                console.set_autoscroll(False)
                console.setTextCursor(console.document().find("Row 040"))
                console.setFocus()
                console.verticalScrollBar().setValue(35)
                console.horizontalScrollBar().setValue(50)
                visible = console.firstVisibleBlock().text()
                horizontal = console.horizontalScrollBar().value()
                for mode in ("light", "solarized_dark", "default"):
                    console.apply_theme(mode)
                    self.assertEqual(console.firstVisibleBlock().text(), visible)
                    self.assertEqual(console.textCursor().selectedText(), "Row 040")
                    self.assertTrue(console.hasFocus())
                    self.assertEqual(console.horizontalScrollBar().value(), horizontal)
                    color = console.document().find("Row 040").charFormat().foreground().color().name()
                    self.assertEqual(color, themed_log_colors(mode)["normal"])
                console.set_timestamp_enabled(True)
                self.assertTrue(console.firstVisibleBlock().text().endswith(visible),
                                f"timestamp anchor changed from {visible!r} to {console.firstVisibleBlock().text()!r}; "
                                f"scroll={console.verticalScrollBar().value()}")
                self.assertEqual(console.textCursor().selectedText(), "Row 040")
                console.set_timestamp_enabled(False)
                self.assertEqual(console.firstVisibleBlock().text(), visible)
                console.append_log({"text": "new output while reading"})
                flush(console)
                self.assertEqual(console.firstVisibleBlock().text(), visible)
                self.assertEqual(console.textCursor().selectedText(), "Row 040")
                self.assertTrue(console.hasFocus())
                console.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
