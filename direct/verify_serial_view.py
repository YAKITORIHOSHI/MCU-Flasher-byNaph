#!/usr/bin/env python3
"""Hardware-free serial display, bounded stream and clipboard regressions."""
from __future__ import annotations

import os
import re
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QTimer, Qt
from PySide6.QtGui import QContextMenuEvent, QTextCursor, QTextOption
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QPlainTextEdit
from main.core import config
from main.qt.serial_panel import SerialOutputView, SerialPanel
from src.modules.runtime_resources import performance_profile

APP = QApplication.instance() or QApplication([])


class BusyClipboard:
    def __init__(self, refusals):
        self.refusals = refusals
        self.calls = []
        self.content = ""

    def setText(self, text):
        self.calls.append(text)
        if self.refusals:
            self.refusals -= 1
        else:
            self.content = text

    def text(self):
        return self.content


class SerialViewChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Restore once after the suite; repeatedly reclaiming the native
        # clipboard between fixtures can race desktop clipboard managers.
        original = APP.clipboard().mimeData()
        saved = QMimeData()
        if original is not None:
            for fmt in original.formats():
                saved.setData(fmt, original.data(fmt))
        cls.addClassCleanup(APP.clipboard().setMimeData, saved)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config, "_load_raw_config", return_value={"shared": {}, "instances": {}}))
        self.stack.enter_context(patch.object(config, "load_gui_config", return_value={}))
        self.stack.enter_context(patch.object(config, "_save_raw_config", side_effect=AssertionError("live persistence write")))
        self.stack.enter_context(patch.object(config, "save_gui_config", side_effect=AssertionError("live settings write")))
        self.stack.enter_context(patch.object(config, "get_theme_mode", return_value="default"))
        self.stack.enter_context(patch.object(config, "get_monitor_font_size", return_value=11))
        self.view = SerialOutputView()
        self.view.set_timestamp_enabled(False)
        def dispose():
            self.view._flush_timer.stop()
            self.view.close()
            self.view.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)

    def feed(self, text, *, newline=False, **extra):
        self.view.append_log(dict(text=text, newline=newline, **extra))
        self.view._flush_queue()

    def panel(self):
        panel = SerialPanel(None)
        panel._output.set_timestamp_enabled(False)
        def dispose():
            panel.close()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)
        return panel

    def wait_for_copy(self, panel):
        deadline = time.monotonic() + 1.0
        while panel._copy_active and time.monotonic() < deadline:
            APP.processEvents()
            QTest.qWait(5)
        self.assertFalse(panel._copy_active, "Bounded clipboard attempts did not finish")

    def send_context_event(self, view):
        # QAbstractScrollArea routes context events through its viewport.
        # Bound unexpected native-menu routing so a failed fixture cannot hang.
        expired = []
        watchdog = QTimer(view)
        watchdog.setSingleShot(True)
        def close_unexpected_popup():
            expired.append(True)
            popup = APP.activePopupWidget()
            if popup is not None:
                popup.close()
        watchdog.timeout.connect(close_unexpected_popup)
        watchdog.start(1000)
        event = QContextMenuEvent(QContextMenuEvent.Reason.Keyboard,
                                  QPoint(5, 5), view.viewport().mapToGlobal(QPoint(5, 5)))
        try:
            APP.sendEvent(view.viewport(), event)
        finally:
            watchdog.stop()
            watchdog.deleteLater()
        self.assertFalse(expired, "Native context event bypassed the owned Copy menu")

    def inspect_context_event(self, view, inspect):
        # Shiboken's native QMenu.exec descriptor ignores a class monkeypatch.
        # Override it on a real Qt subclass while retaining parent ownership.
        class FixtureMenu(QMenu):
            def exec(menu, position):
                return inspect(menu, position)
        with patch("main.qt.serial_panel.QMenu", FixtureMenu):
            self.send_context_event(view)

    def test_default_wrap_makes_message_after_escaped_boot_noise_visible(self):
        self.view.resize(420, 150)
        self.view.show()
        APP.processEvents()
        text = "\\x03\\xe4\\x1b\\x83" * 70 + "Hello from MCU Flasher by Naph!"
        self.feed(text, newline=True)
        APP.processEvents()
        self.assertEqual(self.view.lineWrapMode(), QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.assertEqual(self.view.document().defaultTextOption().wrapMode(),
                         QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.assertEqual(self.view.horizontalScrollBar().maximum(), 0)
        self.assertGreater(self.view.document().firstBlock().layout().lineCount(), 1)
        message_end = QTextCursor(self.view.document())
        message_end.setPosition(len(text))
        self.assertTrue(self.view.viewport().rect().intersects(self.view.cursorRect(message_end)))
        self.assertEqual(self.view.toPlainText(), text + "\n")
        self.assertEqual(self.view.get_content_for_clipboard(), text + "\n")

    def test_wrap_reflows_on_resize_without_adding_copy_line_breaks(self):
        self.view.resize(850, 150)
        self.view.show()
        APP.processEvents()
        text = "\\x00\\xa3" * 140 + "Hello from MCU Flasher by Naph!"
        self.feed(text)
        APP.processEvents()
        original = self.view.get_content_for_clipboard()
        wide_rows = self.view.document().firstBlock().layout().lineCount()
        self.view.resize(320, 150)
        APP.processEvents()
        self.assertGreater(self.view.document().firstBlock().layout().lineCount(), wide_rows)
        self.assertEqual(self.view.horizontalScrollBar().maximum(), 0)
        self.assertEqual(self.view.get_content_for_clipboard(), original)
        self.view.selectAll()
        self.view.copy()
        self.assertEqual(APP.clipboard().text(), text)
        self.assertNotIn("\n", APP.clipboard().text())
        self.view.set_line_wrap_enabled(False)
        APP.processEvents()
        self.assertGreater(self.view.horizontalScrollBar().maximum(), 0)
        self.assertEqual(self.view.textCursor().selectedText(), text)
        self.view.set_line_wrap_enabled(True)
        APP.processEvents()
        self.assertEqual(self.view.horizontalScrollBar().maximum(), 0)
        self.assertEqual(self.view.toPlainText(), text)
        self.assertEqual(self.view.get_content_for_clipboard(), original)

    def test_saved_wrap_preference_is_loaded_without_persistence(self):
        with patch.object(config, "load_gui_config", return_value={"serial_line_wrap": False}):
            view = SerialOutputView()
            try:
                self.assertFalse(view._line_wrap_enabled)
                self.assertEqual(view.lineWrapMode(), QPlainTextEdit.LineWrapMode.NoWrap)
                self.assertEqual(view.document().defaultTextOption().wrapMode(), QTextOption.WrapMode.NoWrap)
            finally:
                view.close()
                view.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_options_and_context_wrap_choice_save_then_sync(self):
        panel = self.panel()
        with patch.object(config, "save_gui_config", return_value=True) as save:
            panel._wrap_action.trigger()
            self.assertFalse(panel._output._line_wrap_enabled)
            self.assertFalse(panel._wrap_action.isChecked())
            save.assert_called_once_with({"serial_line_wrap": False}, shared_updates={"serial_line_wrap": False})
            save.reset_mock()

            def choose_wrap(menu, _position):
                action = next(action for action in menu.actions()
                              if action.objectName() == "serial-wrap-lines")
                self.assertTrue(action.isCheckable())
                self.assertFalse(action.isChecked())
                self.assertIn("original line breaks", action.toolTip())
                action.trigger()
            self.inspect_context_event(panel._output, choose_wrap)
            self.assertTrue(panel._output._line_wrap_enabled)
            self.assertTrue(panel._wrap_action.isChecked())
            save.assert_called_once_with({"serial_line_wrap": True}, shared_updates={"serial_line_wrap": True})

    def test_failed_wrap_write_preserves_display_and_restores_menu_choices(self):
        panel = self.panel()
        with patch.object(config, "save_gui_config", return_value=False), \
             patch("main.qt.preferences.report_preference_failure") as failure:
            panel._wrap_action.trigger()
            self.assertTrue(panel._output._line_wrap_enabled)
            self.assertTrue(panel._wrap_action.isChecked())
            failure.assert_called_once_with("Serial line wrapping")
            failure.reset_mock()

            def choose_wrap(menu, _position):
                action = next(action for action in menu.actions()
                              if action.objectName() == "serial-wrap-lines")
                action.trigger()
                self.assertTrue(action.isChecked())
            self.inspect_context_event(panel._output, choose_wrap)
            self.assertTrue(panel._output._line_wrap_enabled)
            self.assertEqual(panel._output.lineWrapMode(), QPlainTextEdit.LineWrapMode.WidgetWidth)
            failure.assert_called_once_with("Serial line wrapping")

    def test_header_copy_retries_one_snapshot_when_clipboard_temporarily_refuses(self):
        panel = self.panel()
        panel._backend = SimpleNamespace(flush_serial_output=Mock())
        panel._output.append_log({"text": "original snapshot", "newline": False})
        clipboard = BusyClipboard(2)
        with patch("main.qt.serial_panel.QApplication.clipboard", return_value=clipboard), \
             patch.object(panel._output, "get_content_for_clipboard", wraps=panel._output.get_content_for_clipboard) as snapshot:
            panel._copy_output()
            self.assertNotIn("Copied", panel.btn_copy.text())
            self.assertIs(panel._copy_retry_timer.parent(), panel)
            panel._output.append_log({"text": " later device output", "newline": False})
            self.wait_for_copy(panel)
            self.assertEqual(clipboard.calls, ["original snapshot"] * 3)
            self.assertEqual(clipboard.text(), "original snapshot")
            snapshot.assert_called_once()
            panel._backend.flush_serial_output.assert_called_once()
            self.assertIn("Copied", panel.btn_copy.text())

    def test_header_copy_reports_failure_after_three_refused_attempts(self):
        panel = self.panel()
        panel._output.append_log({"text": "retained output", "newline": False})
        clipboard = BusyClipboard(10)
        with patch("main.qt.serial_panel.QApplication.clipboard", return_value=clipboard):
            panel._copy_output()
            self.wait_for_copy(panel)
            QTest.qWait(80)
            self.assertEqual(clipboard.calls, ["retained output"] * 3)
            self.assertEqual(panel.btn_copy.text(), "Copy failed")
            self.assertIn("did not accept", panel.btn_copy.toolTip())
            self.assertFalse(panel._copy_retry_timer.isActive())

    def test_newer_header_or_selection_copy_cancels_old_clipboard_retry(self):
        for selection in (False, True):
            with self.subTest(selection=selection):
                panel = self.panel()
                panel._output.append_log({"text": "header and selected", "newline": False})
                clipboard = BusyClipboard(1)
                with patch("main.qt.serial_panel.QApplication.clipboard", return_value=clipboard):
                    panel._copy_output()
                    old_generation = panel._copy_generation
                    if selection:
                        panel._output.setTextCursor(panel._output.document().find("selected"))
                        panel._output.copy()
                        expected = "selected"
                    else:
                        panel._output.clear()
                        panel._output.append_log({"text": "new header snapshot", "newline": False})
                        panel._copy_output()
                        expected = "new header snapshot"
                    panel._attempt_copy_output(old_generation)
                    QTest.qWait(80)
                    self.assertEqual(clipboard.calls, ["header and selected", expected])
                    self.assertEqual(clipboard.text(), expected)
                    self.assertFalse(panel._copy_retry_timer.isActive())

    def test_actual_keyboard_and_context_copy_cancel_pending_header_retry(self):
        for route in ("keyboard", "context"):
            with self.subTest(route=route):
                panel = self.panel()
                panel._output.append_log({"text": "header and selected", "newline": False})
                clipboard = BusyClipboard(1)
                with patch("main.qt.serial_panel.QApplication.clipboard", return_value=clipboard):
                    panel._copy_output()
                    old_generation = panel._copy_generation
                    panel._output.setTextCursor(panel._output.document().find("selected"))
                    if route == "keyboard":
                        QTest.keyClick(panel._output, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
                    else:
                        def choose_copy(menu, _position):
                            self.assertIs(menu.parent(), panel._output)
                            action = next(action for action in menu.actions()
                                          if action.objectName() == "serial-copy-selection")
                            action.trigger()
                        self.inspect_context_event(panel._output, choose_copy)
                    panel._attempt_copy_output(old_generation)
                    QTest.qWait(80)
                    self.assertEqual(clipboard.calls, ["header and selected", "selected"])
                    self.assertEqual(clipboard.text(), "selected")
                    self.assertFalse(panel._copy_retry_timer.isActive())

    def test_native_context_menu_keeps_select_all_and_disables_empty_copy(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                self.view.clear()
                if populated:
                    self.feed("one\ntwo")
                def inspect(menu, _position):
                    self.assertIs(menu.parent(), self.view)
                    actions = [action for action in menu.actions() if not action.isSeparator()]
                    copy = next(action for action in actions
                                if action.objectName() == "serial-copy-selection")
                    self.assertFalse(copy.isEnabled())
                    select_all = next(action for action in actions
                                      if action.objectName() == "serial-select-all")
                    self.assertEqual(select_all.isEnabled(), populated)
                    if populated:
                        select_all.trigger()
                self.inspect_context_event(self.view, inspect)
                if populated:
                    self.assertEqual(self.view.textCursor().selectedText().replace("\u2029", "\n"), "one\ntwo")

    def test_controls_unicode_and_real_clipboard_preserve_full_retained_output(self):
        self.view.append_log({"text": "sd\x00after\x07\x08 Ελληνικά 日本語 🌍\nnext", "newline": False})
        text = self.view.get_content_for_clipboard()
        self.assertEqual(text, "sd\\x00after\\x07\\x08 Ελληνικά 日本語 🌍\nnext")
        self.assertEqual(self.view.toPlainText(), text)
        APP.clipboard().setText(text)
        self.assertEqual(APP.clipboard().text(), text)
        self.assertIn("after", APP.clipboard().text())

    def test_copy_drains_pending_view_output(self):
        self.feed("first", newline=True)
        self.view.append_log({"text": "pending prompt> ", "newline": False})
        self.assertEqual(self.view.get_content_for_clipboard(), "first\npending prompt> ")
        self.assertEqual(self.view.toPlainText(), "first\npending prompt> ")

    def test_logical_line_timestamps_and_device_authored_prefixes(self):
        self.view.set_timestamp_enabled(True)
        self.feed("par")
        self.feed("tial\nsecond\n[12:34:56] device says hello")
        stamped = self.view.get_content_for_clipboard()
        self.assertEqual(len(re.findall(r"^\[\d\d:\d\d:\d\d\] ", stamped, re.M)), 3)
        self.assertEqual(self.view.get_content_for_clipboard(False),
                         "partial\nsecond\n[12:34:56] device says hello")
        self.assertEqual(stamped, self.view.toPlainText())
        self.view.set_timestamp_enabled(False)
        self.assertIn("[12:34:56] device says hello", self.view.toPlainText())
        self.view.selectAll()
        self.assertTrue(self.view.textCursor().hasSelection())
        self.assertEqual(self.view.textCursor().selectedText().replace("\u2029", "\n"), self.view.toPlainText())
        self.view.copy()
        self.assertEqual(APP.clipboard().text(), self.view.toPlainText())

    def test_fragmented_ansi_and_screen_clear_keep_history_consistent(self):
        self.feed("old screen\n\x1b[")
        self.feed("31mred\x1b]title")
        self.feed("\x07 text\x1b[H")
        self.assertEqual(self.view.get_content_for_clipboard(), "old screen\nred text")
        self.feed("\x1b[")
        self.feed("2Jnew screen")
        self.assertEqual(self.view.get_content_for_clipboard(), "new screen")
        self.view.set_timestamp_enabled(True)
        self.assertNotIn("old screen", self.view.toPlainText())
        self.assertIn("new screen", self.view.toPlainText())

    def test_copy_pending_escape_preserves_parser_and_eof_makes_tail_visible(self):
        self.feed("hello\x1b[", timestamp="[01:02:03]")
        self.assertEqual(self.view.get_content_for_clipboard(), "hello\\x1b[")
        self.assertEqual(self.view.toPlainText(), "hello")
        self.feed("31m red\x1b[", timestamp="04:05:06")
        self.assertEqual(self.view.get_content_for_clipboard(), "hello red\\x1b[")
        self.feed("", stream_end=True)
        self.assertEqual(self.view.toPlainText(), "hello red\\x1b[")
        self.assertEqual(self.view.get_content_for_clipboard(), self.view.toPlainText())
        self.view.set_timestamp_enabled(True)
        self.assertTrue(self.view.get_content_for_clipboard().startswith("[01:02:03] hello"))

    def test_pending_escape_keeps_original_timestamp_and_tag_at_eof_or_reconnect(self):
        for ending in ({"stream_end": True}, {"stream": False, "generation": 2}):
            with self.subTest(ending=ending):
                self.view.clear()
                self.view.set_timestamp_enabled(True)
                self.feed("\x1b[", timestamp="01:02:03", tag="warning", generation=1)
                before = self.view.get_content_for_clipboard()
                self.feed("", timestamp="04:05:06", tag="normal", **ending)
                self.assertEqual(self.view.get_content_for_clipboard(), before)
                self.assertEqual(next(iter(self.view._entries))[1:4], ("warning", False, "01:02:03"))

    def test_resume_and_theme_prebound_multi_megabyte_paragraphs(self):
        for cores in (4, 6, 12):
            with self.subTest(cores=cores), patch("src.modules.runtime_resources.performance_profile",
                                                return_value=performance_profile(cores, 16)):
                view = SerialOutputView()
                try:
                    view.set_timestamp_enabled(False)
                    view.set_paused(True)
                    for number in range(250):
                        # Include surrogate-pair characters so the pre-insertion
                        # paragraph limit is also valid in Qt's UTF-16 units.
                        text = f"{number:03d}:" + "🌍" * 7996
                        view.append_log({"text": text, "newline": False})
                        view._flush_queue()
                    retained = view.get_content_for_clipboard()
                    self.assertGreater(len(retained), 480000 if cores < 12 else 1900000)
                    self.assertEqual(view.toPlainText(), "")
                    before_entries = tuple(view._entries)
                    inserted = []
                    original = view._insert_chunks

                    def inspect(cursor, chunks):
                        inserted.append("".join(text for text, _ in chunks))
                        for paragraph in inserted[-1].split("\n"):
                            self.assertLessEqual(len(paragraph.encode("utf-16-le")) // 2, 16384)
                        original(cursor, chunks)

                    with patch.object(view, "_insert_chunks", side_effect=inspect):
                        start = time.monotonic()
                        view.set_paused(False)
                        view.apply_theme("solarized_dark")
                        elapsed = time.monotonic() - start
                    self.assertLess(elapsed, 3.0, f"Long-line Resume/theme took {elapsed:.2f}s on profile {cores}")
                    self.assertEqual(view.get_content_for_clipboard(), retained)
                    self.assertEqual(tuple(view._entries), before_entries)
                    self.assertIn("[Long line display truncated] ", view.toPlainText())
                    self.assertTrue(view.toPlainText().endswith(retained[-4000:]))
                    self.assertLessEqual(view.document().lastBlock().length(), 16384)
                finally:
                    view._flush_timer.stop()
                    view.close()
                    view.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_theme_keeps_selected_prefix_of_existing_truncated_ascii_line(self):
        target = "KEEP_ME_NEAR_VISIBLE_BOUNDARY"
        text = "x" * 15859 + target
        text += "y" * (24000 - len(text))
        for offset in range(0, len(text), 8000):
            self.feed(text[offset:offset + 8000])
        visible = self.view.toPlainText()
        self.assertIn("[Long line display truncated] ", visible)
        self.view.setTextCursor(self.view.document().find(target))
        self.assertEqual(self.view.textCursor().selectedText(), target)
        self.view.apply_theme("solarized_dark")
        self.assertEqual(self.view.toPlainText(), visible)
        self.assertEqual(self.view.textCursor().selectedText(), target)
        self.assertEqual(self.view.get_content_for_clipboard(), text)

    def test_ansi_clear_disabled_and_overlong_escape_do_not_hide_unbounded_text(self):
        self.view.set_ansi_clear_enabled(False)
        self.feed("keep\x1b[2Jstill here")
        self.assertEqual(self.view.get_content_for_clipboard(), "keepstill here")
        self.feed("\x1b]" + "x" * 300 + "tail")
        self.assertIn("\\x1b]", self.view.get_content_for_clipboard())
        self.assertTrue(self.view.get_content_for_clipboard().endswith("tail"))
        self.assertLessEqual(len(self.view._ansi_parser._pending), 256)

    def test_clear_discards_pending_escape_and_queued_old_output(self):
        self.feed("before\x1b[")
        self.view.append_log({"text": "queued before clear", "newline": False})
        self.view.clear()
        self.feed("after")
        self.assertEqual(self.view.get_content_for_clipboard(), "after")
        self.assertEqual(self.view.toPlainText(), "after")

    def test_host_records_and_new_generations_cannot_complete_previous_escape(self):
        self.feed("prompt> \x1b[", generation=1)
        self.feed("connected", stream=False, newline=True, generation=2)
        self.feed("2Jactual device text", generation=2)
        text = self.view.get_content_for_clipboard()
        self.assertIn("prompt> \\x1b[\nconnected\n2Jactual device text", text)

    def test_pause_retains_bounded_history_and_copy_without_moving_visible_text(self):
        self.feed("visible", newline=True)
        self.view.set_paused(True)
        self.feed("during pause", newline=True)
        self.assertEqual(self.view.toPlainText(), "visible\n")
        self.assertEqual(self.view.get_content_for_clipboard(), "visible\nduring pause\n")
        self.assertEqual(self.view.toPlainText(), "visible\n")
        self.view.set_paused(False)
        self.assertEqual(self.view.toPlainText(), "visible\nduring pause\n")
        self.view.set_paused(True)
        for number in range(4000):
            self.view.append_log({"text": f"{number}:" + "x" * 200, "newline": False})
        self.assertLessEqual(self.view._queue.chars, self.view._queue.max_chars)
        copied = self.view.get_content_for_clipboard()
        self.assertIn("older entries omitted", copied)
        self.assertIn("3999:", copied)
        self.assertLessEqual(self.view._entries.chars, self.view._entries.max_chars)
        self.view.set_paused(False)
        self.assertIn("3999:", self.view.toPlainText())

    def test_copy_retains_text_shortened_only_in_visual_long_lines(self):
        for number in range(20):
            self.feed(f"{number}:" + "x" * 2000)
        copied = self.view.get_content_for_clipboard()
        self.assertIn("0:", copied)
        self.assertIn("19:", copied)
        self.assertGreater(len(copied), 40000)
        self.assertLessEqual(self.view.document().lastBlock().length(), 16384)
        self.assertIn("Long line display truncated", self.view.toPlainText())
        self.assertLessEqual(self.view._entries.chars, self.view._entries.max_chars)


if __name__ == "__main__":
    unittest.main(verbosity=2)
