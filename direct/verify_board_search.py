#!/usr/bin/env python3
"""Hardware-free board picker regressions and optional search benchmark.

Uses an isolated catalog, mocked preferences and no registry/network refresh.
Captures and benchmark reports belong in temp/. No firmware or services start.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Optional
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
from PySide6.QtCore import Qt, QTimer, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget, QListView
from main.qt import board_dialog as module
from main.qt.signals import signals

APP = QApplication.instance() or QApplication([])
RENDER_DIR = None


def fixture(extra=0):
    def board(platform, identifier, mcu):
        return dict(platform=platform, board=identifier, mcu=mcu, framework="arduino",
                    frameworks=["arduino"], pio_resolved=True)
    boards = {
        "Arduino UNO": board("atmelavr", "uno", "atmega328p"),
        "Arduino Nano": board("atmelavr", "nanoatmega328", "atmega328p"),
        "ESP32 Dev Module": board("espressif32", "esp32dev", "esp32"),
        "ESP32S3 Dev Module": board("espressif32", "esp32-s3-devkitc-1", "esp32s3"),
        "Raspberry Pi Pico": board("raspberrypi", "pico", "rp2040"),
        "Definition pending": dict(board="pending", pio_resolved=False),
    }
    for number in range(extra):
        boards[f"Vendor {number:05d} ESP32 Development Kit"] = board("espressif32", f"vendor_{number}", "esp32")
    return boards


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(5)
    assert predicate(), "Timed out waiting for board search"


class BoardPickerChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.boards = fixture()
        self.stack.enter_context(patch.object(module, "SUPPORTED_BOARDS", self.boards))
        self.stack.enter_context(patch.object(module, "load_recent_boards", return_value=["arduino uno", "Removed board"]))
        self.stack.enter_context(patch.object(module, "get_theme_mode", return_value="default"))
        self.recent = self.stack.enter_context(patch.object(module, "add_recent_board"))
        self.parent = QWidget()
        self.parent._backend = Mock(is_busy=False, active_operation=None)
        self.parent._backend._resolve_board_info = lambda name: self.boards.get(name, {})
        self.dialogs = []
        self.addCleanup(self.dispose)

    def dispose(self):
        for dialog in self.dialogs:
            dialog.close()
            if dialog._worker._thread:
                dialog._worker._thread.join(timeout=2)
            dialog.deleteLater()
        self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def picker(self, **kwargs):
        dialog = module.BoardSearchDialog(self.parent, **kwargs)
        self.dialogs.append(dialog)
        dialog.show()
        wait_for(lambda: not dialog._search_pending)
        return dialog

    def test_ranking_aliases_frameworks_categories_and_typo_fallback(self):
        index = module.BoardSearchIndex(self.boards, ["Arduino UNO"])
        for query, expected in (("uno", "Arduino UNO"), ("uno r3", "Arduino UNO"),
                                ("nano 328", "Arduino Nano"), ("ESP32-S3", "ESP32S3 Dev Module"),
                                ("esp32", "ESP32 Dev Module"), ("rp2040", "Raspberry Pi Pico"),
                                ("raspbery", "Raspberry Pi Pico")):
            self.assertEqual(index.search(query)[0], expected)
        self.assertEqual(index.search("esp32", "AVR"), [])
        self.assertEqual(index.search("", "RECENT"), ["Arduino UNO"])
        self.assertEqual(index.search("nothing matches 999999"), [])
        # Normal matching must not run fuzzy edit-distance comparisons.
        with patch.object(module.difflib, "SequenceMatcher", side_effect=AssertionError("Fuzzy normal search")):
            index.search("arduino")
            index.search("esp32 s3")

    def test_subset_recents_initial_selection_and_arrow_navigation(self):
        dialog = self.picker(board_list=["Arduino UNO", "Arduino Nano"], current_board="Arduino Nano")
        self.assertIsInstance(dialog.listbox, QListView)
        self.assertEqual(dialog.recent_boards, ["Arduino UNO"])
        self.assertEqual(dialog.listbox.currentIndex().data(Qt.ItemDataRole.UserRole), "Arduino Nano")
        self.assertEqual(dialog.framework_combo.currentText(), "arduino")
        dialog._select_item_by_name("Arduino UNO")
        QTest.keyClick(dialog.search_ent, Qt.Key.Key_Down)
        self.assertEqual(dialog.listbox.currentIndex().data(Qt.ItemDataRole.UserRole), "Arduino Nano")
        self.parent._backend.refresh_board_catalog.assert_not_called()

    def test_enter_during_search_selects_new_query_once(self):
        selected = Mock()
        dialog = self.picker(on_select_callback=selected)
        dialog.search_ent.setText("pico")
        self.assertFalse(dialog.btn_select.isEnabled())
        QTest.keyClick(dialog.search_ent, Qt.Key.Key_Return)
        wait_for(lambda: dialog._closed)
        selected.assert_called_once_with("Raspberry Pi Pico")
        dialog._confirm_selection()
        selected.assert_called_once()
        self.parent._backend.set_board_framework.assert_called_once_with("Raspberry Pi Pico", "arduino")

    def test_slow_superseded_search_and_catalog_refresh_discard_stale_results(self):
        dialog = self.picker()
        entered, release = threading.Event(), threading.Event()
        original = module.BoardSearchIndex.search
        def slow_search(index, query, category="ALL"):
            if query == "hold":
                entered.set()
                release.wait(2)
            return original(index, query, category)
        with patch.object(module.BoardSearchIndex, "search", slow_search):
            dialog.search_ent.setText("hold")
            dialog._dispatch_search()
            wait_for(entered.is_set)
            dialog.search_ent.setText("uno")
            dialog._dispatch_search()
            release.set()
            wait_for(lambda: not dialog._search_pending)
        self.assertEqual(dialog.listbox.currentIndex().data(Qt.ItemDataRole.UserRole), "Arduino UNO")
        replacement = {"New UNO model": self.boards["Arduino UNO"]}
        dialog._catalog_updated({"boards": replacement})
        wait_for(lambda: not dialog._search_pending)
        self.assertEqual(dialog.all_boards, ["New UNO model"])
        self.assertEqual(dialog.listbox.currentIndex().data(Qt.ItemDataRole.UserRole), "New UNO model")

    def test_closed_dialog_stops_worker_and_ignores_late_catalog_updates(self):
        dialog = self.picker()
        dialog.close()
        dialog._worker._thread.join(timeout=1)
        self.assertFalse(dialog._worker._thread.is_alive())
        generation = dialog._generation
        signals.board_catalog_updated.emit({"boards": self.boards})
        self.assertEqual(dialog._generation, generation)

    def test_reopening_reuses_index_without_rebuilding(self):
        first = self.picker()
        first.close()
        with patch.object(module.BoardSearchIndex, "_build_index", side_effect=AssertionError("Repeated index build")):
            second = self.picker()
        self.assertIs(second._search_index, first._search_index)

    def test_selection_rechecks_busy_and_no_results_do_not_accept(self):
        selected = Mock()
        dialog = self.picker(on_select_callback=selected)
        self.parent._backend.is_busy = True
        dialog._confirm_selection()
        selected.assert_not_called()
        self.parent._backend.is_busy = False
        dialog.search_ent.setText("nothing matches 999999")
        dialog._confirm_selection()
        wait_for(lambda: not dialog._search_pending)
        selected.assert_not_called()
        self.assertFalse(dialog._closed)
        self.assertFalse(dialog.btn_select.isEnabled())

    def test_large_catalog_keeps_event_loop_responsive_and_results_complete(self):
        self.boards.update(fixture(6000))
        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        self.addCleanup(timer.stop)
        timer.start()
        dialog = self.picker()
        dialog.search_ent.setText("ESP32")
        wait_for(lambda: not dialog._search_pending)
        self.assertEqual(len(dialog._list_model.rows), 6002)
        rect = dialog.listbox.visualRect(dialog.listbox.currentIndex())
        self.assertGreater(rect.width(), dialog.listbox.viewport().width() * .9)
        self.assertGreater(rect.height(), 0)
        self.assertGreater(len(ticks), 3)
        self.assertLess(max(b - a for a, b in zip(ticks, ticks[1:])), .25)
        if RENDER_DIR:
            QTest.qWait(100)
            self.assertTrue(dialog.grab().save(str(RENDER_DIR / "board-search-6000.png")))


def benchmark(output):
    """Compare ranking against the pre-change class from this checkout's HEAD."""
    source = subprocess.check_output(["git", "show", "HEAD:main/qt/board_dialog.py"], cwd=ROOT, text=True, encoding="utf-8")
    tree = ast.parse(source)
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and
             node.name in ("_normalize_text", "_extract_tokens", "BoardSearchIndex") or
             isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_FLAGSHIP_MAPPINGS"]
    namespace = dict(re=re, difflib=difflib, Optional=Optional)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "baseline-board-index", "exec"), namespace)
    boards = fixture(3000)
    report = {"board_count": len(boards), "queries": ["esp32", "raspbery", "noresult123456789"]}
    for label, cls in (("before", namespace["BoardSearchIndex"]), ("after", module.BoardSearchIndex)):
        index = cls(boards)
        times = []
        for query in report["queries"]:
            started = time.perf_counter()
            index.search(query)
            times.append(round((time.perf_counter() - started) * 1000, 2))
        report[label + "_ms"] = times
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--benchmark", type=Path)
    args, remaining = parser.parse_known_args()
    RENDER_DIR = args.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    if args.benchmark:
        args.benchmark.parent.mkdir(parents=True, exist_ok=True)
        benchmark(args.benchmark)
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
