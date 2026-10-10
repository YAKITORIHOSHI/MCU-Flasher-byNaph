#!/usr/bin/env python3
"""Verify byte decoding and the real serial reader without hardware or stores.

Only serial monitor methods are extracted from the backend AST. Fake ports and
an event collector replace devices, Qt, recovery and notification persistence.
"""
from __future__ import annotations

import ast
from collections import deque
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.core.serial_stream import SerialTextDecoder


def reader_fixture():
    source = ROOT / "main/web_bridge.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    original = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == "MCUWebBackendAPI")
    methods = [node for node in original.body if isinstance(node, ast.FunctionDef)
               and node.name in ("_start_serial_monitor", "_stop_serial_monitor",
                                 "flush_serial_output", "clear_serial_output",
                                 "_queue_serial_notification", "_stop_serial_notifications")]
    fixture = ast.ClassDef(name="ReaderBackend", bases=[], keywords=[], body=methods,
                          decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[fixture], type_ignores=[]))
    namespace = {
        "serial": SimpleNamespace(
            Serial=Mock(), SerialException=OSError,
            EIGHTBITS=8, PARITY_NONE="N", STOPBITS_ONE=1,
        ),
        "threading": threading, "time": time,
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["ReaderBackend"], namespace["serial"]


READER_BACKEND, SERIAL = reader_fixture()
ACTIVE_BACKENDS = []


class FakePort:
    def __init__(self, chunks=(), *, close_when_empty=True):
        self.chunks = deque(chunks)
        self.close_when_empty = close_when_empty
        self.is_open = False
        self.wake = threading.Event()
        self.idle_entered = threading.Event()
        self.read_sizes = []
        self.line_changes = []
        self.input_resets = 0

    def __setattr__(self, name, value):
        if name in ("dtr", "rts") and "line_changes" in self.__dict__:
            self.line_changes.append((name, value))
        object.__setattr__(self, name, value)

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks and isinstance(self.chunks[0], bytes) else 0

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False
        self.wake.set()

    def reset_input_buffer(self):
        self.input_resets += 1

    def read(self, size):
        self.read_sizes.append(size)
        if self.chunks:
            item = self.chunks.popleft()
            if isinstance(item, Exception):
                raise item
            data, tail = item[:size], item[size:]
            if tail:
                self.chunks.appendleft(tail)
            return data
        self.idle_entered.set()
        if self.close_when_empty:
            self.is_open = False
        else:
            self.wake.wait(self.timeout)
        return b""


def backend_fixture(port):
    api = READER_BACKEND()
    api._serial_lock = threading.Lock()
    api._serial_generation = 0
    api._serial_notice_condition = threading.Condition()
    api._serial_notice_pending = None
    api._serial_notice_active = False
    api._serial_notice_thread = None
    api._serial_notice_stopped = False
    api._serial_conn = api._serial_thread = None
    api.current_port, api.current_baud = "SIMULATED", 115200
    api.is_busy, api.active_operation = False, None
    api._schedule_serial_recovery = Mock()
    events = []
    api.emit = lambda name, payload: events.append((name, payload))
    SERIAL.Serial = Mock(return_value=port)
    ACTIVE_BACKENDS.append(api)
    return api, events


def wait_notifications(api, timeout=1):
    with api._serial_notice_condition:
        return api._serial_notice_condition.wait_for(
            lambda: not api._serial_notice_active and api._serial_notice_pending is None,
            timeout=timeout,
        )


def device_text(events):
    return "".join(payload["text"] for name, payload in events
                   if name == "serial:log" and payload.get("stream"))


class DecoderChecks(unittest.TestCase):
    def test_every_byte_boundary_preserves_unicode_and_crlf(self):
        data = "Serial: café 漢字 🙂\r\nsecond\tline\n".encode("utf-8")
        expected = "Serial: café 漢字 🙂\nsecond\tline\n"
        for boundary in range(len(data) + 1):
            decoder = SerialTextDecoder()
            actual = decoder.feed(data[:boundary]) + decoder.feed(data[boundary:], final=True)
            self.assertEqual(actual, expected, boundary)
            self.assertEqual(decoder.invalid_bytes, 0)

    def test_invalid_and_control_bytes_remain_visible_in_copyable_text(self):
        decoder = SerialTextDecoder()
        self.assertEqual(decoder.feed(b"sd\x00\xff\xc0\x08\x7f\rX\n", final=True),
                         "sd\\x00\\xff\\xc0\\x08\\x7f\\x0dX\n")
        self.assertEqual(decoder.invalid_bytes, 2)

    def test_partial_final_character_and_cr_are_not_lost(self):
        decoder = SerialTextDecoder()
        self.assertEqual(decoder.feed(b"prompt \xf0\x9f"), "prompt ")
        self.assertEqual(decoder.feed(b"", final=True), "\\xf0\\x9f")
        self.assertEqual(SerialTextDecoder().feed(b"end\r", final=True), "end\\x0d")

    def test_terminal_sequences_bel_and_unicode_controls(self):
        decoder = SerialTextDecoder()
        text = "\x1b]title\x07\x1b[2J\u0080\u2028\u2029"
        self.assertEqual(decoder.feed(text.encode("utf-8"), final=True),
                         "\x1b]title\x07\x1b[2J\\u0080\\u2028\\u2029")

    def test_newline_free_stream_has_constant_decoder_state(self):
        decoder = SerialTextDecoder()
        for _ in range(1024):
            self.assertEqual(decoder.feed(b"x" * 2048), "x" * 2048)
            self.assertLessEqual(len(decoder._decoder.getstate()[0]), 3)
        self.assertEqual(decoder.feed(b"", final=True), "")


class ReaderChecks(unittest.TestCase):
    def test_serial_write_timeout_is_finite(self):
        port = FakePort()
        api, _events = backend_fixture(port)
        api._start_serial_monitor()
        self.assertEqual(port.write_timeout, 1.0)

    def test_open_uses_explicit_8n1_without_flow_control_and_clears_stale_input(self):
        port = FakePort(close_when_empty=False)
        api, _events = backend_fixture(port)
        try:
            api._start_serial_monitor()
            self.assertEqual(port.baudrate, 115200)
            self.assertEqual(port.bytesize, 8)
            self.assertEqual(port.parity, "N")
            self.assertEqual(port.stopbits, 1)
            self.assertFalse(port.xonxoff)
            self.assertFalse(port.rtscts)
            self.assertFalse(port.dsrdtr)
            self.assertEqual(port.input_resets, 1)
        finally:
            api._stop_serial_monitor()

    def test_status_and_banner_keep_the_baud_used_to_open_this_connection(self):
        api, events = backend_fixture(FakePort(close_when_empty=False))
        port = SERIAL.Serial.return_value
        original_open = port.open

        def open_then_change_requested_baud():
            original_open()
            # A new request can arrive while this connection is opening.  Its
            # status must still report this port's already-configured baud.
            api.current_baud = 57600

        port.open = open_then_change_requested_baud
        try:
            api._start_serial_monitor()
            status = next(payload for name, payload in events if name == "serial:status" and payload["connected"])
            banner = next(payload for name, payload in events if name == "serial:log" and "connected to" in payload["text"])
            self.assertEqual(status["baud"], 115200)
            self.assertIn("@ 115200 baud", banner["text"])
        finally:
            api._stop_serial_monitor()

    def tearDown(self):
        while ACTIVE_BACKENDS:
            api = ACTIVE_BACKENDS.pop()
            notice_thread = api._serial_notice_thread
            api._stop_serial_notifications()
            api._stop_serial_monitor()
            if notice_thread:
                notice_thread.join(1)
                self.assertFalse(notice_thread.is_alive())

    def start_and_finish(self, port):
        api, events = backend_fixture(port)
        api._start_serial_monitor()
        thread = api._serial_thread
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(api.serial_running)
        self.assertTrue(wait_notifications(api))
        return api, events

    def test_partial_prompts_are_delivered_without_newline_or_disconnect(self):
        port = FakePort([b"prompt> "], close_when_empty=False)
        api, events = backend_fixture(port)
        try:
            api._start_serial_monitor()
            limit = time.monotonic() + 0.5
            while not device_text(events) and time.monotonic() < limit:
                time.sleep(0.005)
            self.assertEqual(device_text(events), "prompt> ")
            self.assertTrue(api.serial_running)
        finally:
            api._stop_serial_monitor()

    def test_disconnect_flushes_split_character_and_preserves_generation(self):
        api, events = self.start_and_finish(FakePort([b"Hello \xf0", b"\x9f", b"\x99\x82", b"tail\xe2"]))
        self.assertEqual(device_text(events), "Hello 🙂tail\\xe2")
        self.assertTrue(all(payload.get("generation") == 1 for name, payload in events
                            if name.startswith("serial:")))
        self.assertEqual(len([item for item in events if item[0] == "notification"]), 1)
        self.assertEqual(len([payload for name, payload in events
                              if name == "serial:log" and payload.get("stream_end")]), 1)
        api._schedule_serial_recovery.assert_not_called()

    def test_invalid_bytes_are_warned_once_without_polluting_device_log(self):
        _, events = self.start_and_finish(FakePort([b"sd\xff\x00", b"\xfe\n"]))
        self.assertEqual(device_text(events), "sd\\xff\\x00\\xfe\n")
        notices = [payload for name, payload in events if name == "notification"]
        self.assertEqual(len(notices), 1)
        self.assertIn("binary data", notices[0]["message"])

    def test_high_rate_and_expanded_bytes_have_bounded_lossless_events(self):
        data = b"\xc2" + b"\xff" * 12000 + b"tail"
        port = FakePort([data])
        _, events = self.start_and_finish(port)
        self.assertEqual(device_text(events), "\\xc2" + "\\xff" * 12000 + "tail")
        self.assertLessEqual(max(port.read_sizes), 2048)
        self.assertTrue(all(len(payload["text"]) <= 8192 for name, payload in events
                            if name == "serial:log" and payload.get("stream")))

    def test_per_byte_bursts_are_coalesced(self):
        _, events = self.start_and_finish(FakePort([b"x"] * 1000))
        self.assertEqual(device_text(events), "x" * 1000)
        self.assertLess(len([payload for name, payload in events
                             if name == "serial:log" and payload.get("stream")]), 30)

    def test_explicit_stop_flushes_already_read_bytes_before_generation_change(self):
        port = FakePort([b"prompt\xf0\x9f"], close_when_empty=False)
        api, events = backend_fixture(port)
        api._start_serial_monitor()
        self.assertTrue(port.idle_entered.wait(1))
        api._stop_serial_monitor()
        self.assertEqual(device_text(events), "prompt\\xf0\\x9f")
        statuses = [payload for name, payload in events if name == "serial:status"]
        self.assertEqual(statuses[-1]["generation"], 2)
        self.assertFalse(statuses[-1]["connected"])

    def test_replacement_reader_stays_connected_and_passive(self):
        old_port = FakePort([b"oldtail"], close_when_empty=False)
        new_port = FakePort([b"newtail"], close_when_empty=False)
        api, events = backend_fixture(old_port)
        try:
            api._start_serial_monitor()
            old_thread = api._serial_thread
            self.assertTrue(old_port.idle_entered.wait(1))
            SERIAL.Serial = Mock(return_value=new_port)
            api._start_serial_monitor()
            self.assertTrue(new_port.idle_entered.wait(1))
            old_thread.join(1)
            self.assertFalse(old_thread.is_alive())
            self.assertTrue(api.serial_running)
            self.assertIn("oldtail", device_text(events))
        finally:
            api._stop_serial_monitor()
        self.assertEqual(device_text(events), "oldtailnewtail")
        self.assertTrue(all(not value for port in (old_port, new_port)
                            for _, value in port.line_changes))

    def test_delayed_old_clear_does_not_clear_replacement_connection(self):
        old_port = FakePort([b"old history"], close_when_empty=False)
        new_port = FakePort([b"new history"], close_when_empty=False)
        api, events = backend_fixture(old_port)
        clear_entered, release_clear = threading.Event(), threading.Event()
        clearer = None
        try:
            api._start_serial_monitor()
            self.assertTrue(old_port.idle_entered.wait(1))
            old_flush = api._serial_log_flush

            def delayed_flush(**kwargs):
                clear_entered.set()
                release_clear.wait(1)
                old_flush(**kwargs)

            api._serial_log_flush = delayed_flush
            clearer = threading.Thread(target=api.clear_serial_output, daemon=True)
            clearer.start()
            self.assertTrue(clear_entered.wait(1))
            SERIAL.Serial = Mock(return_value=new_port)
            api._start_serial_monitor()
            self.assertTrue(new_port.idle_entered.wait(1))
            release_clear.set()
            clearer.join(1)
            self.assertFalse(clearer.is_alive())
            # idle_entered marks the next read, before its timeout publishes
            # the coalesced new text. Snapshot those already decoded bytes.
            api.flush_serial_output()
            self.assertFalse(any(name == "serial:clear" for name, _ in events))
            self.assertTrue(api.serial_running)
            self.assertEqual(device_text(events), "old historynew history")
        finally:
            release_clear.set()
            if clearer:
                clearer.join(1)
            api._stop_serial_monitor()

    def test_clear_retires_buffer_without_reset_or_losing_decoder_continuity(self):
        port = FakePort([b"old\xf0\x9f"], close_when_empty=False)
        api, events = backend_fixture(port)
        try:
            api._start_serial_monitor()
            self.assertTrue(port.idle_entered.wait(1))
            changes = list(port.line_changes)
            api.clear_serial_output()
            clear_index = next(index for index, (name, _) in enumerate(events)
                               if name == "serial:clear")
            self.assertEqual(device_text(events[:clear_index]), "old")
            self.assertEqual(port.line_changes, changes)
            self.assertEqual(api._serial_generation, 1)
            self.assertTrue(port.is_open)
            port.chunks.append(b"\x99\x82new")
            limit = time.monotonic() + 0.5
            while not device_text(events[clear_index + 1:]) and time.monotonic() < limit:
                time.sleep(0.005)
            self.assertEqual(device_text(events[clear_index + 1:]), "🙂new")
        finally:
            api._stop_serial_monitor()

    def test_copy_flushes_read_bytes_without_finalizing_character_or_touching_port(self):
        port = FakePort([b"copy prefix\xf0\x9f"], close_when_empty=False)
        api, events = backend_fixture(port)
        try:
            api._start_serial_monitor()
            self.assertTrue(port.idle_entered.wait(1))
            changes = list(port.line_changes)
            self.assertTrue(api.flush_serial_output())
            self.assertEqual(device_text(events), "copy prefix")
            self.assertFalse(any(payload.get("stream_end") for name, payload in events
                                 if name == "serial:log"))
            self.assertFalse(any(name == "serial:clear" for name, _ in events))
            self.assertEqual(port.line_changes, changes)
            self.assertEqual(api._serial_generation, 1)
            self.assertTrue(port.is_open)
            self.assertTrue(api.serial_running)
            port.chunks.append(b"\x99\x82 suffix")
            limit = time.monotonic() + 0.5
            while device_text(events) == "copy prefix" and time.monotonic() < limit:
                time.sleep(0.005)
            self.assertEqual(device_text(events), "copy prefix🙂 suffix")
            self.assertEqual(port.line_changes, changes)
            self.assertFalse(any(name == "notification" for name, _ in events))
        finally:
            api._stop_serial_monitor()

    def test_copy_and_clear_do_not_wait_for_busy_state_lock(self):
        port = FakePort([b"buffered"], close_when_empty=False)
        api, events = backend_fixture(port)
        state_locked = threading.Event()
        release_state = threading.Event()

        def slow_state_operation():
            # Port open/close owns this same lock; Copy/Clear must not join it.
            with api._serial_lock:
                state_locked.set()
                release_state.wait(1)

        holder = threading.Thread(target=slow_state_operation, daemon=True)
        try:
            api._start_serial_monitor()
            self.assertTrue(port.idle_entered.wait(1))
            holder.start()
            self.assertTrue(state_locked.wait(1))
            start = time.monotonic()
            self.assertTrue(api.flush_serial_output())
            self.assertTrue(api.clear_serial_output())
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertEqual(device_text(events), "buffered")
            self.assertTrue(any(name == "serial:clear" for name, _ in events))
            self.assertTrue(port.is_open)
        finally:
            release_state.set()
            holder.join(1)
            api._stop_serial_monitor()

    def test_copy_and_clear_remain_available_during_slow_port_open(self):
        opening = threading.Event()
        release_open = threading.Event()

        class SlowOpenPort(FakePort):
            def open(self):
                opening.set()
                release_open.wait(1)
                super().open()

        port = SlowOpenPort(close_when_empty=False)
        api, events = backend_fixture(port)
        starter = threading.Thread(target=api._start_serial_monitor, daemon=True)
        try:
            starter.start()
            self.assertTrue(opening.wait(1))
            start = time.monotonic()
            self.assertTrue(api.flush_serial_output())
            self.assertTrue(api.clear_serial_output())
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertTrue(any(name == "serial:clear" for name, _ in events))
            self.assertFalse(port.is_open)
        finally:
            release_open.set()
            starter.join(1)
            api._stop_serial_monitor()

    def test_warning_persistence_holds_no_serial_locks_or_blocks_copy_clear(self):
        port = FakePort([b"sd\xff"], close_when_empty=False)
        api, events = backend_fixture(port)
        notification_started = threading.Event()
        release_notification = threading.Event()
        lock_checks = []

        def emit(name, payload):
            if name == "notification":
                def check_from_other_thread():
                    # RLock acquisition must be checked from another thread;
                    # its owning thread can always reacquire it.
                    state_free = api._serial_lock.acquire(blocking=False)
                    log_free = api._serial_log_lock.acquire(blocking=False)
                    lock_checks.append((state_free, log_free))
                    if log_free:
                        api._serial_log_lock.release()
                    if state_free:
                        api._serial_lock.release()

                probe = threading.Thread(target=check_from_other_thread)
                probe.start()
                probe.join(1)
                notification_started.set()
                release_notification.wait(1)
            events.append((name, payload))

        api.emit = emit
        try:
            api._start_serial_monitor()
            self.assertTrue(notification_started.wait(1))
            self.assertEqual(lock_checks, [(True, True)])
            start = time.monotonic()
            self.assertTrue(api.flush_serial_output())
            self.assertTrue(api.clear_serial_output())
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertEqual(device_text(events), "sd\\xff")
            self.assertTrue(port.is_open)
        finally:
            release_notification.set()
            api._stop_serial_monitor()

    def test_eof_warning_is_dispatched_after_state_and_log_locks_release(self):
        port = FakePort([b"tail\xf0"])
        api, events = backend_fixture(port)
        state_checks, log_checks = [], []

        def emit(name, payload):
            if name == "notification":
                state_checks.append(api._serial_lock.locked())
                log_checks.append(api._serial_log_lock._is_owned())
            events.append((name, payload))

        api.emit = emit
        api._start_serial_monitor()
        api._serial_thread.join(1)
        self.assertTrue(wait_notifications(api))
        self.assertEqual(device_text(events), "tail\\xf0")
        self.assertEqual(state_checks, [False])
        self.assertEqual(log_checks, [False])

    def test_blocked_notification_storage_does_not_stop_uart_draining(self):
        port = FakePort([b"sd\xff"], close_when_empty=False)
        api, events = backend_fixture(port)
        api.current_baud = 921600
        notification_started = threading.Event()
        release_notification = threading.Event()
        notification_threads = []

        def emit(name, payload):
            if name == "notification":
                notification_threads.append(threading.current_thread().name)
                notification_started.set()
                release_notification.wait(1)
            events.append((name, payload))

        api.emit = emit
        try:
            api._start_serial_monitor()
            self.assertTrue(notification_started.wait(1))
            latest = b"latest " + b"x" * 64000 + b"\n"
            port.chunks.append(latest)
            limit = time.monotonic() + 0.5
            expected = "sd\\xff" + latest.decode("ascii")
            while device_text(events) != expected and time.monotonic() < limit:
                time.sleep(0.005)
            self.assertEqual(device_text(events), expected)
            self.assertEqual(notification_threads, ["MCU_SerialNotice"])
            self.assertTrue(api.serial_running)
            self.assertFalse(release_notification.is_set())
        finally:
            release_notification.set()
            api._stop_serial_monitor()

    def test_notification_worker_keeps_only_latest_pending_notice(self):
        api, events = backend_fixture(FakePort())
        api._serial_generation = 1
        started = threading.Event()
        release = threading.Event()

        def emit(name, payload):
            if payload.get("message") == "active":
                started.set()
                release.wait(1)
            events.append((name, payload))

        api.emit = emit
        try:
            self.assertTrue(api._queue_serial_notification(1, {"message": "active"}))
            self.assertTrue(started.wait(1))
            worker = api._serial_notice_thread
            for index in range(1000):
                self.assertTrue(api._queue_serial_notification(1, {"message": str(index)}))
                self.assertIs(api._serial_notice_thread, worker)
            with api._serial_notice_condition:
                self.assertTrue(api._serial_notice_active)
                self.assertEqual(api._serial_notice_pending, (1, {"message": "999"}))
            self.assertFalse(api._queue_serial_notification(0, {"message": "stale"}))
            release.set()
            self.assertTrue(wait_notifications(api))
            self.assertEqual([payload["message"] for _, payload in events], ["active", "999"])
            self.assertIs(api._serial_notice_thread, worker)
            self.assertTrue(worker.is_alive())
        finally:
            release.set()

    def test_notification_worker_discards_pending_notice_after_generation_changes(self):
        api, events = backend_fixture(FakePort())
        api._serial_generation = 1
        started = threading.Event()
        release = threading.Event()

        def emit(name, payload):
            if payload.get("message") == "active":
                started.set()
                release.wait(1)
            events.append((name, payload))

        api.emit = emit
        try:
            self.assertTrue(api._queue_serial_notification(1, {"message": "active"}))
            self.assertTrue(started.wait(1))
            self.assertTrue(api._queue_serial_notification(1, {"message": "pending old connection"}))
            api._serial_generation = 2
            release.set()
            self.assertTrue(wait_notifications(api))
            self.assertEqual([payload["message"] for _, payload in events], ["active"])
            self.assertTrue(api._queue_serial_notification(2, {"message": "current"}))
            self.assertTrue(wait_notifications(api))
            self.assertEqual([payload["message"] for _, payload in events], ["active", "current"])
        finally:
            release.set()

    def test_notice_worker_allocation_failure_does_not_fail_reader(self):
        port = FakePort([b"sd\xff" + b"x" * 10000])
        api, events = backend_fixture(port)
        original_thread = threading.Thread

        def thread_factory(*args, **kwargs):
            if kwargs.get("name") == "MCU_SerialNotice":
                raise RuntimeError("no notice worker slot")
            return original_thread(*args, **kwargs)

        with patch.object(threading, "Thread", side_effect=thread_factory):
            api._start_serial_monitor()
            api._serial_thread.join(1)
        self.assertEqual(device_text(events), "sd\\xff" + "x" * 10000)
        self.assertFalse(api.serial_running)
        self.assertIsNone(api._serial_notice_pending)
        self.assertIsNone(api._serial_notice_thread)
        self.assertFalse(any(name == "notification" for name, _ in events))

    def test_notice_worker_start_failure_drops_optional_notice_cleanly(self):
        api, events = backend_fixture(FakePort())
        api._serial_generation = 1
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("no worker slot")):
            self.assertFalse(api._queue_serial_notification(1, {"message": "optional warning"}))
        self.assertIsNone(api._serial_notice_pending)
        self.assertIsNone(api._serial_notice_thread)
        self.assertFalse(api._serial_notice_active)
        self.assertFalse(events)

    def test_notice_shutdown_clears_pending_without_waiting_for_storage(self):
        api, events = backend_fixture(FakePort())
        api._serial_generation = 1
        started = threading.Event()
        release = threading.Event()

        def emit(name, payload):
            started.set()
            release.wait(1)
            events.append((name, payload))

        api.emit = emit
        try:
            self.assertTrue(api._queue_serial_notification(1, {"message": "active"}))
            self.assertTrue(started.wait(1))
            self.assertTrue(api._queue_serial_notification(1, {"message": "pending"}))
            start = time.monotonic()
            api._stop_serial_notifications()
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertIsNone(api._serial_notice_pending)
            self.assertFalse(api._queue_serial_notification(1, {"message": "after stop"}))
            release.set()
            worker = api._serial_notice_thread
            if worker:
                worker.join(1)
                self.assertFalse(worker.is_alive())
            self.assertEqual([payload["message"] for name, payload in events
                              if name == "notification"], ["active"])
        finally:
            release.set()

    def test_reader_error_flushes_output_and_schedules_only_connection_recovery(self):
        api, events = self.start_and_finish(FakePort([b"tail", OSError("removed")]))
        self.assertEqual(device_text(events), "tail")
        api._schedule_serial_recovery.assert_called_once()

    def test_reader_close_failure_preserves_original_error_and_status(self):
        class BrokenClosePort(FakePort):
            def close(self):
                super().close()
                raise RuntimeError("close failed")

        port = BrokenClosePort([b"tail", OSError("device removed")])
        api, events = self.start_and_finish(port)
        self.assertEqual(device_text(events), "tail")
        statuses = [payload for name, payload in events if name == "serial:status"]
        self.assertFalse(statuses[-1]["connected"])
        api._schedule_serial_recovery.assert_called_once()
        self.assertEqual(str(api._schedule_serial_recovery.call_args.args[-1]), "device removed")

    def test_open_and_close_failures_keep_original_open_diagnostic(self):
        class BrokenOpenPort(FakePort):
            def open(self):
                raise OSError("device unavailable")

            def close(self):
                super().close()
                raise RuntimeError("close failed")

        api, events = backend_fixture(BrokenOpenPort())
        api._start_serial_monitor()
        self.assertFalse(api.serial_running)
        self.assertIsNone(api._serial_conn)
        self.assertTrue(any(name == "serial:log" and "device unavailable" in payload["text"]
                            for name, payload in events))
        api._schedule_serial_recovery.assert_called_once()
        self.assertEqual(str(api._schedule_serial_recovery.call_args.args[-1]), "device unavailable")

    def test_thread_start_failure_closes_port_and_reports_error(self):
        port = FakePort(close_when_empty=False)
        api, events = backend_fixture(port)
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("no reader slot")):
            api._start_serial_monitor()
        self.assertFalse(port.is_open)
        self.assertFalse(api.serial_running)
        self.assertIsNone(api._serial_thread)
        self.assertTrue(any(name == "serial:log" and "no reader slot" in payload["text"]
                            for name, payload in events))

    def test_thread_construction_failure_closes_port_and_reports_error(self):
        port = FakePort(close_when_empty=False)
        api, events = backend_fixture(port)
        with patch.object(threading, "Thread", side_effect=RuntimeError("cannot allocate reader")):
            api._start_serial_monitor()
        self.assertFalse(port.is_open)
        self.assertFalse(api.serial_running)
        self.assertIsNone(api._serial_thread)
        self.assertIsNone(api._serial_log_finalize)
        self.assertIsNone(api._serial_log_flush)
        self.assertTrue(any(name == "serial:log" and "cannot allocate reader" in payload["text"]
                            for name, payload in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
