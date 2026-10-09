#!/usr/bin/env python3
"""Isolated syntax row navigation and unsaved-buffer revision checks."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from main.qt.signals import signals
from main.qt.syntax_panel import SyntaxPanel
from src import syntax_checker

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class SyntaxNavigationChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp" / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(prefix="syntax-navigation-", dir=scratch)
        self.addCleanup(self.fixture.cleanup)
        self.project = Path(self.fixture.name) / "Sketch with spaces"
        self.project.mkdir()
        self.source = self.project / "Sketch with spaces.ino"
        self.source.write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
        self.buffers = {}
        self.revision = [0]
        self.config = patch("main.core.config._load_raw_config", return_value={"shared": {}, "instances": {}})
        self.config.start()
        self.addCleanup(self.config.stop)
        self.persistence = patch("main.core.config._save_raw_config", side_effect=AssertionError("Live persistence forbidden"))
        self.persistence.start()
        self.addCleanup(self.persistence.stop)
        self.backend = SimpleNamespace(get_project_dir=lambda: self.project, is_busy=False)
        self.panel = SyntaxPanel(self.backend)
        self.panel._bg_timer.stop()
        self.panel.set_buffer_provider(lambda: dict(self.buffers), lambda: self.revision[0])
        self.addCleanup(self.panel.deleteLater)
        self.locations = []
        self.receive_location = lambda value: self.locations.append(value)
        signals.editor_goto_diagnostic.connect(self.receive_location)
        self.addCleanup(signals.editor_goto_diagnostic.disconnect, self.receive_location)
        self.deliveries = []
        self.receive_diagnostics = lambda value: self.deliveries.append(value)
        signals.syntax_errors.connect(self.receive_diagnostics)
        self.addCleanup(signals.syntax_errors.disconnect, self.receive_diagnostics)
        syntax_checker.clear_syntax_cache()
        self.addCleanup(syntax_checker.clear_syntax_cache)

    def flush(self):
        for _ in range(4):
            APP.processEvents()

    def queued_workers(self):
        jobs = []
        # Replace only the panel's thread factory; the shared parser executor
        # must keep its real thread implementation for disk source checks.
        worker = patch("main.qt.syntax_panel.threading", SimpleNamespace(
            Thread=lambda target, **kwargs: SimpleNamespace(start=lambda: jobs.append(target))))
        worker.start()
        self.addCleanup(worker.stop)
        return jobs

    def finish_job(self, jobs, index):
        jobs[index]()
        self.flush()

    def fire_retry(self):
        self.panel._retry_timer.stop()
        self.panel._retry_latest_analysis()

    def test_filtered_row_keeps_exact_file_and_multiline_range(self):
        other = self.project / "driver.hpp"
        other.write_text("#pragma once\n", encoding="utf-8")
        self.panel.set_diagnostics([
            {"file": self.source.name, "line": 3, "col": 4, "severity": "error", "message": "First failure"},
            {"file": other.name, "line": 9, "col": 6, "endLine": 11, "endCol": 3,
             "severity": "warning", "message": "Retain full range"},
        ])
        self.panel._filter_combo.setCurrentIndex(2)
        self.assertEqual(self.panel._table.rowCount(), 1)
        self.panel._on_row_double_clicked(self.panel._table.item(0, 3))
        self.assertEqual(self.locations, [{"file": str(other), "line": 9, "col": 6,
            "endLine": 11, "endCol": 3, "columnEncoding": "codepoint"}])

    def test_absolute_source_and_malformed_positions(self):
        self.panel.set_diagnostics([{"file": str(self.source), "line": "invalid", "col": -8,
            "endLine": None, "endCol": "invalid", "severity": "warning", "message": "Position fallback"}])
        self.panel._on_row_double_clicked(self.panel._table.item(0, 1))
        self.assertEqual(self.locations[0], {"file": str(self.source), "line": 1, "col": 1,
            "endLine": 1, "endCol": 1, "columnEncoding": "codepoint"})

    def test_real_double_click_on_warning_description_activates_its_source(self):
        self.panel.set_diagnostics([{"file": self.source.name, "line": 2, "col": 7,
            "endCol": 9, "severity": "warning", "message": "Double-click warning"}])
        self.panel.resize(1000, 260)
        self.panel.show()
        self.flush()
        point = self.panel._table.visualItemRect(self.panel._table.item(0, 3)).center()
        viewport = self.panel._table.viewport()
        QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseDClick(viewport, Qt.MouseButton.LeftButton, pos=point)
        self.flush()
        self.panel.hide()
        self.assertEqual(len(self.locations), 1)
        self.assertEqual(self.locations[0]["file"], str(self.source))
        self.assertEqual(self.locations[0]["col"], 7)

    def test_nested_and_external_sources_never_guess_same_basename(self):
        for source in ("nested/Sketch with spaces.ino", "../Sketch with spaces.ino",
                       str(self.project.parent / self.source.name), "readme.txt", ""):
            with self.subTest(source=source):
                self.panel.set_diagnostics([{"file": source, "line": 9, "severity": "error"}])
                self.panel._on_row_double_clicked(self.panel._table.item(0, 0))
        self.assertEqual(self.locations, [])
        self.assertIn("outside this sketch", self.panel._lbl_status.text())

    def test_dirty_buffer_check_uses_unsaved_line_and_keeps_disk(self):
        original = self.source.read_bytes()
        self.buffers[str(self.source)] = "\n\nvoid setup() {\n  delay(10)\n}\nvoid loop() {}\n"
        jobs = self.queued_workers()
        self.panel._run_manual_check()
        self.finish_job(jobs, 0)
        warnings = [diag for diag in self.panel._all_diagnostics if diag["severity"] == "warning"]
        self.assertEqual([diag["line"] for diag in warnings], [4])
        self.assertEqual(self.source.read_bytes(), original)
        self.panel._on_row_double_clicked(self.panel._table.item(0, 3))
        self.assertEqual(self.locations[0]["line"], 4)

    def test_unsaved_revision_changes_are_checked_without_disk_change(self):
        jobs = self.queued_workers()
        self.buffers[str(self.source)] = "void setup() {}\nvoid loop() {}\n"
        self.panel._execute_analysis(None)
        self.finish_job(jobs, 0)
        self.assertFalse(self.panel._all_diagnostics)
        with patch.object(syntax_checker, "analyze_cpp_syntax", wraps=syntax_checker.analyze_cpp_syntax) as parse:
            self.panel._execute_analysis(None)
            self.finish_job(jobs, 1)
            parse.assert_not_called()
            self.buffers[str(self.source)] = "void setup() {\n  delay(10)\n}\nvoid loop() {}\n"
            self.panel._execute_analysis(None)
            self.finish_job(jobs, 2)
            parse.assert_called_once()
        self.assertEqual(self.panel._all_diagnostics[0]["line"], 2)

    def test_pending_old_revision_is_discarded_and_latest_check_retried(self):
        jobs = self.queued_workers()
        self.buffers[str(self.source)] = "void setup() {\n delay(1)\n}\n"
        self.panel._run_manual_check()
        self.buffers[str(self.source)] = "\n\nvoid setup() {\n delay(2)\n}\n"
        self.finish_job(jobs, 0)
        self.assertEqual(self.deliveries, [], "The obsolete result reached the editor")
        self.assertFalse(self.panel._is_checking)
        self.assertTrue(self.panel._retry_timer.isActive())
        self.assertEqual(len(jobs), 1)
        self.fire_retry()
        self.assertEqual(len(jobs), 2)
        self.finish_job(jobs, 1)
        self.assertFalse(self.panel._is_checking)
        self.assertEqual([diag["line"] for diag in self.deliveries[0]], [4])

    def test_edit_then_save_discards_pending_disk_result_with_empty_snapshots(self):
        self.source.write_text("void setup() {\n delay(1)\n}\n", encoding="utf-8")
        jobs = self.queued_workers()
        self.panel._run_manual_check()
        jobs[0]()  # Old disk diagnostics await queued GUI delivery.
        self.source.write_text("\n\nvoid setup() {\n delay(2)\n}\n", encoding="utf-8")
        self.revision[0] += 1  # An edit crossed the bridge before Save cleared it.
        self.assertEqual(self.buffers, {})
        self.flush()
        self.assertEqual(self.deliveries, [], "Old disk diagnostics survived edit-then-save")
        self.assertTrue(self.panel._retry_timer.isActive())
        self.fire_retry()
        self.assertEqual(len(jobs), 2)
        self.finish_job(jobs, 1)
        self.assertEqual([diag["line"] for diag in self.deliveries[0]], [4])

    def test_worker_start_failure_with_dirty_snapshot_releases_reservation(self):
        self.buffers[str(self.source)] = "void setup() {\n delay(1)\n}\n"
        with patch("main.qt.syntax_panel.threading", SimpleNamespace(
                Thread=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("No fixture worker slot")))):
            self.panel._run_manual_check()
        self.assertFalse(self.panel._is_checking)
        self.assertIn("failed", self.panel._lbl_status.text())
        self.assertIn("No fixture worker slot", self.panel._lbl_status.toolTip())
        jobs = self.queued_workers()
        self.panel._run_manual_check()
        self.finish_job(jobs, 0)
        self.assertEqual(self.panel._all_diagnostics[0]["line"], 2)

    def test_dirty_buffer_survives_missing_disk_file_without_save(self):
        self.source.unlink()
        self.buffers[str(self.source)] = "void setup() {\n delay(1)\n}\n"
        jobs = self.queued_workers()
        self.panel._run_manual_check()
        self.finish_job(jobs, 0)
        self.assertEqual(self.panel._all_diagnostics[0]["line"], 2)
        self.assertFalse(self.source.exists())

    def test_buffer_provider_failure_does_not_leave_analysis_busy(self):
        self.panel.set_buffer_provider(lambda: (_ for _ in ()).throw(RuntimeError("Snapshot unavailable")))
        jobs = self.queued_workers()
        self.panel._run_manual_check()
        self.assertEqual(jobs, [])
        self.assertFalse(self.panel._is_checking)
        self.assertIn("Snapshot unavailable", self.panel._lbl_status.toolTip())

    def test_bridge_edit_and_save_cleanup_retains_monotonic_revision(self):
        from main.qt.editor_panel import EditorBridgeAPI
        backend = SimpleNamespace(sketch_dir_path=self.project, mark_modified=Mock())
        bridge = EditorBridgeAPI(backend)
        self.addCleanup(bridge.deleteLater)
        before = bridge._syntax_generation
        bridge.snapshot_buffer(str(self.source), "\nvoid setup() {}\n")
        self.assertGreater(bridge._syntax_generation, before)
        bridge.mark_modified(str(self.source), False)
        self.assertEqual(bridge._buffer_snapshots, {})
        self.assertGreater(bridge._syntax_generation, before)

    def test_navigation_reveals_hidden_pane_without_reattaching_detached_editor(self):
        from main.qt.main_window import MCUMainWindow
        for detached, visible, expected_sync in ((False, False, 1), (False, True, 0), (True, False, 0)):
            with self.subTest(detached=detached, visible=visible):
                host = SimpleNamespace(_editor_detached=detached, _editor_pane_visible=visible,
                                       _sync_ai_and_editor_layout=Mock())
                MCUMainWindow.reveal_editor_for_navigation(host)
                self.assertEqual(host._sync_ai_and_editor_layout.call_count, expected_sync)
                self.assertEqual(host._editor_detached, detached)
                self.assertEqual(host._editor_pane_visible, visible if detached else True)

    def test_navigation_restores_minimized_host_and_focus_before_source_request(self):
        from main.qt.editor_panel import MonacoEditorPanel
        host = SimpleNamespace(isMinimized=lambda: True, showNormal=Mock(),
            reveal_editor_for_navigation=Mock(), raise_=Mock(), activateWindow=Mock())
        page = SimpleNamespace(runJavaScript=Mock())
        view = SimpleNamespace(setFocus=Mock(), page=lambda: page)
        panel = SimpleNamespace(window=lambda: host, _view=view)
        MonacoEditorPanel._navigate_source(panel, {"path": str(self.source), "line": 2, "reveal": "top"})
        host.showNormal.assert_called_once()
        host.reveal_editor_for_navigation.assert_called_once()
        host.raise_.assert_called_once()
        host.activateWindow.assert_called_once()
        view.setFocus.assert_called_once_with(Qt.FocusReason.OtherFocusReason)
        page.runJavaScript.assert_called_once()
        self.assertIn('"reveal": "top"', page.runJavaScript.call_args.args[0])

    def test_root_check_omits_nested_files_and_external_dirty_snapshots(self):
        nested = self.project / "nested"
        nested.mkdir()
        (nested / "broken.cpp").write_text("void function() {", encoding="utf-8")
        self.buffers[str(nested / "broken.cpp")] = "void function() {"
        self.buffers[str(self.project.parent / "external.ino")] = "void function() {"
        self.buffers[str(self.project / "readme.txt")] = "A note with {"
        jobs = self.queued_workers()
        self.panel._run_manual_check()
        self.finish_job(jobs, 0)
        self.assertFalse(self.panel._all_diagnostics)
        self.assertEqual(set(self.panel._last_mtimes), {str(self.source)})

    def test_symlink_source_never_escapes_root_through_navigation_or_dirty_buffer(self):
        external = self.project.parent / "external.cpp"
        external.write_text("void external() {\n", encoding="utf-8")
        link = self.project / "linked.cpp"
        try:
            link.symlink_to(external)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"This host cannot create isolated symlinks: {exc}")
        self.buffers[str(link)] = "void external() {\n"
        self.panel.set_diagnostics([{"file": link.name, "line": 1,
                                     "severity": "error", "message": "Escaping source"}])
        self.panel._on_row_double_clicked(self.panel._table.item(0, 0))
        self.assertEqual(self.locations, [])
        jobs = self.queued_workers()
        self.panel._run_manual_check()
        self.finish_job(jobs, 0)
        self.assertEqual(self.panel._all_diagnostics, [])
        self.assertNotIn(str(link), self.panel._last_mtimes)

    def test_continuous_typing_coalesces_into_one_stable_retry(self):
        jobs = self.queued_workers()
        self.buffers[str(self.source)] = "void setup() {\n delay(1)\n}\n"
        self.panel._run_manual_check()
        self.buffers[str(self.source)] += "\n"
        self.finish_job(jobs, 0)
        for revision in range(1, 6):
            self.buffers[str(self.source)] += "\n"
            self.revision[0] = revision
            self.fire_retry()
            self.assertEqual(len(jobs), 1)
            self.assertTrue(self.panel._retry_timer.isActive())
            self.assertFalse(self.panel._is_checking)
        self.fire_retry()
        self.assertEqual(len(jobs), 2)
        self.assertTrue(self.panel._is_checking)
        self.finish_job(jobs, 1)
        self.assertFalse(self.panel._retry_timer.isActive())
        self.assertEqual(len(self.deliveries), 1)

    def test_changed_editor_revision_forces_check_after_save_to_same_bytes(self):
        jobs = self.queued_workers()
        self.panel._execute_analysis(None)
        self.finish_job(jobs, 0)
        with patch.object(syntax_checker, "analyze_files_parallel",
                          wraps=syntax_checker.analyze_files_parallel) as parse:
            self.revision[0] += 1
            self.panel._execute_analysis(None)
            self.finish_job(jobs, 1)
            parse.assert_called_once()
        self.assertEqual(self.buffers, {})
        self.assertEqual(self.panel._last_buffer_revision, self.revision[0])

    def test_coarse_timestamps_never_hide_same_size_source_change(self):
        self.source.write_bytes(b"int value = 3;\n")
        before = self.source.stat()
        jobs = self.queued_workers()
        self.panel._execute_analysis(None)
        self.finish_job(jobs, 0)
        self.source.write_bytes(b"int value = 3 \n")
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.panel._execute_analysis(None)
        self.finish_job(jobs, 1)
        self.assertEqual(len(self.panel._all_diagnostics), 1)
        self.assertIn("semicolon", self.panel._all_diagnostics[0]["message"])

    def test_clear_cancels_deferred_retry(self):
        jobs = self.queued_workers()
        self.panel._run_manual_check()
        self.buffers[str(self.source)] = "void setup() {\n"
        self.finish_job(jobs, 0)
        self.assertTrue(self.panel._retry_timer.isActive())
        self.panel.clear()
        self.assertFalse(self.panel._retry_timer.isActive())
        self.assertIsNone(self.panel._retry_state)
        self.assertFalse(self.panel._retry_manual)

    def test_source_read_failure_preserves_diagnostics_and_reports_failed_check(self):
        self.panel.set_diagnostics([{"file": self.source.name, "line": 1,
                                     "severity": "warning", "message": "Keep this result"}])
        jobs = self.queued_workers()
        self.panel._run_manual_check()
        with patch.object(Path, "open", side_effect=OSError("Fixture source is unreadable")):
            self.finish_job(jobs, 0)
        self.assertIn("failed", self.panel._lbl_status.text().lower())
        self.assertIn("Fixture source is unreadable", self.panel._lbl_status.toolTip())
        self.assertEqual(self.panel._all_diagnostics[0]["message"], "Keep this result")

    def test_gui_snapshot_filter_never_probes_storage(self):
        self.buffers[str(self.source)] = "void setup() {}\n"
        with patch.object(Path, "resolve", side_effect=AssertionError("GUI storage probe")), \
                patch.object(Path, "is_symlink", side_effect=AssertionError("GUI storage probe")):
            self.assertEqual(self.panel._current_buffers(), self.buffers)

    def test_canonical_escape_is_rejected_even_without_symlink_flag(self):
        link = self.project / "linked.cpp"
        external = self.project.parent / "external.cpp"
        link.write_text("void external() {\n", encoding="utf-8")
        self.buffers[str(link)] = "void external() {\n"
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            return external if path == link else original_resolve(path, *args, **kwargs)
        jobs = self.queued_workers()
        with patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            self.panel.set_diagnostics([{"file": link.name, "line": 1,
                                         "severity": "error", "message": "Escaping source"}])
            self.panel._on_row_double_clicked(self.panel._table.item(0, 0))
            self.panel._run_manual_check()
            self.finish_job(jobs, 0)
        self.assertEqual(self.locations, [])
        self.assertEqual(self.panel._all_diagnostics, [])
        self.assertNotIn(str(link), self.panel._last_mtimes)

    def test_project_character_budget_is_reported_without_clean_result(self):
        self.panel._MAX_PROJECT_CHARACTERS = 10
        jobs = self.queued_workers()
        self.panel._run_manual_check()
        self.finish_job(jobs, 0)
        self.assertIn("failed", self.panel._lbl_status.text().lower())
        self.assertIn("memory limit", self.panel._lbl_status.toolTip())
        self.assertEqual(self.deliveries, [])

    def test_project_source_budget_is_reported_without_clean_result(self):
        self.panel._MAX_PROJECT_SOURCES = 0
        jobs = self.queued_workers()
        self.panel._run_manual_check()
        self.finish_job(jobs, 0)
        self.assertIn("failed", self.panel._lbl_status.text().lower())
        self.assertIn("source limit", self.panel._lbl_status.toolTip())
        self.assertEqual(self.deliveries, [])

    def test_worker_reuses_one_captured_source_read_for_analysis(self):
        jobs = self.queued_workers()
        with patch.object(syntax_checker, "read_source_snapshot",
                          wraps=syntax_checker.read_source_snapshot) as read, \
                patch.object(syntax_checker, "analyze_file_syntax",
                             side_effect=AssertionError("Duplicate source read")):
            self.panel._run_manual_check()
            self.finish_job(jobs, 0)
            read.assert_called_once_with(self.source)
        self.assertNotIn("failed", self.panel._lbl_status.text().lower())

    @unittest.skipUnless(os.name == "nt", "Windows paths are case-insensitive")
    def test_dirty_snapshot_path_case_does_not_duplicate_saved_diagnostics(self):
        self.source.write_text("void setup() {\n delay(1)\n}\n", encoding="utf-8")
        self.buffers[str(self.source).swapcase()] = "void setup() {}\nvoid loop() {}\n"
        jobs = self.queued_workers()
        self.panel._run_manual_check()
        self.finish_job(jobs, 0)
        self.assertEqual(self.panel._all_diagnostics, [])
        self.assertEqual(len(self.panel._last_mtimes), 1)

    def test_bridge_submit_failure_releases_reservation_and_next_check_succeeds(self):
        from main.qt.editor_panel import EditorBridgeAPI
        bridge = EditorBridgeAPI(SimpleNamespace(active_file_path=str(self.source)))
        self.addCleanup(bridge.deleteLater)
        jobs = []
        submissions = [0]
        def submit(work):
            submissions[0] += 1
            if submissions[0] == 1:
                raise RuntimeError("Fixture executor is temporarily unavailable")
            jobs.append(work)
            return SimpleNamespace()
        executor = SimpleNamespace(submit=submit)
        notifications = []
        receive = notifications.append
        signals.notification.connect(receive)
        self.addCleanup(signals.notification.disconnect, receive)
        with patch.object(syntax_checker, "get_syntax_executor", return_value=executor):
            self.assertEqual(bridge.realtime_check_syntax(str(self.source), "void old() {"), "null")
            self.assertFalse(bridge._syntax_running)
            self.assertIsNone(bridge._syntax_pending)
            self.assertEqual(self.deliveries, [])
            self.assertEqual(len(notifications), 1)
            self.assertIn("temporarily unavailable", notifications[0]["message"])
            bridge.realtime_check_syntax(str(self.source), "void setup() {}\nvoid loop() {}\n")
            self.assertTrue(bridge._syntax_running)
            self.assertEqual(len(jobs), 1)
            jobs[0]()
            self.flush()
        self.assertFalse(bridge._syntax_running)
        self.assertIsNone(bridge._syntax_pending)
        self.assertEqual(self.deliveries, [[]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
