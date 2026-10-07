#!/usr/bin/env python3
"""Isolated USB/HDD data-flow regressions; no hardware or live metadata writes.

Slow reads/writes are simulated in root-source fixtures under temp/. Backend
methods are extracted without importing its runtime hooks or package stores.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import shiboken6
from main.core import ai_review

APP = QApplication.instance() or QApplication([])


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        QTest.qWait(5)
    raise AssertionError("Background fixture did not finish")


class FakeFileWatcher:
    def __init__(self, *_):
        self.directoryChanged = self.fileChanged = SimpleNamespace(connect=lambda *_: None)
        self.paths = set()

    def directories(self):
        return [path for path in self.paths if Path(path).is_dir()]

    def files(self):
        return [path for path in self.paths if Path(path).is_file()]

    def addPath(self, path):
        self.paths.add(path)

    def addPaths(self, paths):
        self.paths.update(paths)

    def removePaths(self, paths):
        self.paths.difference_update(paths)


class StorageFlowChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/slow-storage-flow"
        audit.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "sample.ino"
        self.source.write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
        self.watcher_patch = patch.object(ai_review, "QFileSystemWatcher", FakeFileWatcher)
        self.watcher_patch.start()
        self.addCleanup(self.watcher_patch.stop)
        self.gui_thread = threading.get_ident()

    def watcher(self):
        manager = SimpleNamespace(project_dir=self.root, queue_ai_edit_snapshot=Mock(return_value=True))
        watcher = ai_review.AIEditWatcher(self.root, manager)
        watcher._timer.stop()
        self.addCleanup(watcher.shutdown)
        self.addCleanup(watcher.deleteLater)
        return watcher, manager

    def baseline(self, watcher):
        wait_until(lambda: not watcher._scan_running)
        watcher._timer.stop()
        self.assertTrue(watcher._baseline_ready)

    def settle(self, watcher):
        watcher._poll_step()
        wait_until(lambda: not watcher._scan_running)
        watcher._timer.stop()
        with watcher._lock:
            for value in watcher._pending_settle.values():
                value["last_change_time"] -= 1
        watcher._poll_step()
        wait_until(lambda: not watcher._scan_running)
        watcher._timer.stop()

    def backend(self):
        tree = ast.parse((ROOT / "main/web_bridge.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "MCUWebBackendAPI")
        names = {"_queue_hardware_state_write", "_sync_project_hardware_state",
                 "_extract_port_device", "_hash_sources", "_stage_root_source_file",
                 "save_file", "_get_jobs", "_detect_sketch_baud_rate"}
        cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
        ram = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_SketchRAMCache")
        self.ns = dict(Path=Path, os=os, sys=sys, time=time, json=json, hashlib=hashlib,
                       datetime=datetime, shutil=shutil, threading=threading, re=re,
                       ensure_file_writable=Mock(), ensure_hidden_read_first_md=Mock(),
                       hide_internal_project_metadata=Mock(), SCRIPT_DIR=self.root,
                       VALID_BAUD_RATES={115200, 230400}, MAX_BAUD_RATE=921600,
                       write_generated_text=Mock(),
                       get_project_build_cache_root=lambda project: Path(project) / "fixture-cache",
                       get_sketch_files_fast=lambda project: sorted(Path(project).glob("*.ino")),
                       retry_transient_file_operation=lambda operation, **_: operation(),
                       load_gui_config=lambda: {}, get_optimal_compiler_jobs=Mock(return_value=2))
        exec(compile(ast.Module(body=[ram, cls], type_ignores=[]), "isolated_storage_backend", "exec"), self.ns)
        api = self.ns["MCUWebBackendAPI"]()
        self.ns["_sketch_ram_cache"] = self.ns["_SketchRAMCache"]()
        api.sketch_dir_path = self.root
        api.current_port, api.current_board = "", ""
        api.current_baud = 115200
        api._resolve_board_info = lambda *_: {"platform": "fixture", "board": "exact", "framework": "arduino"}
        api.emit = Mock()
        api._hardware_state_lock = threading.Lock()
        api._hardware_state_pending = None
        api._hardware_state_running = False
        api._hardware_state_active_payload = None
        api._last_synced_hardware_payload = None
        api.modified_files = {}
        api.update_skip_compile_availability = Mock()
        api.ai_watcher = SimpleNamespace(note_user_save=Mock())
        return api

    def test_initial_slow_source_read_is_not_on_gui_thread(self):
        entered, release = threading.Event(), threading.Event()
        original = ai_review.AIReviewManager._read_text_exact
        threads = []
        def slow_read(path):
            threads.append(threading.get_ident())
            entered.set()
            release.wait(2)
            return original(path)
        with patch.object(ai_review.AIReviewManager, "_read_text_exact", side_effect=slow_read):
            began = time.monotonic()
            watcher, _ = self.watcher()
            self.assertLess(time.monotonic() - began, 0.15)
            self.assertTrue(entered.wait(1))
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            wait_until(lambda: bool(ticks))
            self.assertTrue(watcher._scan_running)
            release.set()
            self.baseline(watcher)
        self.assertTrue(all(thread != self.gui_thread for thread in threads))

    def test_scan_burst_has_one_worker_and_one_latest_request(self):
        watcher, _ = self.watcher()
        self.baseline(watcher)
        entered, release = threading.Event(), threading.Event()
        original = watcher._scan_project
        count = []
        def blocked(request):
            count.append(request[0])
            entered.set()
            release.wait(2)
            return original(request)
        with patch.object(watcher, "_scan_project", side_effect=blocked):
            watcher._poll_step()
            self.assertTrue(entered.wait(1))
            for _ in range(100):
                watcher._poll_step()
            self.assertEqual(len(count), 1)
            release.set()
            wait_until(lambda: len(count) == 2 and not watcher._scan_running)
        self.assertEqual(len(count), 2)

    def test_unchanged_fallback_does_not_read_source(self):
        watcher, manager = self.watcher()
        self.baseline(watcher)
        with patch.object(ai_review.AIReviewManager, "_read_text_exact", side_effect=AssertionError("idle source read")):
            for _ in range(5):
                watcher._poll_step()
                wait_until(lambda: not watcher._scan_running)
        manager.queue_ai_edit_snapshot.assert_not_called()
        self.assertEqual(watcher._timer.interval(), 5000)

    def test_coarse_timestamp_event_reads_and_publishes_off_gui(self):
        watcher, manager = self.watcher()
        self.baseline(watcher)
        initial = self.source.stat()
        self.source.write_text("void setup() {}\nvoid loop() {;}\n", encoding="utf-8")
        os.utime(self.source, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        watcher._on_fs_changed(str(self.source))
        watcher._wake_timer.stop()
        threads = []
        manager.queue_ai_edit_snapshot.side_effect = lambda *_, **__: (threads.append(threading.get_ident()) or True)
        self.settle(watcher)
        self.assertEqual(manager.queue_ai_edit_snapshot.call_count, 1)
        self.assertTrue(all(thread != self.gui_thread for thread in threads))
        self.assertEqual(manager.queue_ai_edit_snapshot.call_args.kwargs["expected_project"], self.root)

    def test_manual_save_wins_over_slow_inflight_baseline(self):
        entered, release = threading.Event(), threading.Event()
        original = ai_review.AIReviewManager._read_text_exact
        def blocked(path):
            old = original(path)
            entered.set()
            release.wait(2)
            return old
        with patch.object(ai_review.AIReviewManager, "_read_text_exact", side_effect=blocked):
            watcher, manager = self.watcher()
            self.assertTrue(entered.wait(1))
            saved = "manual user save"
            self.source.write_text(saved, encoding="utf-8")
            watcher.note_user_save(self.source, saved)
            release.set()
            self.baseline(watcher)
        self.settle(watcher)
        self.assertIn(saved, watcher._baseline_contents.values())
        manager.queue_ai_edit_snapshot.assert_not_called()

    def test_old_project_scan_is_discarded_after_switch(self):
        watcher, manager = self.watcher()
        self.baseline(watcher)
        entered, release = threading.Event(), threading.Event()
        original = watcher._scan_project
        first = True
        def blocked(request):
            nonlocal first
            if first:
                first = False
                entered.set()
                release.wait(2)
            return original(request)
        next_project = self.root / "next-project"
        next_project.mkdir()
        (next_project / "new.ino").write_text("new project", encoding="utf-8")
        with patch.object(watcher, "_scan_project", side_effect=blocked):
            watcher._poll_step()
            self.assertTrue(entered.wait(1))
            manager.project_dir = next_project
            watcher.bind_project(next_project)
            watcher._timer.stop()
            release.set()
            wait_until(lambda: watcher._baseline_ready and not watcher._scan_running)
        self.assertEqual(list(watcher._baseline_contents.values()), ["new project"])
        manager.queue_ai_edit_snapshot.assert_not_called()

    def test_worker_start_failure_releases_reservation(self):
        watcher, _ = self.watcher()
        self.baseline(watcher)
        with patch.object(ai_review.threading, "Thread", side_effect=RuntimeError("fixture worker failure")):
            watcher._poll_step()
        self.assertFalse(watcher._scan_running)
        watcher._poll_step()
        wait_until(lambda: not watcher._scan_running)

    def assert_deleted_watcher_completion_is_quiet(self, *, closed):
        # Real QObject destruction, not a mocked signal: a slow filesystem
        # worker may outlive the window on both native Ubuntu and Windows.
        manager = SimpleNamespace(project_dir=self.root, queue_ai_edit_snapshot=Mock())
        watcher = ai_review.AIEditWatcher(None, manager)
        watcher.project_dir = self.root
        entered, release = threading.Event(), threading.Event()
        workers, errors = [], []
        original_thread = threading.Thread

        def thread(*args, **kwargs):
            worker = original_thread(*args, **kwargs)
            workers.append(worker)
            return worker

        def blocked(request):
            entered.set()
            release.wait(2)
            return {"generation": request[0], "events": [], "files": []}

        with patch.object(watcher, "_scan_project", side_effect=blocked), \
                patch.object(threading, "Thread", side_effect=thread), \
                patch.object(threading, "excepthook", side_effect=lambda args: errors.append(args.exc_value)):
            try:
                watcher._poll_step()
                self.assertTrue(entered.wait(1))
                if closed:
                    watcher.shutdown()
                shiboken6.delete(watcher)
                release.set()
                workers[0].join(2)
                self.assertFalse(workers[0].is_alive())
                self.assertEqual(errors, [])
            finally:
                release.set()
                for worker in workers:
                    worker.join(2)
                if shiboken6.isValid(watcher):
                    watcher.shutdown()
                    shiboken6.delete(watcher)

    def test_shutdown_during_slow_scan_skips_deleted_signal_source(self):
        self.assert_deleted_watcher_completion_is_quiet(closed=True)

    def test_qobject_deleted_before_completion_does_not_raise_worker_error(self):
        self.assert_deleted_watcher_completion_is_quiet(closed=False)

    def test_hardware_state_writer_coalesces_and_finishes_latest(self):
        api = self.backend()
        entered, release = threading.Event(), threading.Event()
        writes, threads = [], []
        def slow_write(path, text):
            threads.append(threading.get_ident())
            if not writes:
                entered.set()
                release.wait(2)
            writes.append((path, text))
        self.ns["write_generated_text"] = slow_write
        api._queue_hardware_state_write(self.root, ("first",), "first")
        self.assertTrue(entered.wait(1))
        for index in range(100):
            api._queue_hardware_state_write(self.root, (index,), str(index))
        release.set()
        wait_until(lambda: not api._hardware_state_running)
        self.assertEqual([text for _, text in writes], ["first", "99"])
        self.assertTrue(all(thread != self.gui_thread for thread in threads))
        api._queue_hardware_state_write(self.root, (99,), "99")
        self.assertEqual(len(writes), 2)

    def test_hardware_state_failure_is_reported_and_next_update_can_retry(self):
        api = self.backend()
        write = Mock(side_effect=OSError("removed USB fixture"))
        self.ns["write_generated_text"] = write
        api._queue_hardware_state_write(self.root, ("state",), "state")
        wait_until(lambda: not api._hardware_state_running)
        self.assertIsNone(api._last_synced_hardware_payload)
        self.assertTrue(api.emit.called)
        write.side_effect = None
        api._queue_hardware_state_write(self.root, ("state",), "state")
        wait_until(lambda: not api._hardware_state_running)
        self.assertEqual(write.call_count, 2)
        self.assertEqual(api._last_synced_hardware_payload, ("state",))

    def test_hardware_state_latest_project_wins_without_overlapping_writers(self):
        api = self.backend()
        entered, release = threading.Event(), threading.Event()
        other = self.root / "other-project"
        other.mkdir()
        writes = []
        def slow_write(path, text):
            if not writes:
                entered.set()
                release.wait(2)
            writes.append((path, text))
        self.ns["write_generated_text"] = slow_write
        api._queue_hardware_state_write(self.root, ("old-active",), "old-active")
        self.assertTrue(entered.wait(1))
        api._queue_hardware_state_write(self.root, ("old-pending",), "old-pending")
        api._queue_hardware_state_write(other, ("new-project",), "new-project")
        release.set()
        wait_until(lambda: not api._hardware_state_running)
        self.assertEqual([text for _, text in writes], ["old-active", "new-project"])
        self.assertEqual(writes[-1][0], other / "fixture-cache/project_state.json")

    def test_hardware_state_start_failure_can_retry_without_false_saved_marker(self):
        api = self.backend()
        with patch.object(threading, "Thread", side_effect=RuntimeError("fixture startup failure")):
            api._queue_hardware_state_write(self.root, ("state",), "state")
        self.assertFalse(api._hardware_state_running)
        self.assertIsNone(api._last_synced_hardware_payload)
        api._queue_hardware_state_write(self.root, ("state",), "state")
        wait_until(lambda: not api._hardware_state_running)
        self.ns["write_generated_text"].assert_called_once()

    def test_review_manager_revalidates_project_and_manual_save_before_journal(self):
        manager = ai_review.AIReviewManager.__new__(ai_review.AIReviewManager)
        manager.project_dir = self.root
        manager._pending_ai_lock = threading.RLock()
        manager._pending_ai_edits = {}
        manager._ai_review_revision = 0
        manager._ai_decision_redo = []
        manager._ai_backup_store = None
        manager._commit_pending_ai_edits_locked = Mock()
        allowed = iter((True, False))
        result = manager.queue_ai_edit_snapshot(self.source, "before", "after",
                                                expected_project=self.root,
                                                expected_valid=lambda: next(allowed))
        self.assertFalse(result)
        self.assertEqual(manager._pending_ai_edits, {})
        manager._commit_pending_ai_edits_locked.assert_not_called()
        result = manager.queue_ai_edit_snapshot(self.source, "before", "after",
                                                expected_project=self.root / "old-project")
        self.assertFalse(result)
        manager._commit_pending_ai_edits_locked.assert_not_called()

    def test_queued_review_signal_is_discarded_after_manual_save(self):
        watcher, _ = self.watcher()
        self.baseline(watcher)
        key = ai_review.AIReviewManager._path_key(self.source)
        events = []
        watcher.ai_edit_detected.connect(lambda *args: events.append(args))
        self.source.write_text("manual", encoding="utf-8")
        watcher.note_user_save(self.source, "manual")
        watcher._finish_scan({"generation": watcher._generation, "save_versions": {key: 0},
                              "events": [(key, str(self.source), "old", "stale", True, True)],
                              "files": [str(self.source)]})
        self.assertEqual(events, [])

    def test_build_fingerprint_ignores_coarse_timestamp_cache(self):
        api = self.backend()
        ram = self.ns["_sketch_ram_cache"]
        first = api._hash_sources()
        original = self.source.stat()
        old_content = ram.get_content(self.source)
        changed = old_content.replace("setup", "other")
        self.source.write_text(changed, encoding="utf-8")
        os.utime(self.source, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(ram.get_content(self.source), old_content)
        self.assertNotEqual(api._hash_sources(), first)

    def test_staging_compares_bytes_and_leaves_identical_file_untouched(self):
        api = self.backend()
        staged = self.root / "staged.cpp"
        staged.write_bytes(self.source.read_bytes())
        before = staged.stat().st_mtime_ns
        self.assertFalse(api._stage_root_source_file(self.source, staged))
        self.assertEqual(staged.stat().st_mtime_ns, before)
        stat = self.source.stat()
        self.source.write_bytes(self.source.read_bytes().replace(b"setup", b"other"))
        os.utime(self.source, ns=(stat.st_atime_ns, before))
        self.assertTrue(api._stage_root_source_file(self.source, staged))
        self.assertEqual(staged.read_bytes(), self.source.read_bytes())

    def test_changed_staging_escapes_two_second_clock_collision(self):
        api = self.backend()
        staged = self.root / "staged.cpp"
        staged.write_bytes(b"old source")
        before = staged.stat()
        self.source.write_bytes(b"new source")
        copy = shutil.copyfile
        def coarse_copy(source, destination):
            copy(source, destination)
            os.utime(destination, ns=(before.st_atime_ns, before.st_mtime_ns))
        with patch.object(shutil, "copyfile", side_effect=coarse_copy):
            self.assertTrue(api._stage_root_source_file(self.source, staged))
        self.assertGreaterEqual(staged.stat().st_mtime_ns, before.st_mtime_ns + 2_000_000_000)
        self.assertEqual(staged.read_bytes(), b"new source")

    def test_baud_detection_cache_miss_uses_fresh_content(self):
        api = self.backend()
        self.source.write_text("void setup(){ Serial.begin(115200); }", encoding="utf-8")
        self.assertEqual(api._detect_sketch_baud_rate(), "115200")
        original = self.source.stat()
        self.source.write_text("void setup(){ Serial.begin(230400); }", encoding="utf-8")
        os.utime(self.source, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(api._detect_sketch_baud_rate(), "230400")

    def test_identical_explicit_save_skips_disk_write_but_acknowledges(self):
        api = self.backend()
        before = self.source.stat().st_mtime_ns
        with self.source.open("r", encoding="utf-8", newline="") as stream:
            exact = stream.read()
        result = api.save_file(str(self.source), exact)
        self.assertTrue(result["success"])
        self.assertEqual(self.source.stat().st_mtime_ns, before)
        self.ns["ensure_file_writable"].assert_not_called()
        api.ai_watcher.note_user_save.assert_called_once()

    def test_compiler_budget_includes_app_and_source_drives(self):
        api = self.backend()
        self.assertEqual(api._get_jobs(), 2)
        call = self.ns["get_optimal_compiler_jobs"].call_args
        self.assertIn(self.root, call.kwargs["storage_paths"])
        self.assertTrue(call.kwargs["storage_wait"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
