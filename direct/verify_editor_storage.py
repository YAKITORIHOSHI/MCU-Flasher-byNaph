#!/usr/bin/env python3
"""Verify bounded editor source workers without project files or live settings."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from main.qt.editor_panel import EditorBridgeAPI

APP = QApplication.instance() or QApplication([])


def until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        time.sleep(.002)
    raise AssertionError("The isolated source worker did not finish")


class Backend:
    sketch_dir_path = Path("/fixture/sketch")

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def get_project_files(self):
        self.calls.append(("list", threading.get_ident()))
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("Fixture read was not released")
        return [{"name": "source.cpp", "path": str(self.sketch_dir_path / "source.cpp")}]

    def read_file(self, path):
        self.calls.append((path, threading.get_ident()))
        return {"success": True, "content": "// source held only in memory"}

    def get_cloud_project_files(self):
        return {"success": True, "files": self.get_project_files()}


class EditorStorageChecks(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.bridge = EditorBridgeAPI(self.backend)
        self.token = self.bridge.begin_renderer_load()
        self.results = []
        self.bridge.editor_storage_ready.connect(lambda *args: self.results.append((*args, threading.get_ident())))
        self.addCleanup(self.backend.release.set)

    def request(self, request, operation="get_project_files", token=None):
        return self.bridge.request_editor_storage(token or self.token, request, operation, "/fixture/sketch/source.cpp")

    def test_source_io_keeps_qt_responsive_and_delivers_on_gui(self):
        gui = threading.get_ident()
        self.assertTrue(self.request("first"))
        until(self.backend.entered.is_set)
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        until(lambda: bool(heartbeat))
        self.assertFalse(self.results, "Qt must keep dispatching while cold IO is blocked")
        self.backend.release.set()
        until(lambda: len(self.results) == 1)
        self.assertNotEqual(self.backend.calls[0][1], gui)
        self.assertEqual(self.results[0][-1], gui)
        self.assertTrue(self.results[0][2]["success"])
        self.assertFalse(self.bridge._editor_storage_running)
        self.assertTrue(self.request("read", "read_file"))
        until(lambda: len(self.results) == 2)
        self.assertTrue(self.results[1][2]["value"]["success"])
        self.assertNotEqual(self.backend.calls[1][1], gui)

    def test_renderer_replacement_has_one_pending_slot_and_refuses_old_result(self):
        self.assertTrue(self.request("old"))
        until(self.backend.entered.is_set)
        replacement = self.bridge.begin_renderer_load()
        self.assertTrue(self.request("replacement", token=replacement))
        self.assertFalse(self.request("excess", token=replacement))
        self.assertFalse(self.request("stale", token=self.token))
        self.backend.release.set()
        until(lambda: len(self.results) == 2)
        replies = {row[1]: row[2] for row in self.results}
        self.assertFalse(replies["old"]["success"])
        self.assertTrue(replies["replacement"]["success"])
        self.assertEqual(len(self.backend.calls), 2)
        self.assertFalse(self.bridge._editor_storage_running)

    def test_project_switch_refuses_old_io_and_skips_stale_pending_read(self):
        self.assertTrue(self.request("active"))
        until(self.backend.entered.is_set)
        self.assertTrue(self.request("queued", "read_file"))
        self.backend.sketch_dir_path = Path("/fixture/other")
        self.backend.release.set()
        until(lambda: len(self.results) == 2)
        self.assertTrue(all(not row[2]["success"] for row in self.results))
        self.assertEqual(len(self.backend.calls), 1, "A stale queued request must not touch storage")
        self.assertFalse(self.bridge._editor_storage_running)
        self.assertTrue(self.request("retry", "read_file"))
        until(lambda: len(self.results) == 3)
        self.assertTrue(self.results[-1][2]["success"])

    def test_failed_enumeration_refuses_but_a_valid_empty_sketch_completes(self):
        self.backend.get_cloud_project_files = lambda: {"success": False, "files": []}
        self.assertTrue(self.request("inaccessible"))
        until(lambda: len(self.results) == 1)
        self.assertFalse(self.results[-1][2]["success"])
        self.backend.get_cloud_project_files = lambda: {"success": True, "files": []}
        self.assertTrue(self.request("empty"))
        until(lambda: len(self.results) == 2)
        self.assertEqual(self.results[-1][2], {"success": True, "value": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
