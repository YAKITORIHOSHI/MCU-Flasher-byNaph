"""Isolated upload connection/output checks: no serial, builders or live writes."""
from __future__ import annotations

import ast
import io
import os
import queue
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent.parent


class TrackingSerial:
    def __init__(self):
        self.is_open = True
        self.writes = []

    def __setattr__(self, name, value):
        if name in ("dtr", "rts"):
            self.writes.append((name, value))
        object.__setattr__(self, name, value)


class UploadWorkerChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/upload-workers"
        audit.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        tree = ast.parse((ROOT / "main/web_bridge.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MCUWebBackendAPI")
        names = {"_soft_reset_esptool_write", "_write_esptool_connect_config",
                 "_fast_upload_retry_allowed", "pulse_dtr_reset", "start_services",
                 "refresh_board_catalog", "update_skip_compile_availability", "stop_operation"}
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
        cls.bases = []
        iterator = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "_iter_process_output")
        self.processes = []
        self.outputs = []

        def spawn(*args, **kwargs):
            code, output = self.outputs.pop(0)
            proc = SimpleNamespace(stdout=io.StringIO(output), returncode=code,
                                   poll=lambda: code, wait=lambda **_: code)
            self.processes.append((args, kwargs, proc))
            return proc

        self.ns = dict(Path=Path, os=os, sys=SimpleNamespace(platform="win32"),
                       time=SimpleNamespace(sleep=lambda _: None, monotonic=time.monotonic,
                                            perf_counter=time.perf_counter),
                       threading=threading, queue=queue, re=re,
                       subprocess=SimpleNamespace(Popen=spawn, PIPE=-1, STDOUT=-2,
                                                  CREATE_NO_WINDOW=0, TimeoutExpired=TimeoutError),
                       _parse_esptool_wrote=lambda _: None)
        exec(compile(ast.Module(body=[iterator, cls], type_ignores=[]), "isolated_upload", "exec"), self.ns)
        self.api = self.ns["MCUWebBackendAPI"]()
        b = self.api
        b.current_board, b.current_port = "Fixture ESP32", "COM99"
        b.sketch_dir_path = self.root
        b.active_operation = "upload"
        b.is_busy = False
        b._serial_generation = 1
        b._serial_lock = threading.Lock()
        b._serial_conn = None
        b._services_lock = threading.Lock()
        b._services_started = False
        b._stop_telemetry = threading.Event()
        b._stop_port_monitor = threading.Event()
        b._telemetry_thread = b._port_monitor_thread = None
        b._telemetry_loop = b._port_monitor_loop = Mock()
        b._catalog_lock = threading.Lock()
        b._catalog_refresh_running = False
        b._catalog_refresh_generation = 0
        b._skip_compile_check_lock = threading.Lock()
        b._skip_compile_check_gen = 0
        b._skip_compile_check_running = False
        b._kill_active_process_tree = Mock()
        b.emit = Mock()
        b._get_esptool_cmd = lambda: ["mock-esptool"]
        b._resolve_board_info = lambda *_: {"platform": "espressif32", "board": "fixture", "framework": "arduino"}
        b._esptool_target = lambda *_: ("esp32", "0x1000")
        b._is_native_usb_port = lambda _: False
        b._effective_cache_root = lambda _: self.root / "fixture-cache"
        b._append_connecting_progress = Mock()
        b._print_chip_info_box = Mock()
        b._append_fast_upload_metadata = Mock()
        b._new_upload_progress_state = lambda *_: {"stages": [{"key": "Firmware"}]}
        b._consume_esptool_upload_progress = lambda *_, **__: False
        b._record_fast_upload_diagnostic = Mock()
        b._is_port_present = lambda _: True
        self.bins = {"platform": "espressif8266", "firmware": self.root / "firmware.bin",
                     "upload_speed": "460800"}

    @staticmethod
    def wrong_boot():
        return 2, "Connecting........\nERROR: A fatal error occurred: Failed to connect to ESP32: Wrong boot mode detected (0x13)!\n"

    def test_fast_children_receive_reset_config_and_exact_budget(self):
        self.outputs = [self.wrong_boot() for _ in range(10)]
        ok, error, attempts = self.api._soft_reset_esptool_write(self.bins, "COM99")
        self.assertFalse(ok)
        self.assertIn("Wrong boot mode", error)
        self.assertEqual(attempts, 10)
        self.assertEqual(len(self.processes), 10)
        for args, kwargs, proc in self.processes:
            command = args[0]
            self.assertEqual(command[command.index("--connect-attempts") + 1], "1")
            self.assertEqual(command[command.index("--port") + 1], "COM99")
            config = Path(kwargs["env"]["ESPTOOL_CFGFILE"]).read_text(encoding="utf-8")
            self.assertIn("D1|R0|W0.5", config)
            self.assertEqual(kwargs["env"]["ESPTOOL_CONNECT_ATTEMPTS"], "1")
            self.assertTrue(proc.stdout.closed)
        self.assertIsNone(self.api._active_process)

    def test_ninth_connection_attempt_can_still_succeed(self):
        self.outputs = [self.wrong_boot() for _ in range(8)] + [(0, "Connected to ESP32\n")]
        self.assertEqual(self.api._soft_reset_esptool_write(self.bins, "COM99"), (True, "", 9))
        self.assertEqual(len(self.processes), 9)

    def test_first_erase_is_terminal_no_replay(self):
        self.outputs = [(2, "Connected to ESP32\nFlash will be erased from 0x0\nERROR: Serial exception\n")]
        ok, _, attempts = self.api._soft_reset_esptool_write(self.bins, "COM99")
        self.assertFalse(ok)
        self.assertEqual(attempts, 1)
        self.assertEqual(len(self.processes), 1)
        self.assertTrue(self.api._last_fast_upload_write_started)
        self.assertEqual(self.api._last_fast_upload_failure_kind, "flash")

    def test_tool_failure_is_not_misreported_as_ten_connection_failures(self):
        self.outputs = [(1, "mock-python: No module named esptool\n")]
        ok, error, attempts = self.api._soft_reset_esptool_write(self.bins, "COM99")
        self.assertFalse(ok)
        self.assertIn("No module named esptool", error)
        self.assertEqual(attempts, 1)
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.api._last_fast_upload_failure_kind, "tool")

    def test_reader_start_failure_never_claims_untouched_flash(self):
        self.outputs = [(0, "Connected to ESP32\nFlash will be erased\nWriting at 0x0000\n")]
        self.ns["threading"] = SimpleNamespace(Event=threading.Event,
            Thread=Mock(side_effect=RuntimeError("Reader resources exhausted")))
        ok, error, attempts = self.api._soft_reset_esptool_write(self.bins, "COM99")
        self.assertFalse(ok)
        self.assertIn("firmware may have changed", error)
        self.assertEqual(attempts, 1)
        self.assertEqual(len(self.processes), 1)
        self.assertTrue(self.api._last_fast_upload_write_started)
        self.assertEqual(self.api._last_fast_upload_failure_kind, "flash")
        self.assertTrue(self.processes[0][2].stdout.closed)

    def test_pipe_read_error_is_terminal_even_before_write_telemetry(self):
        class BrokenStdout:
            closed = False

            def readline(self, size=-1):
                raise OSError("Uploader output pipe failed")

            def close(self):
                self.closed = True

        stdout = BrokenStdout()
        proc = SimpleNamespace(stdout=stdout, returncode=2, poll=lambda: 2, wait=lambda **_: 2)
        self.ns["subprocess"].Popen = Mock(return_value=proc)
        ok, error, attempts = self.api._soft_reset_esptool_write(self.bins, "COM99")
        self.assertFalse(ok)
        self.assertIn("firmware may have changed", error)
        self.assertEqual(attempts, 1)
        self.ns["subprocess"].Popen.assert_called_once()
        self.assertTrue(self.api._last_fast_upload_write_started)
        self.assertEqual(self.api._last_fast_upload_failure_kind, "flash")
        self.assertTrue(stdout.closed)

    def test_native_usb_and_posix_keep_their_reset_strategy(self):
        config_root = self.root / "fixture-cache"
        for platform, mode in (("win32", "usb-reset"), ("linux", "default-reset")):
            self.ns["sys"].platform = platform
            config_path = self.api._write_esptool_connect_config(config_root, 10, mode)
            text = Path(config_path).read_text(encoding="utf-8")
            self.assertNotIn("custom_reset_sequence", text)

    def test_fast_native_usb_never_overrides_transport_reset(self):
        self.api._is_native_usb_port = lambda _: True
        self.outputs = [(0, "Connected to ESP32\n")]
        self.api._soft_reset_esptool_write(self.bins, "COM99")
        env = self.processes[0][1]["env"]
        self.assertNotIn("custom_reset_sequence", Path(env["ESPTOOL_CFGFILE"]).read_text())

    def test_late_silent_reset_cannot_touch_a_new_target(self):
        queued = []
        self.ns["threading"] = SimpleNamespace(Thread=lambda **kw: SimpleNamespace(start=lambda: queued.append(kw["target"])))
        conn = TrackingSerial()
        self.api._serial_conn = conn
        self.api.pulse_dtr_reset()
        self.api.current_board = "Changed target"
        queued.pop()()
        self.assertFalse(conn.writes)

    def test_silent_reset_revalidates_after_target_resolution(self):
        queued = []
        self.ns["threading"] = SimpleNamespace(Thread=lambda **kw: SimpleNamespace(start=lambda: queued.append(kw["target"])))
        b = self.api
        for change in ("board", "busy", "connection"):
            b.current_board = "Fixture ESP32"
            b.is_busy = False
            b.active_operation = None
            b._last_dtr_pulse_time = 0
            conn = TrackingSerial()
            b._serial_conn = conn

            def resolve(*_, change=change):
                if change == "board":
                    b.current_board = "New target"
                elif change == "busy":
                    b.is_busy = True
                    b.active_operation = "upload"
                else:
                    b._serial_conn = TrackingSerial()
                return {"platform": "espressif32"}

            b._resolve_board_info = resolve
            b.pulse_dtr_reset()
            queued.pop()()
            self.assertFalse(conn.writes, change)

    def test_silent_reset_waveform_holds_serial_handoff_lock(self):
        queued = []
        self.ns["threading"] = SimpleNamespace(Thread=lambda **kw: SimpleNamespace(start=lambda: queued.append(kw["target"])))
        b = self.api
        conn = TrackingSerial()
        b._serial_conn = conn
        self.ns["time"].sleep = lambda _: self.assertTrue(b._serial_lock.locked())
        b.pulse_dtr_reset()
        queued.pop()()
        self.assertTrue(conn.writes)
        self.assertFalse(b._serial_lock.locked())

    def test_silent_reset_worker_failure_is_reported_without_replay(self):
        self.ns["threading"] = SimpleNamespace(Thread=Mock(side_effect=RuntimeError("No thread resources")))
        b = self.api
        conn = TrackingSerial()
        b._serial_conn = conn
        b.pulse_dtr_reset()
        self.assertFalse(conn.writes)
        self.ns["threading"].Thread.assert_called_once()
        messages = [call.args[1]["text"] for call in b.emit.call_args_list
                    if call.args[0] == "serial:log"]
        self.assertTrue(any("Reset pulse could not start" in text for text in messages))
        self.assertEqual(b._last_dtr_pulse_time, 0)

    def test_abandoned_output_iterator_releases_bounded_reader(self):
        before = {t.ident for t in threading.enumerate()}
        stream = io.StringIO("short line\n" * 5000)
        iterator = self.ns["_iter_process_output"](
            SimpleNamespace(stdout=stream), lambda: False, lambda: None, lambda _: None,
            poll_interval=0.02)
        self.assertEqual(next(iterator), "short line\n")
        iterator.close()
        remaining = [t for t in threading.enumerate()
                     if t.ident not in before and t.name == "MCU_BuildOutputReader"]
        self.assertFalse(remaining)

    def test_worker_start_failure_releases_services_catalog_and_cache_checks(self):
        self.ns["threading"] = SimpleNamespace(Thread=Mock(side_effect=RuntimeError("No thread resources")))
        b = self.api
        b.active_operation = None
        b.start_services()
        self.assertFalse(b._services_started)
        b.refresh_board_catalog()
        self.assertFalse(b._catalog_refresh_running)
        b.update_skip_compile_availability()
        self.assertFalse(b._skip_compile_check_running)

    def test_repeated_stop_dispatches_once_and_cannot_unlock_a_live_worker(self):
        queued = []
        self.ns["threading"] = SimpleNamespace(Thread=lambda **kw: SimpleNamespace(start=lambda: queued.append(kw["target"])))
        b = self.api
        b.is_busy = True
        b.active_operation = "compile"
        b._current_op_phase = "compiling"
        b._stop_requested = False
        b._active_process = None
        b._operation_worker = SimpleNamespace(is_alive=lambda: True)
        b.stop_operation()
        b.stop_operation()
        self.assertEqual(len(queued), 1)
        queued.pop()()
        self.assertTrue(b.is_busy)
        b._kill_active_process_tree.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
