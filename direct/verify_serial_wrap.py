#!/usr/bin/env python3
"""Hardware-free serial wrapping and visual-row following regressions.

Fixtures use real Qt layout with mocked settings, never open a port and do not
change firmware baud, reset hardware or write project metadata.
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

from PySide6.QtCore import QCoreApplication, QEvent, QPoint
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from main.core import config
from main.qt.serial_panel import SerialOutputView
from src.modules.runtime_resources import performance_profile

APP = QApplication.instance() or QApplication([])


class SerialWrapChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config, "_load_raw_config", return_value={"shared": {}, "instances": {}}))
        self.stack.enter_context(patch.object(config, "load_gui_config", return_value={}))
        self.stack.enter_context(patch.object(config, "_save_raw_config", side_effect=AssertionError("live persistence write")))
        self.stack.enter_context(patch.object(config, "save_gui_config", side_effect=AssertionError("live settings write")))
        self.stack.enter_context(patch.object(config, "get_theme_mode", return_value="default"))
        self.stack.enter_context(patch.object(config, "get_monitor_font_size", return_value=11))

    def view(self, cores=None):
        profile_patch = patch("src.modules.runtime_resources.performance_profile",
                              return_value=performance_profile(cores, 8)) if cores else None
        if profile_patch:
            self.stack.enter_context(profile_patch)
        view = SerialOutputView()
        view.set_timestamp_enabled(False)
        view.resize(430, 190)
        view.show()
        APP.processEvents()
        def dispose():
            view._flush_timer.stop()
            view.close()
            view.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)
        return view

    @staticmethod
    def feed(view, text, *, newline=False):
        view.append_log(dict(text=text, newline=newline, timestamp="12:34:56"))
        while view._queue:
            view._flush_queue()
        APP.processEvents()

    @staticmethod
    def row_at_top(view):
        # Capture the first *visual row*, including its offset within a logical
        # paragraph. firstVisibleBlock alone cannot distinguish wrapped rows.
        cursor = view.cursorForPosition(QPoint(0, 1))
        return QTextCursor(cursor), view.cursorRect(cursor).top()

    def assert_anchor_retained(self, view, anchor, y):
        actual, actual_y = self.row_at_top(view)
        self.assertEqual(actual.block().text(), anchor.block().text())
        self.assertEqual(actual.positionInBlock(), anchor.positionInBlock())
        self.assertLessEqual(abs(actual_y - y), 1)

    def test_default_wrap_exposes_long_boot_prefix_and_device_message(self):
        view = self.view()
        payload = "l\\x00l\\x9c\\x9f|" * 180 + "Hello from MCU Flasher by Naph!\n"
        self.feed(view, payload)
        self.assertEqual(view.lineWrapMode(), QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.assertGreater(view.document().firstBlock().layout().lineCount(), 1)
        self.assertEqual(view.horizontalScrollBar().maximum(), 0)
        self.assertEqual(view.toPlainText(), payload)
        self.assertEqual(view.get_content_for_clipboard(False), payload)
        # Wrapping must not invent new device line endings in selection copy.
        view.selectAll()
        self.assertEqual(view.textCursor().selectedText().replace("\u2029", "\n"), payload)

    def test_reading_inside_wrapped_paragraph_survives_output_append(self):
        view = self.view()
        payload = " ".join(f"word{number:04d}" for number in range(900))
        self.feed(view, payload, newline=True)
        self.feed(view, "trailing row", newline=True)
        view.set_autoscroll(False)
        view.verticalScrollBar().setValue(28)
        APP.processEvents()
        anchor, y = self.row_at_top(view)
        self.assertGreater(anchor.positionInBlock(), 0)
        self.feed(view, "received while reading", newline=True)
        self.assert_anchor_retained(view, anchor, y)

    def test_selected_unicode_and_visual_row_survive_theme_rebuild(self):
        view = self.view()
        payload = " ".join(f"word{number:04d} 😀" for number in range(550))
        self.feed(view, payload, newline=True)
        self.feed(view, "trailing row", newline=True)
        view.set_autoscroll(False)
        selection = view.document().find("word0400")
        self.assertTrue(selection.hasSelection())
        # QTextDocument.find does not consistently match astral characters on
        # every Qt host; extend its ordinary match using native UTF-16 units.
        selection.setPosition(selection.selectionStart())
        selection.setPosition(selection.position() + len("word0400 😀".encode("utf-16-le")) // 2,
                              QTextCursor.MoveMode.KeepAnchor)
        view.setTextCursor(selection)
        view.verticalScrollBar().setValue(28)
        APP.processEvents()
        anchor, y = self.row_at_top(view)
        old_text, old_column = anchor.block().text(), anchor.positionInBlock()
        view.apply_theme("solarized_dark")
        APP.processEvents()
        self.assertEqual(view.textCursor().selectedText(), "word0400 😀")
        actual, actual_y = self.row_at_top(view)
        self.assertEqual(actual.block().text(), old_text)
        self.assertEqual(actual.positionInBlock(), old_column)
        self.assertLessEqual(abs(actual_y - y), 1)

    def test_manual_reading_anchor_remains_visible_when_window_width_changes(self):
        view = self.view()
        self.feed(view, "0123456789" * 650, newline=True)
        view.set_autoscroll(False)
        view.verticalScrollBar().setValue(35)
        APP.processEvents()
        anchor, _ = self.row_at_top(view)
        for width in (250, 620, 430):
            view.resize(width, 190)
            APP.processEvents()
            # Reflow can put the retained character inside a different visual
            # row. Keep that same character visible at the top reading row.
            self.assertGreaterEqual(view.cursorRect(anchor).top(), -1)
            self.assertLess(view.cursorRect(anchor).top(), view.fontMetrics().lineSpacing())

    def test_timestamp_prefix_reflow_keeps_reading_character_in_top_row(self):
        view = self.view()
        self.feed(view, " ".join(f"word{number:04d}" for number in range(900)), newline=True)
        view.set_autoscroll(False)
        view.verticalScrollBar().setValue(35)
        APP.processEvents()
        anchor, _ = self.row_at_top(view)
        old_position = anchor.position()
        view.set_timestamp_enabled(True)
        APP.processEvents()
        shifted = QTextCursor(view.document())
        shifted.setPosition(old_position + len("[12:34:56] "))
        self.assertGreaterEqual(view.cursorRect(shifted).top(), -1)
        self.assertLess(view.cursorRect(shifted).top(), view.fontMetrics().lineSpacing())
        anchor, _ = self.row_at_top(view)
        old_position = anchor.position()
        view.set_timestamp_enabled(False)
        APP.processEvents()
        shifted = QTextCursor(view.document())
        shifted.setPosition(old_position - len("[12:34:56] "))
        self.assertGreaterEqual(view.cursorRect(shifted).top(), -1)
        self.assertLess(view.cursorRect(shifted).top(), view.fontMetrics().lineSpacing())

    def test_wrapped_layout_settles_at_bottom_and_follows_after_resize(self):
        view = self.view()
        self.feed(view, "0123456789" * 700, newline=True)
        bar = view.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum())
        view.resize(250, 190)
        APP.processEvents()
        self.assertEqual(bar.value(), bar.maximum())
        self.feed(view, "latest output", newline=True)
        self.assertEqual(bar.value(), bar.maximum())

    def test_bottom_follow_and_manual_hold_use_wrapped_rows(self):
        view = self.view()
        self.feed(view, "abcdef0123456789" * 300, newline=True)
        bar = view.verticalScrollBar()
        self.assertGreater(bar.maximum(), 10)
        self.assertEqual(bar.value(), bar.maximum())
        self.feed(view, "next unbroken output " * 70)
        self.assertEqual(bar.value(), bar.maximum())
        bar.setValue(max(0, bar.maximum() - 12))
        APP.processEvents()
        anchor, y = self.row_at_top(view)
        self.feed(view, " output during manual scroll " * 12)
        self.assert_anchor_retained(view, anchor, y)
        bar.setValue(bar.maximum())
        self.feed(view, " resumed at bottom", newline=True)
        self.assertEqual(bar.value(), bar.maximum())

    def test_pending_follow_does_not_override_manual_scroll_or_selection(self):
        view = self.view()
        self.feed(view, "0123456789" * 650, newline=True)
        view.append_log(dict(text="new output", newline=True))
        view._flush_queue()
        view.verticalScrollBar().setValue(20)
        anchor, y = self.row_at_top(view)
        selection = QTextCursor(view.document())
        selection.setPosition(anchor.position())
        selection.setPosition(anchor.position() + 4, QTextCursor.MoveMode.KeepAnchor)
        view.setTextCursor(selection)
        selected = selection.selectedText()
        APP.processEvents()
        self.assert_anchor_retained(view, anchor, y)
        self.assertEqual(view.textCursor().selectedText(), selected)
        self.assertFalse(view._follow._settle_timer.isActive())

    def test_four_and_six_core_bursts_keep_wrap_and_memory_bounds(self):
        for cores in (4, 6):
            with self.subTest(cores=cores):
                view = self.view(cores)
                for number in range(600):
                    view.append_log(dict(text=f"{number:04d}:" + "x" * 2000, newline=False))
                self.assertLessEqual(view._queue.chars, view._queue.max_chars)
                self.assertGreater(view._queue.dropped, 0)
                while view._queue:
                    view._flush_queue()
                APP.processEvents()
                self.assertIn("0599:", view.toPlainText())
                self.assertIn("older entries omitted", view.toPlainText())
                self.assertLessEqual(view.document().lastBlock().length(), 16384)
                self.assertLessEqual(view.document().characterCount(), view._history_limit + 1)
                self.assertLessEqual(view._entries.chars, view._entries.max_chars)
                self.assertEqual(view.horizontalScrollBar().maximum(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
