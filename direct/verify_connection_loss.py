"""USB loss fixtures: harmless local children, mocked enumeration and persistence."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from main import web_bridge
from main.core import arduino_backend
from main.core.connection_loss import (
    OperationConnectionLost, SerialConnectionGuard, check_write_connection,
    expects_serial_handoff,
)

API = web_bridge.MCUWebBackendAPI
PORT = SimpleNamespace(device="COM99", serial_number="fixture-usb", location="fixture-slot")
INFO = {"platform": "espressif32", "board": "fixture", "framework": "arduino", "upload_protocol": "esptool"}


class FakeProcess:
    returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fixture", timeout)
        return self.returncode


class ConnectionLossChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(dir=ROOT / "temp/audit")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.api = API.__new__(API)
        api = self.api
        api._op_session_id = 1
        api._operation_connection_loss = ""
        api._operation_serial_guard = None
        api._active_process = FakeProcess()
        api._last_known_ports = []
        api.current_port = api._active_port_label = "COM99"
        api.current_board = "Fixture"
        api._active_board_info = dict(INFO)
        api._stop_requested = False
        api.is_busy = True
        api.active_operation = "flash"
        api._current_op_phase = "flashing"
        api.emit = Mock()
        api._start_serial_monitor = Mock()
        api._stop_serial_monitor = Mock()
        self.killed = []
        api._kill_active_process_tree = lambda proc=None: self.killed.append(proc) or (proc or api._active_process).kill()
        self.now = 0.0
        self.ports = [PORT]

    def guard(self, **kwargs):
        return SerialConnectionGuard(self.api, self.api._active_process, "COM99", dict(INFO),
                                     scan=lambda: self.ports, clock=lambda: self.now,
                                     scan_interval=0.1, **kwargs)

    def notices(self):
        return [call.args[1] for call in self.api.emit.call_args_list
                if call.args[0] == "notification"]

    def test_stable_loss_reaps_captured_writer_and_latches_once(self):
        guard = self.guard()
        self.ports = []
        guard.poll()
        self.now = 1.6
        with self.assertRaises(OperationConnectionLost):
            guard.poll()
        self.assertIsNotNone(self.api._active_process.poll())
        self.assertEqual(self.killed, [guard.process])
        self.assertEqual(len(self.notices()), 1)
        with self.assertRaises(OperationConnectionLost):
            check_write_connection(self.api, "COM99", INFO)
        self.assertEqual(len(self.notices()), 1)

    def test_native_esp_usb_identity_gets_bounded_handoff_without_manifest_flags(self):
        self.ports = [SimpleNamespace(device="COM99", vid=0x303A,
                                      serial_number="fixture-usb", location="fixture-slot")]
        guard = self.guard()
        self.assertTrue(guard.handoff)
        self.ports = []
        guard.poll()
        self.now = 2.0
        guard.poll()
        self.assertFalse(self.killed)
        self.ports = [SimpleNamespace(device="COM100", vid=0x303A,
                                      serial_number="fixture-usb", location="fixture-slot")]
        self.now = 3.0
        guard.poll()
        self.assertFalse(self.killed)
        self.ports = []
        self.now = 4.0
        guard.poll()
        self.now = 14.1
        with self.assertRaises(OperationConnectionLost):
            guard.poll()

    def test_cached_native_usb_evidence_is_scoped_to_exact_esp_platform(self):
        self.api._last_known_ports = [{"device": "COM99", "hwid": "USB VID:PID=303A:1001 SER=fixture-usb"}]
        guard = self.guard()
        self.assertTrue(guard.handoff)
        guard.close()
        guard = SerialConnectionGuard(self.api, self.api._active_process, "COM99",
                                      dict(INFO, platform="atmelavr"), scan=lambda: [PORT])
        self.assertFalse(guard.handoff)

    def test_exited_child_does_not_enumerate_or_register_hardware_guard(self):
        self.api._active_process.returncode = 0
        scan = Mock(side_effect=AssertionError("Exited child scanned hardware"))
        guard = SerialConnectionGuard(self.api, self.api._active_process, "COM99", INFO, scan=scan)
        guard.poll()
        self.assertEqual(guard.wait(), 0)
        scan.assert_not_called()
        self.assertIsNone(self.api._operation_serial_guard)

    def test_transient_inventory_gap_is_not_loss(self):
        guard = self.guard()
        self.ports = []
        guard.poll()
        self.now = 1.0
        self.ports = [PORT]
        guard.poll()
        self.now = 3.0
        guard.poll()
        self.assertEqual(self.killed, [])
        self.assertFalse(self.api._operation_connection_loss)

    def test_enumeration_failure_is_unknown_not_disconnect(self):
        guard = self.guard()
        guard.scan = Mock(side_effect=OSError("fixture inventory unavailable"))
        for self.now in (0.0, 3.0, 20.0):
            guard.poll()
        self.assertEqual(self.killed, [])
        self.assertFalse(self.notices())

    def test_stale_session_and_replaced_process_cannot_kill(self):
        guard = self.guard()
        self.ports = []
        guard.poll()
        self.now = 5.0
        self.api._op_session_id += 1
        guard.poll()
        self.api._op_session_id -= 1
        self.api._active_process = FakeProcess()
        guard.poll()
        self.assertEqual(self.killed, [])
        self.assertFalse(self.api._operation_connection_loss)

    def test_nonserial_programmer_ignores_monitor_disappearance(self):
        scan = Mock(side_effect=AssertionError("Nonserial programmer scanned COM ports"))
        info = dict(INFO, upload_protocol="stlink")
        guard = SerialConnectionGuard(self.api, self.api._active_process, "COM99", info, scan=scan)
        guard.poll()
        self.api.current_port = ""
        check_write_connection(self.api, "COM99", info)
        scan.assert_not_called()
        self.assertEqual(self.killed, [])

    def test_declared_handoff_is_bounded_and_accepts_exact_usb_identity(self):
        guard = self.guard(expected_handoff=True)
        self.ports = []
        guard.poll()
        self.now = 5.0
        guard.poll()
        self.assertTrue(guard.defer_selection_clear("COM99"))
        self.ports = [SimpleNamespace(device="COM100", serial_number="fixture-usb", location="fixture-slot")]
        self.now = 11.0
        guard.poll()
        self.assertEqual(self.killed, [])
        self.ports = []
        self.now = 12.0
        guard.poll()
        self.now = 22.1
        with self.assertRaises(OperationConnectionLost):
            guard.poll()

    def test_handoff_rejects_an_unrelated_usb_board(self):
        guard = self.guard(expected_handoff=True)
        self.ports = [SimpleNamespace(device="COM100", serial_number="other", location="other")]
        guard.poll()
        self.now = 10.1
        with self.assertRaises(OperationConnectionLost):
            guard.poll()

    def test_different_serial_in_same_usb_socket_is_not_the_same_mcu(self):
        guard = self.guard(expected_handoff=True)
        self.ports = [SimpleNamespace(device="COM100", serial_number="other", location="fixture-slot")]
        guard.poll()
        self.now = 10.1
        with self.assertRaises(OperationConnectionLost):
            guard.poll()

    def test_loss_remains_failed_when_child_later_exits_zero(self):
        guard = self.guard()
        self.api._operation_connection_loss = "Fixture confirmed loss"
        self.api._active_process.returncode = 0
        with self.assertRaises(OperationConnectionLost):
            guard.wait()
        self.assertEqual(guard.finish(), 0)

    def test_termination_denial_keeps_worker_protected_until_exit(self):
        guard = self.guard()
        self.api._operation_connection_loss = "Fixture confirmed loss"
        self.api._kill_active_process_tree = Mock(side_effect=PermissionError("fixture denial"))
        def delayed_exit(_):
            self.assertTrue(self.api.is_busy)
            self.assertIs(self.api._active_process, guard.process)
            guard.process.returncode = -9
        with patch("main.core.connection_loss.time.sleep", side_effect=delayed_exit):
            self.assertEqual(guard.finish(), -9)
        self.assertTrue(self.api.is_busy)  # Only its owning worker can unlock.

    def test_exact_manifest_declares_1200bps_handoff(self):
        manifest = self.root / "board.json"
        manifest.write_text('{"upload":{"use_1200bps_touch":true}}', encoding="utf-8")
        self.assertTrue(expects_serial_handoff(dict(INFO, pio_manifest=str(manifest))))
        self.assertFalse(expects_serial_handoff(INFO))

    def spawn(self, script="import time; time.sleep(30)"):
        process = subprocess.Popen([sys.executable, "-B", "-c", script], cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8",
                                   **web_bridge._HOST_RUNTIME.process_options(session=True))
        self.api._active_process = process
        self.api._kill_active_process_tree = lambda child=None: API._kill_active_process_tree(self.api, child)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
        self.addCleanup(cleanup)
        return process

    def fast_guard(self, *args, **kwargs):
        return SerialConnectionGuard(*args, **kwargs, scan=lambda: [],
                                     confirm_after=0.08, handoff_after=0.08, scan_interval=0.02)

    def test_real_silent_child_stops_without_waiting_for_a_log_line(self):
        process = self.spawn()
        guard = SerialConnectionGuard(self.api, process, "COM99", INFO, scan=lambda: [],
                                      confirm_after=0.08, scan_interval=0.02)
        started = time.monotonic()
        with self.assertRaises(OperationConnectionLost):
            list(web_bridge._iter_process_output(process, lambda: False, lambda: None,
                                                lambda _: None, poll_interval=0.03, on_poll=guard.poll))
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertIsNotNone(process.poll())

    def test_healthy_silent_child_is_not_stopped_by_transport_guard(self):
        process = self.spawn()
        guard = SerialConnectionGuard(self.api, process, "COM99", INFO, scan=lambda: [PORT],
                                      confirm_after=0.02, scan_interval=0.02)
        started = time.monotonic()
        list(web_bridge._iter_process_output(
            process, lambda: time.monotonic() - started > 0.12,
            lambda: self.api._kill_active_process_tree(process), lambda _: None,
            poll_interval=0.03, on_poll=guard.poll))
        self.assertFalse(self.api._operation_connection_loss)
        self.assertFalse(self.notices())

    def test_reset_output_reader_reaps_silent_owned_child(self):
        process = self.spawn()
        with patch("main.core.connection_loss.SerialConnectionGuard", side_effect=self.fast_guard):
            with self.assertRaises(OperationConnectionLost):
                list(self.api._iter_hardware_output(process, "COM99", INFO))
        self.assertIsNotNone(process.poll())
        self.assertIsNone(self.api._operation_serial_guard)

    def test_arduino_upload_stream_halts_on_loss_without_replay(self):
        self.api._kill_active_process_tree = lambda child=None: API._kill_active_process_tree(self.api, child)
        with patch("main.core.connection_loss.SerialConnectionGuard", side_effect=self.fast_guard):
            with self.assertRaises(OperationConnectionLost):
                arduino_backend._stream(self.api, [sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                                        os.environ.copy(), self.root, upload=True)
        self.assertIsNone(self.api._active_process)
        self.assertEqual(len(self.notices()), 1)
        self.api._start_serial_monitor.assert_not_called()

    def test_compile_does_not_require_mcu_or_network(self):
        self.api.current_port = ""
        with patch("serial.tools.list_ports.comports", side_effect=AssertionError("Compile scanned hardware")):
            arduino_backend._stream(self.api, [sys.executable, "-B", "-c", "print('fixture compiled')"],
                                    os.environ.copy(), self.root)
        self.assertFalse(self.api._operation_connection_loss)

    def test_native_upload_halts_once_and_does_not_resume_monitor(self):
        api = self.api
        api._kill_active_process_tree = lambda child=None: API._kill_active_process_tree(api, child)
        api._resolve_board_info = lambda *_: dict(INFO)
        api.sketch_dir_path = self.root
        api._effective_cache_root = lambda _: self.root
        api._board_workspace_dir = lambda: self.root
        api._generate_platformio_ini = Mock()
        api._needs_recompile = Mock(return_value=(False, "isolated connection fixture"))
        api._get_jobs = lambda: 1
        api._unmap_unc_after_build = Mock()
        command = [sys.executable, "-B", "-c", "import time; time.sleep(30)"]
        with patch.object(web_bridge, "port_occupied_owner", return_value=None), \
             patch.object(web_bridge, "find_pio_executable", return_value=command), \
             patch.object(web_bridge, "_refresh_platformio_core_environment", return_value=(self.root, {})), \
             patch("main.core.connection_loss.SerialConnectionGuard", side_effect=self.fast_guard):
            api._native_upload_worker(can_skip=True)
        self.assertFalse(api.is_busy)
        self.assertIsNone(api._active_process)
        self.assertEqual(len(self.notices()), 1)
        self.assertFalse(any(row.get("type") == "success" for row in self.notices()))
        api._start_serial_monitor.assert_not_called()

    def test_new_operation_clears_only_the_previous_loss(self):
        self.api._operation_connection_loss = "Previous loss"
        self.api._begin_operation_session()
        self.assertEqual(self.api._op_session_id, 2)
        self.assertFalse(self.api._operation_connection_loss)

    def test_controls_retain_only_a_declared_pending_handoff(self):
        class Combo:
            def __init__(self):
                self.rows, self.index = [("COM99", "COM99")], 0
            def currentData(self):
                return self.rows[self.index][0] if self.index >= 0 else None
            def count(self): return len(self.rows)
            def itemData(self, index): return self.rows[index][0]
            def itemText(self, index): return self.rows[index][1]
            def blockSignals(self, _): return False
            def clear(self): self.rows.clear(); self.index = -1
            def addItem(self, text, userData): self.rows.append((userData, text))
            def setCurrentIndex(self, index): self.index = index
            def currentIndex(self): return self.index
            def findData(self, value):
                return next((i for i, row in enumerate(self.rows) if row[0] == value), -1)
        tree = ast.parse((ROOT / "main/qt/toolbar.py").read_text(encoding="utf-8"))
        cls = next(row for row in tree.body if isinstance(row, ast.ClassDef) and row.name == "ControlsBar")
        method = next(row for row in cls.body if isinstance(row, ast.FunctionDef) and row.name == "on_ports_updated")
        method.decorator_list = []
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "isolated_controls", "exec"), namespace)
        controls = SimpleNamespace(port_combo=Combo(), _backend=self.api,
                                   _update_action_button_states_on_controls=Mock())
        guard = self.guard(expected_handoff=True)
        namespace["on_ports_updated"](controls, [])
        self.assertEqual(self.api.current_port, "COM99")
        self.assertEqual(controls.port_combo.currentData(), "COM99")
        self.assertIn("Waiting for USB bootloader", controls.port_combo.rows[0][1])
        guard.close()
        namespace["on_ports_updated"](controls, [])
        self.assertEqual(self.api.current_port, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
