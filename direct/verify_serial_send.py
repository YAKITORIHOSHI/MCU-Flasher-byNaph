"""Serial Send ownership and responsiveness with fake drivers only."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from main import web_bridge
from main.core.serial_send import SerialSendQueue


class FakePort:
    def __init__(self, *, blocked=False, error=None, partial=None):
        self.is_open = True
        self.write_timeout = 1.0
        self.entered = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.writes = []
        self.error, self.partial = error, partial
        self.closes = 0
        self.flush = Mock(side_effect=AssertionError("Serial Send called an unbounded flush"))

    def write(self, data):
        self.entered.set()
        self.release.wait(3.0)
        self.writes.append(data)
        if self.error:
            raise self.error
        return len(data) if self.partial is None else self.partial

    def close(self):
        self.is_open = False
        self.closes += 1
        self.release.set()


class SendChecks(unittest.TestCase):
    def setUp(self):
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        self.api = api
        self.port = FakePort()
        api._serial_lock = threading.Lock()
        api._serial_generation = 1
        api._serial_conn = self.port
        api._serial_send_queue = SerialSendQueue(api)
        api.current_port, api.current_baud = "FIXTURE", 115200
        api.is_busy, api.active_operation = False, None
        api.serial_running = True
        api.emit = Mock()

    def tearDown(self):
        sender = self.api._serial_send_queue
        sender.stop()
        self.port.close()
        if sender.thread:
            sender.thread.join(2.0)
            self.assertFalse(sender.thread.is_alive())

    def idle(self):
        sender = self.api._serial_send_queue
        with sender.condition:
            self.assertTrue(sender.condition.wait_for(lambda: not sender.pending and sender.active is None, 2.0))

    def test_unicode_and_line_endings_written_once_without_flush(self):
        for ending, suffix in (("both", "\r\n"), ("nl", "\n"), ("cr", "\r"), ("none", "")):
            self.assertTrue(self.api.serial_send("héllo 漢字", ending))
            self.idle()
            self.assertEqual(self.port.writes[-1], ("héllo 漢字" + suffix).encode("utf-8"))
        self.assertEqual(len(self.port.writes), 4)
        self.port.flush.assert_not_called()

    def test_blocked_driver_does_not_block_gui_submit_and_queue_is_bounded(self):
        self.port.release.clear()
        started = time.monotonic()
        self.assertTrue(self.api.serial_send("first"))
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(self.port.entered.wait(1.0))
        sender = self.api._serial_send_queue
        for _ in range(sender.MAX_MESSAGES - 1):
            self.assertTrue(self.api.serial_send("pending"))
        self.assertFalse(self.api.serial_send("rejected"))
        self.assertLessEqual(sender.queued_bytes, sender.MAX_QUEUED_BYTES)
        sender.stop()
        self.port.release.set()
        self.idle()
        self.assertEqual(self.port.writes, [b"first\r\n"])

    def test_disconnected_busy_and_connection_lock_reject_immediately(self):
        self.api._serial_conn = None
        self.assertFalse(self.api.serial_send("keep this"))
        self.api._serial_conn = self.port
        self.api.is_busy, self.api.active_operation = True, "flash"
        self.assertFalse(self.api.serial_send("keep this"))
        self.api.is_busy = False
        with self.api._serial_lock:
            started = time.monotonic()
            self.assertFalse(self.api.serial_send("keep this"))
            self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(self.port.writes, [])

    def test_failed_send_discards_pending_and_disconnects_without_retry(self):
        self.port.release.clear()
        self.port.error = OSError("fixture device removed")
        self.assertTrue(self.api.serial_send("first"))
        self.assertTrue(self.port.entered.wait(1.0))
        self.assertTrue(self.api.serial_send("must not replay"))
        self.port.release.set()
        self.idle()
        self.assertEqual(self.port.writes, [b"first\r\n"])
        self.assertIsNone(self.api._serial_conn)
        self.assertEqual(self.api._serial_generation, 2)
        self.assertFalse(self.api.serial_running)
        errors = [call.args[1]["text"] for call in self.api.emit.call_args_list
                  if call.args[0] == "serial:log" and call.args[1].get("tag") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Nothing was retried", errors[0])

    def test_stale_failure_does_not_disconnect_new_connection(self):
        self.port.release.clear()
        self.port.error = OSError("fixture old device removed")
        self.assertTrue(self.api.serial_send("old active"))
        self.assertTrue(self.port.entered.wait(1.0))
        self.assertTrue(self.api.serial_send("old pending"))
        replacement = FakePort()
        with self.api._serial_lock:
            self.api._serial_generation += 1
            self.api._serial_conn = replacement
            self.api.current_port = "NEW FIXTURE"
        self.port.release.set()
        self.idle()
        self.assertIs(self.api._serial_conn, replacement)
        self.assertTrue(replacement.is_open)
        self.assertEqual(replacement.writes, [])
        self.assertEqual(self.port.writes, [b"old active\r\n"])
        self.assertTrue(self.api.serial_running)

    def test_stale_pending_commands_do_not_follow_a_successful_send_to_new_port(self):
        self.port.release.clear()
        self.assertTrue(self.api.serial_send("old active"))
        self.assertTrue(self.port.entered.wait(1.0))
        self.assertTrue(self.api.serial_send("old pending"))
        replacement = FakePort()
        with self.api._serial_lock:
            self.api._serial_generation += 1
            self.api._serial_conn = replacement
            self.api.current_port = "NEW FIXTURE"
        self.port.release.set()
        self.idle()
        self.assertEqual(self.port.writes, [b"old active\r\n"])
        self.assertEqual(replacement.writes, [])
        self.assertTrue(replacement.is_open)

    def test_pending_byte_budget_is_enforced_before_message_count_limit(self):
        self.port.release.clear()
        sender = self.api._serial_send_queue
        command = "x" * (sender.MAX_MESSAGE_BYTES - 2)
        self.assertTrue(self.api.serial_send(command))
        self.assertTrue(self.port.entered.wait(1.0))
        for _ in range(3):
            self.assertTrue(self.api.serial_send(command))
        self.assertEqual(sender.queued_bytes, sender.MAX_QUEUED_BYTES)
        self.assertFalse(self.api.serial_send("rejected"))
        self.assertLess(len(sender.pending), sender.MAX_MESSAGES)
        sender.stop()
        self.port.release.set()
        self.idle()
        self.assertEqual(len(self.port.writes), 1)

    def test_partial_write_is_terminal_and_not_completed_by_a_second_write(self):
        self.port.partial = 2
        self.assertTrue(self.api.serial_send("partial"))
        self.idle()
        self.assertEqual(len(self.port.writes), 1)
        self.assertIsNone(self.api._serial_conn)

    def test_worker_start_failure_and_oversized_input_remain_rejected(self):
        with patch("main.core.serial_send.threading.Thread", side_effect=RuntimeError("fixture thread limit")):
            self.assertFalse(self.api.serial_send("keep this"))
        sender = self.api._serial_send_queue
        self.assertEqual(sender.queued_bytes, 0)
        self.assertFalse(self.api.serial_send("x" * (sender.MAX_MESSAGE_BYTES + 1)))
        self.assertFalse(self.api.serial_send("漢" * sender.MAX_MESSAGE_BYTES))
        self.assertEqual(self.port.writes, [])

    def test_serial_panel_keeps_rejected_input_and_clears_accepted_input(self):
        tree = ast.parse((ROOT / "main/qt/serial_panel.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SerialPanel")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_send_serial")
        method.decorator_list = []
        namespace = {"_LINE_ENDINGS": [("Both", "both")]}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "isolated_send_ui", "exec"), namespace)
        panel = SimpleNamespace(input_field=Mock(text=Mock(return_value="keep this")),
                                line_ending_combo=Mock(currentIndex=Mock(return_value=0)),
                                _backend=Mock(serial_send=Mock(return_value=False)))
        namespace["_send_serial"](panel)
        panel.input_field.clear.assert_not_called()
        panel._backend.serial_send.return_value = True
        namespace["_send_serial"](panel)
        panel.input_field.clear.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
