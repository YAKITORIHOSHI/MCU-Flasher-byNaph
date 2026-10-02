#!/usr/bin/env python3
"""Hardware-free performance regressions with isolated fixtures and real Qt.

Checks bounded streaming memory, latest-edit parsing, background service startup
and warm-launch fallback without running installers or writing live metadata.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
from PySide6.QtCore import QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from main.core import config
from src.modules.runtime_resources import performance_profile

APP = QApplication.instance() or QApplication([])


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        QTest.qWait(5)
    raise AssertionError("Background operation did not finish")


class PerformanceChecks(unittest.TestCase):
    def setUp(self):
        self.config_patch = patch.object(config, "_load_raw_config", return_value={"shared": {}, "instances": {}})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def test_streaming_display_memory_and_no_newline(self):
        from main.qt.console_panel import ConsolePanel
        from main.qt.serial_panel import SerialOutputView
        for cores in (4, 6, 8):
            with patch("src.modules.runtime_resources.performance_profile", return_value=performance_profile(cores, 8)):
                for cls in (ConsolePanel, SerialOutputView):
                    widget = cls()
                    for i in range(20000):
                        widget.append_log({"text": f"{i}:" + "x" * 1000, "newline": False})
                    self.assertLessEqual(widget._queue.chars, widget._queue.max_chars)
                    self.assertGreater(widget._queue.dropped, 0)
                    while widget._queue:
                        widget._flush_queue()
                    self.assertIn("19999:", widget.toPlainText())
                    self.assertIn("older entries omitted", widget.toPlainText())
                    for _ in range(4):
                        for _ in range(80):
                            widget.append_log({"text": "y" * 8000, "newline": False})
                        while widget._queue:
                            widget._flush_queue()
                    self.assertLessEqual(widget.document().characterCount(), widget._history_limit + 1)
                    self.assertLessEqual(widget.document().lastBlock().length(), 16384)
                    self.assertLessEqual(widget._entries.chars, widget._entries.max_chars)
                    widget.clear()
                    widget.set_timestamp_enabled(True)
                    self.assertEqual(widget.toPlainText(), "")
                    widget.deleteLater()
        APP.processEvents()

    def test_serial_ansi_clear_does_not_resurrect_history(self):
        from main.qt.serial_panel import SerialOutputView
        widget = SerialOutputView()
        widget.append_log({"lines": ["old screen", "\x1b[2Jnew screen"]})
        widget._flush_queue()
        widget.set_timestamp_enabled(True)
        self.assertNotIn("old screen", widget.toPlainText())
        self.assertIn("new screen", widget.toPlainText())
        widget.deleteLater()

    def test_bad_progress_pattern_cannot_stop_console(self):
        from main.qt.console_panel import ConsolePanel
        widget = ConsolePanel()
        widget.append_log({"text": "compile error", "replace_pattern": "[invalid"})
        widget._flush_queue()
        self.assertIn("compile error", widget.toPlainText())
        widget.deleteLater()

    def test_worker_logs_are_bounded_before_qt_event_delivery(self):
        from main.qt.signals import MCUSignals
        bus = MCUSignals()
        wakeups = []
        bus._logs_ready.connect(lambda: wakeups.append(1))
        def producer():
            for i in range(30000):
                bus.queue_log("console", {"text": str(i) + "x" * 1000})
        worker = threading.Thread(target=producer)
        worker.start()
        worker.join()
        buffer = bus._pending_logs["console"]
        self.assertLessEqual(buffer.chars, buffer.max_chars)
        APP.processEvents()
        self.assertEqual(len(wakeups), 1)
        received = []
        bus.console_log.connect(received.append)
        while buffer:
            bus._flush_log_signals()
        self.assertIn("29999", received[-1]["text"])
        self.assertTrue(any("older entries omitted" in r["text"] for r in received))
        self.assertFalse(bus._log_timer.isActive())
        bus.deleteLater()

    def test_syntax_completion_is_on_gui_thread_and_can_repeat(self):
        from main.qt.syntax_panel import SyntaxPanel
        from src import syntax_checker
        scratch = ROOT / "temp/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            path = Path(directory) / "sample.ino"
            path.write_text("void setup() {\n int x = 0\n}\n", encoding="utf-8")
            backend = SimpleNamespace(get_project_dir=lambda: directory, is_busy=False)
            panel = SyntaxPanel(backend)
            panel._bg_timer.stop()
            threads = []
            original = panel.set_diagnostics
            def record(diags):
                threads.append(QThread.currentThread())
                original(diags)
            panel.set_diagnostics = record
            panel._run_manual_check()
            wait_until(lambda: not panel._is_checking)
            self.assertTrue(threads)
            self.assertTrue(all(thread == APP.thread() for thread in threads))
            with patch.object(syntax_checker, "analyze_files_parallel", side_effect=OSError("fixture read failed")):
                panel._run_manual_check()
                wait_until(lambda: not panel._is_checking)
                self.assertIn("failed", panel._lbl_status.text())
            panel._run_manual_check()
            wait_until(lambda: not panel._is_checking)
            self.assertNotIn("failed", panel._lbl_status.text())
            panel.deleteLater()

    def test_editor_only_parses_latest_pending_revision(self):
        from main.qt.editor_panel import EditorBridgeAPI
        from main.qt.signals import signals
        from src import syntax_checker
        backend = SimpleNamespace(active_file_path="sample.ino")
        bridge = EditorBridgeAPI(backend)
        entered, release = threading.Event(), threading.Event()
        calls = []
        diagnostics = []
        def analyze(content, path):
            calls.append(content)
            if content == "first":
                entered.set()
                release.wait(2)
            return [{"file": str(path), "line": 1, "message": content}]
        signals.syntax_errors.connect(diagnostics.append)
        try:
            with patch.object(syntax_checker, "analyze_cpp_syntax", side_effect=analyze):
                self.assertEqual(bridge.realtime_check_syntax("sample.ino", "first"), "null")
                self.assertTrue(entered.wait(1))
                for i in range(100):
                    bridge.realtime_check_syntax("sample.ino", f"revision {i}")
                release.set()
                wait_until(lambda: not bridge._syntax_running and bridge._syntax_pending is None)
            self.assertEqual(calls, ["first", "revision 99"])
            self.assertEqual([d[0]["message"] for d in diagnostics], ["revision 99"])
        finally:
            release.set()
            signals.syntax_errors.disconnect(diagnostics.append)
            bridge.deleteLater()

    def test_project_switch_discards_old_syntax_results(self):
        from main.qt.syntax_panel import SyntaxPanel
        from main.qt.signals import signals
        from src import syntax_checker
        scratch = ROOT / "temp/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            a, b = root / "a", root / "b"
            a.mkdir()
            b.mkdir()
            (a / "sample.ino").write_text("void setup() {}", encoding="utf-8")
            current = [str(a)]
            panel = SyntaxPanel(SimpleNamespace(get_project_dir=lambda: current[0], is_busy=False))
            panel._bg_timer.stop()
            entered, release = threading.Event(), threading.Event()
            def analyze(_files):
                entered.set()
                release.wait(2)
                return [{"file": str(a / "sample.ino"), "message": "stale", "line": 1}]
            try:
                panel.set_diagnostics([{"message": "old project", "line": 1}])
                with patch.object(syntax_checker, "analyze_files_parallel", side_effect=analyze):
                    panel._run_manual_check()
                    self.assertTrue(entered.wait(1))
                    current[0] = str(b)
                    signals.project_updated.emit({"path": str(b)})
                    self.assertEqual(panel._all_diagnostics, [])
                    release.set()
                    wait_until(lambda: not panel._is_checking)
                self.assertEqual(panel._all_diagnostics, [])
            finally:
                release.set()
                panel.deleteLater()

    def test_low_end_parser_and_cache_budgets(self):
        from src import syntax_checker
        for cores in (4, 6):
            with patch("src.modules.runtime_resources.performance_profile", return_value=performance_profile(cores, 8)):
                self.assertEqual(syntax_checker.get_optimal_worker_count(), 1)
        cache = {}
        for revision in range(200):
            syntax_checker._remember_diagnostics(cache, revision, [{"message": "error"}] * 500)
        self.assertLessEqual(sum(len(value) for value in cache.values()), 8192)
        from main.web_bridge import _SketchRAMCache
        ram = _SketchRAMCache()
        for revision in range(200):
            ram._cache_content(str(revision), 500_000, revision, "x" * 500_000)
        self.assertLessEqual(ram._content_chars, ram._content_limit)
        ram.invalidate()
        self.assertEqual(ram._content_chars, 0)

    def test_services_do_not_scan_ports_on_calling_thread(self):
        from main.web_bridge import MCUWebBackendAPI
        api = MCUWebBackendAPI.__new__(MCUWebBackendAPI)
        api._services_lock = threading.Lock()
        api._services_started = False
        api._stop_telemetry = threading.Event()
        api._stop_port_monitor = threading.Event()
        api._telemetry_thread = api._port_monitor_thread = None
        api._telemetry_loop = lambda: api._stop_telemetry.wait(2)
        threads = []
        started = threading.Event()
        def monitor():
            threads.append(threading.get_ident())
            started.set()
            api._stop_port_monitor.wait(2)
        api._port_monitor_loop = monitor
        api._scan_ports = Mock(side_effect=AssertionError("UI thread enumerated ports"))
        api.refresh_board_catalog = Mock()
        api.start_services()
        api.start_services()
        self.assertTrue(started.wait(1))
        api._stop_telemetry.set()
        api._stop_port_monitor.set()
        api._telemetry_thread.join(1)
        api._port_monitor_thread.join(1)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        api._scan_ports.assert_not_called()

    def test_skip_compile_edits_coalesce_to_one_worker(self):
        from main.web_bridge import MCUWebBackendAPI
        api = MCUWebBackendAPI.__new__(MCUWebBackendAPI)
        api._skip_compile_check_lock = threading.Lock()
        api._skip_compile_check_running = False
        api._skip_compile_check_gen = 0
        api._stop_port_monitor = threading.Event()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def check():
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
            return True
        api.check_can_skip_compile = check
        api.emit = Mock()
        api.update_skip_compile_availability()
        self.assertTrue(entered.wait(1))
        for _ in range(100):
            api.update_skip_compile_availability()
        release.set()
        wait_until(lambda: not api._skip_compile_check_running)
        self.assertEqual(len(calls), 2)
        api.emit.assert_called_once_with("skip_compile:availability", True)

    def test_warm_launch_health_and_failed_child_fallback(self):
        # Load only the pure health helpers: never execute bootstrap/setup.
        names = {"_startup_app_fingerprint", "_startup_installation_identity", "_startup_required_paths", "_read_startup_health_snapshot", "_write_startup_health_snapshot", "_explicit_setup_requested", "_try_fast_normal_launch"}
        tree = ast.parse((ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig"))
        helpers = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[])
        scratch = ROOT / "temp/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            fixture = Path(directory)
            site = fixture / "site"
            site.mkdir()
            gui = fixture / "mcu_flash_gui.py"
            gui.write_text("# fixture", encoding="utf-8")
            dep = site / "serial"
            dep.mkdir()
            qt = site / "PySide6"
            qt.mkdir()
            for name in ("QtCore.pyd", "QtGui.pyd", "QtWidgets.pyd", "QtWebChannel.pyd", "QtWebEngineCore.pyd", "QtWebEngineWidgets.pyd"):
                (qt / name).write_text("fixture presence only", encoding="utf-8")
            (site / "winpty").mkdir()
            for name in ("src/editor/index.html", "src/editor/bundle.js", "src/editor/qwebchannel.js", "src/editor/terminal.html", "src/assets/xterm/xterm.js", "src/assets/xterm/xterm.css"):
                asset = fixture / name
                asset.parent.mkdir(parents=True, exist_ok=True)
                asset.write_text("offline asset fixture", encoding="utf-8")
            scope = {"Path": Path, "sys": sys, "os": os, "time": time, "json": json, "subprocess": subprocess,
                     "SCRIPT_DIR": fixture, "GUI_SCRIPT": gui, "STARTUP_HEALTH_FILE": fixture / "health.json",
                     "STARTUP_HEALTH_SCHEMA": 3, "_STARTUP_REQUIRED_PACKAGE_DIRS": ("serial",),
                     "_startup_site_packages_dir": lambda: site, "_record_bootstrap_log": Mock()}
            exec(compile(helpers, "<isolated bootstrap helpers>", "exec"), scope)
            from src.modules import offline_bootstrap
            import platform as host_platform
            offline_core = fixture / "native-core"
            offline_core.mkdir()
            (offline_core / "fixture.json").write_text("{}")
            certificate = {"schema": offline_bootstrap.SCHEMA, "plan": offline_bootstrap.plan_hash(offline_bootstrap.load_plan()),
                           "default_plan": offline_bootstrap.plan_hash(offline_bootstrap.load_plan()),
                           "host": sys.platform, "architecture": host_platform.machine(), "files": ["fixture.json"]}
            (offline_core / offline_bootstrap.MARKER).write_text(json.dumps(certificate))
            with patch.object(sys, "argv", ["bootstrap.py"]), patch.dict(os.environ, {"PLATFORMIO_CORE_DIR": str(offline_core)}), patch.dict(sys.modules, {"crash_detector": SimpleNamespace(detect_previous_crash=lambda: {"crashed": False})}):
                self.assertIsNone(scope["_read_startup_health_snapshot"]())
                self.assertTrue(scope["_write_startup_health_snapshot"]())
                child = Mock()
                child.wait.side_effect = subprocess.TimeoutExpired("fixture", 1)
                scope["_spawn_main_gui"] = Mock(return_value=(child, None))
                self.assertTrue(scope["_try_fast_normal_launch"]())
                for arguments in (["--repair"], ["--plan", "custom-plan.json"]):
                    with patch.object(sys, "argv", ["bootstrap.py", *arguments]):
                        self.assertFalse(scope["_try_fast_normal_launch"]())
                child.wait.side_effect = None
                child.wait.return_value = 1
                self.assertFalse(scope["_try_fast_normal_launch"]())
                self.assertFalse(scope["STARTUP_HEALTH_FILE"].exists())
                self.assertTrue(scope["_write_startup_health_snapshot"]())
                gui.write_text("# changed fixture", encoding="utf-8")
                self.assertIsNone(scope["_read_startup_health_snapshot"]())
                self.assertTrue(scope["_write_startup_health_snapshot"]())
                dep.rmdir()
                self.assertIsNone(scope["_read_startup_health_snapshot"]())


if __name__ == "__main__":
    unittest.main(verbosity=2)
