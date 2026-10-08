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
                 "refresh_board_catalog", "update_skip_compile_availability", "stop_operation",
                 "_upload_status_key", "_append_upload_boot_hint",
                 "_finish_upload_connection_status", "_esptool_chip_mismatch",
                 "_append_connecting_progress", "_upload_worker"}
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
        cls.bases = []
        iterator = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "_iter_process_output")
        upload_classifier = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                                 and n.name == "_classify_platformio_upload_line")
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
                                            perf_counter=time.perf_counter, time=time.time),
                       threading=threading, queue=queue, re=re,
                       subprocess=SimpleNamespace(Popen=spawn, PIPE=-1, STDOUT=-2,
                                                  CREATE_NO_WINDOW=0, TimeoutExpired=TimeoutError,
                                                  STARTUPINFO=lambda: SimpleNamespace(dwFlags=0, wShowWindow=0)),
                       _parse_esptool_wrote=lambda _: None,
                       _parse_esptool_write_progress=lambda _: None,
                       port_occupied_owner=lambda _: None,
                       is_unc_or_network_path=lambda _: False,
                       load_gui_config=lambda: {"clear_console_on_action": False},
                       is_s3_board=lambda _: False,
                       find_pio_executable=lambda: ["mock-platformio"],
                       _refresh_platformio_core_environment=lambda _: (self.root / "fixture-core", {}),
                       SCRIPT_DIR=self.root, DEFAULT_UPLOAD_SPEED="460800")
        exec(compile(ast.Module(body=[iterator, upload_classifier, cls], type_ignores=[]),
                     "isolated_upload", "exec"), self.ns)
        self.api = self.ns["MCUWebBackendAPI"]()
        b = self.api
        b.current_board, b.current_port = "Fixture ESP32", "COM99"
        b.sketch_dir_path = self.root
        b.active_operation = "upload"
        b.is_busy = False
        b._op_session_id = 7
        b._stop_requested = False
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

    def prime_polling(self):
        self.api._append_upload_boot_hint(
            "  💡 Hold BOOT now — the uploader will keep polling the bootloader.")
        self.api._append_connecting_progress(1, 10)

    def retained_status_rows(self):
        # The console verifier checks actual widget replacement. Here retain
        # the latest event per operation key to inspect the worker's outcome.
        rows = {}
        for call in self.api.emit.call_args_list:
            if call.args[0] == "console:log" and call.args[1].get("progress_key"):
                record = call.args[1]
                rows[record["progress_key"]] = record
        return rows

    def assert_terminal_connection(self, text="Connection failed", tag="error"):
        rows = self.retained_status_rows()
        status = rows[self.api._upload_status_key("connection")]
        self.assertIn(text, status["text"])
        self.assertEqual(status["tag"], tag)
        self.assertNotIn("Connecting", status["text"])
        self.assertNotIn("▰", status["text"])
        self.assertNotIn("▱", status["text"])
        self.assertNotIn("BOOT", status["text"])
        hint = rows[self.api._upload_status_key("boot-hint")]["text"]
        self.assertNotIn("Hold BOOT", hint)
        self.assertNotIn("keep polling", hint)
        self.assertFalse(self.api._upload_connection_pending)

    def prepare_upload_caller(self, *, fast):
        b = self.api
        b.current_board = "Fixture ESP32-S3"
        b.upload_speed = "921600"
        b.is_busy = True
        b._active_skip_compile = True
        b._last_compiled_board = b.current_board
        b._last_source_hash = "fixture-source-identity"
        b._serial_thread = None
        b._hash_sources = Mock(return_value="fixture-source-identity")
        firmware = self.root / "firmware.bin"
        firmware.write_bytes(b"isolated firmware fixture")
        b._find_cached_firmware_binary = Mock(return_value=firmware)
        b._load_compile_cache = Mock(side_effect=AssertionError("No live compile-cache read"))
        b._has_prior_build = Mock(return_value=True)
        b._needs_recompile = Mock(return_value=(False, ""))
        b._compile_worker = Mock(side_effect=AssertionError("No compilation in upload fixture"))
        b._get_compat_analysis = Mock(return_value=([], []))
        b._resolve_board_info = Mock(return_value={"platform": "espressif32",
                                                  "board": "fixture-s3", "framework": "arduino"})
        b._esptool_target = Mock(return_value=("esp32s3", "0x0"))
        b._stop_serial_monitor = Mock()
        b._start_serial_monitor = Mock()
        b._release_port_lines = Mock()
        b._trigger_actual_board_reset = Mock()
        b._unmap_unc_after_build = Mock()
        b._print_info_box = Mock()
        b._probe_chip_info = Mock(return_value=False)
        b._get_jobs = Mock(return_value=2)
        cache = self.root / "fixture-cache"
        cache.mkdir(exist_ok=True)
        (cache / "platformio.ini").write_text("; isolated upload fixture\n", encoding="utf-8")
        b._generate_platformio_ini = Mock(side_effect=AssertionError("No metadata generation"))
        bins = dict(self.bins, platform="espressif32", upload_speed=b.upload_speed,
                    bootloader_addr="0x0", board_name=b.current_board)
        for name in ("bootloader", "partitions", "boot_app0"):
            image = self.root / (name + ".bin")
            image.write_bytes(b"isolated upload image")
            bins[name] = image
        b._locate_soft_reset_fast_binaries = Mock(return_value=bins if fast else None)

    def console_messages(self):
        return [call.args[1] for call in self.api.emit.call_args_list
                if call.args[0] == "console:log"]

    def assert_upload_caller_completed(self, *, success):
        self.assertFalse(self.api.is_busy)
        self.assertIsNone(self.api.active_operation)
        phases = [call.args[1] for call in self.api.emit.call_args_list
                  if call.args[0] == "operation:phase"]
        self.assertEqual(phases[-1]["success"], success)
        self.api._compile_worker.assert_not_called()
        self.api._generate_platformio_ini.assert_not_called()

    def test_terminal_connection_rows_are_compact_and_idempotent(self):
        for options, text, tag in (
                ({}, "Connection failed", "error"),
                ({"stopped": True}, "Connection stopped", "info"),
                ({"reason": "Board mismatch", "hint": "  ℹ Detected ESP32; select an ESP32 board."},
                 "Board mismatch", "error")):
            with self.subTest(text=text):
                self.api.emit.reset_mock()
                self.prime_polling()
                self.api._finish_upload_connection_status(**options)
                self.assert_terminal_connection(text, tag)
                before = self.api.emit.call_count
                self.api._finish_upload_connection_status()
                self.assertEqual(self.api.emit.call_count, before)

    def test_status_keys_preserve_previous_operation_outcomes(self):
        self.prime_polling()
        self.api._finish_upload_connection_status()
        old_keys = set(self.retained_status_rows())
        self.api._op_session_id += 1
        self.prime_polling()
        rows = self.retained_status_rows()
        self.assertEqual(len(rows), 4)
        self.assertTrue(old_keys.isdisjoint({self.api._upload_status_key("connection"),
                                           self.api._upload_status_key("boot-hint")}))
        self.assertTrue(any("Connection failed" in rows[key]["text"] for key in old_keys))

    def test_chip_mismatch_parser_accepts_esptool_variants(self):
        for output, expected in (
                ("This chip is ESP32, not ESP32-S3. Wrong chip argument?", ("ESP32", "ESP32-S3")),
                ("A fatal error occurred: This chip is ESP32-C3 not ESP32. Wrong --chip argument?",
                 ("ESP32-C3", "ESP32")),
                ("this chip is esp32-s2, not esp32-s3", ("ESP32-S2", "ESP32-S3"))):
            with self.subTest(output=output):
                self.assertEqual(self.api._esptool_chip_mismatch(output), expected)
        self.assertIsNone(self.api._esptool_chip_mismatch("Chip is ESP32-S3 (revision v0.2)"))
        self.assertIsNone(self.api._esptool_chip_mismatch("Failed to connect to ESP32: Wrong boot mode"))

    def test_wrong_chip_is_terminal_and_preserves_detected_target_diagnostic(self):
        diagnostic = "This chip is ESP32, not ESP32-S3. Wrong chip argument?"
        for header in ("Connecting........\n", "Connecting........\nConnected to ESP32\n"):
            with self.subTest(header=header):
                self.api.emit.reset_mock()
                self.api._record_fast_upload_diagnostic.reset_mock()
                self.processes.clear()
                self.prime_polling()
                self.outputs = [(2, header + "ERROR: A fatal error occurred: " + diagnostic + "\n")]
                ok, error, attempts = self.api._soft_reset_esptool_write(self.bins, "COM99")
                self.assertFalse(ok)
                self.assertIn(diagnostic, error)
                self.assertEqual(attempts, 1)
                self.assertEqual(len(self.processes), 1)
                self.assertEqual(self.api._last_fast_upload_failure_kind, "target")
                self.assertFalse(self.api._last_fast_upload_write_started)
                self.api._record_fast_upload_diagnostic.assert_called_once()
                recorded = self.api._record_fast_upload_diagnostic.call_args.kwargs
                self.assertIn(diagnostic, recorded["error"])
                self.assertTrue(any(diagnostic in line for line in recorded["output_lines"]))
                self.assert_terminal_connection("Board mismatch")
                hint = self.retained_status_rows()[self.api._upload_status_key("boot-hint")]["text"]
                self.assertIn("ESP32", hint)
                self.assertIn("ESP32-S3", hint)
                self.assertNotIn("baud", hint.lower())

    def test_fast_upload_caller_preserves_wrong_chip_error_without_speed_advice(self):
        self.prepare_upload_caller(fast=True)
        diagnostic = "This chip is ESP32, not ESP32-S3. Wrong chip argument?"
        self.outputs = [(2, "Connecting........\nA fatal error occurred: " + diagnostic + "\n")]
        self.api._upload_worker(can_skip=True)
        messages = self.console_messages()
        self.assertTrue(any(diagnostic in record["text"] and record.get("tag") == "error"
                            for record in messages))
        self.assertTrue(any("Upload FAILED" in record["text"] for record in messages))
        self.assertFalse(any("High upload speed" in record["text"] or "Try selecting" in record["text"]
                             for record in messages))
        self.assertEqual(len(self.processes), 1)
        command = self.processes[0][0][0]
        self.assertEqual(command[command.index("--baud") + 1], "921600")
        self.assert_terminal_connection("Board mismatch")
        self.assert_upload_caller_completed(success=False)
        self.api._trigger_actual_board_reset.assert_not_called()
        self.api._release_port_lines.assert_called_once_with("COM99", pulse_reset=False)

    def test_platformio_upload_caller_wrong_chip_omits_manual_boot_guide(self):
        self.prepare_upload_caller(fast=False)
        diagnostic = "This chip is ESP32, not ESP32-S3. Wrong chip argument?"
        self.outputs = [(2, "Connecting........\nA fatal error occurred: " + diagnostic + "\n"
                         "========================= [FAILED] Took 1.00 seconds =========================\n")]
        self.api._upload_worker(can_skip=True)
        messages = self.console_messages()
        self.assertTrue(any(diagnostic in record["text"] and record.get("tag") == "error"
                            for record in messages))
        self.assertFalse(any("Manual Bootloader Mode" in record["text"]
                             or "Press and HOLD" in record["text"]
                             or "while holding 'BOOT'" in record["text"] for record in messages))
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.processes[0][0][0][0], "mock-platformio")
        self.assert_terminal_connection("Board mismatch")
        self.assert_upload_caller_completed(success=False)
        self.api._trigger_actual_board_reset.assert_not_called()
        self.api._release_port_lines.assert_called_once_with("COM99", pulse_reset=False)

    def test_platformio_success_without_chip_telemetry_retires_polling(self):
        self.prepare_upload_caller(fast=False)
        self.outputs = [(0, "========================= [SUCCESS] Took 1.00 seconds =========================\n")]
        self.api._upload_worker(can_skip=True)
        self.assertEqual(len(self.processes), 1)
        rows = self.retained_status_rows()
        status = rows[self.api._upload_status_key("connection")]
        self.assertIn("✔ Connected", status["text"])
        self.assertEqual(status["tag"], "success")
        hint = rows[self.api._upload_status_key("boot-hint")]["text"]
        self.assertIn("release", hint.lower())
        self.assertNotIn("keep polling", hint)
        self.assertFalse(self.api._upload_connection_pending)
        self.assert_upload_caller_completed(success=True)
        self.api._trigger_actual_board_reset.assert_called_once()
        self.api._start_serial_monitor.assert_called_once()
        self.api._release_port_lines.assert_not_called()

    def test_fast_children_receive_reset_config_and_exact_budget(self):
        self.prime_polling()
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
        self.assert_terminal_connection()

    def test_ninth_connection_attempt_can_still_succeed(self):
        self.prime_polling()
        self.outputs = [self.wrong_boot() for _ in range(8)] + [(0, "Connected to ESP32\n")]
        self.assertEqual(self.api._soft_reset_esptool_write(self.bins, "COM99"), (True, "", 9))
        self.assertEqual(len(self.processes), 9)
        rows = self.retained_status_rows()
        status = rows[self.api._upload_status_key("connection")]
        self.assertIn("✔ Connected", status["text"])
        self.assertEqual(status["tag"], "success")
        hint = rows[self.api._upload_status_key("boot-hint")]["text"]
        self.assertIn("release", hint.lower())
        self.assertNotIn("keep polling", hint)
        self.assertFalse(self.api._upload_connection_pending)

    def test_first_erase_is_terminal_no_replay(self):
        self.outputs = [(2, "Connected to ESP32\nFlash will be erased from 0x0\nERROR: Serial exception\n")]
        ok, _, attempts = self.api._soft_reset_esptool_write(self.bins, "COM99")
        self.assertFalse(ok)
        self.assertEqual(attempts, 1)
        self.assertEqual(len(self.processes), 1)
        self.assertTrue(self.api._last_fast_upload_write_started)
        self.assertEqual(self.api._last_fast_upload_failure_kind, "flash")
        self.assertFalse(self.api._upload_connection_pending)

    def test_tool_failure_is_not_misreported_as_ten_connection_failures(self):
        self.prime_polling()
        self.outputs = [(1, "mock-python: No module named esptool\n")]
        ok, error, attempts = self.api._soft_reset_esptool_write(self.bins, "COM99")
        self.assertFalse(ok)
        self.assertIn("No module named esptool", error)
        self.assertEqual(attempts, 1)
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.api._last_fast_upload_failure_kind, "tool")
        self.assert_terminal_connection()

    def test_missing_uploader_executable_finalizes_polling_without_retry(self):
        self.prime_polling()
        self.ns["subprocess"].Popen = Mock(side_effect=FileNotFoundError("Uploader executable is missing"))
        ok, error, attempts = self.api._soft_reset_esptool_write(self.bins, "COM99")
        self.assertFalse(ok)
        self.assertIn("Uploader executable is missing", error)
        self.assertEqual(attempts, 1)
        self.ns["subprocess"].Popen.assert_called_once()
        self.assertEqual(self.api._last_fast_upload_failure_kind, "tool")
        self.assertFalse(self.api._last_fast_upload_write_started)
        self.assert_terminal_connection()

    def test_connection_watchdog_finalizes_polling(self):
        self.prime_polling()
        self.outputs = [(2, "") for _ in range(10)]
        elapsed = iter(range(0, 10000, 100))
        self.ns["time"].monotonic = lambda: next(elapsed)
        self.ns["threading"] = SimpleNamespace(Event=threading.Event,
            Thread=lambda **_: SimpleNamespace(start=lambda: None, join=lambda **_: None,
                                               is_alive=lambda: False))

        class SilentQueue:
            def get(self, **_):
                raise queue.Empty

        self.ns["queue"] = SimpleNamespace(Queue=lambda **_: SilentQueue(),
                                           Empty=queue.Empty, Full=queue.Full)
        ok, error, attempts = self.api._soft_reset_esptool_write(self.bins, "COM99")
        self.assertFalse(ok)
        self.assertIn("timed out", error)
        self.assertLessEqual(attempts, 10)
        self.assert_terminal_connection()

    def test_cancelled_polling_exits_without_launching_or_replaying(self):
        self.prime_polling()
        self.api._stop_requested = True
        ok, _, _ = self.api._soft_reset_esptool_write(self.bins, "COM99")
        self.assertFalse(ok)
        self.assertFalse(self.processes)
        self.assert_terminal_connection("Connection stopped", "info")

    def test_stop_during_connection_does_not_launch_a_second_child(self):
        self.prime_polling()
        self.outputs = [self.wrong_boot()]
        spawn = self.ns["subprocess"].Popen

        def stop_after_launch(*args, **kwargs):
            proc = spawn(*args, **kwargs)
            self.api._stop_requested = True
            return proc

        self.ns["subprocess"].Popen = stop_after_launch
        ok, _, attempts = self.api._soft_reset_esptool_write(self.bins, "COM99")
        self.assertFalse(ok)
        self.assertEqual(attempts, 1)
        self.assertEqual(len(self.processes), 1)
        self.assert_terminal_connection("Connection stopped", "info")

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
        process = SimpleNamespace(poll=lambda: None)
        b._active_process = process
        b._operation_worker = SimpleNamespace(is_alive=lambda: True)
        b.stop_operation()
        b.stop_operation()
        self.assertEqual(len(queued), 1)
        queued.pop()()
        self.assertTrue(b.is_busy)
        b._kill_active_process_tree.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main(verbosity=2)
