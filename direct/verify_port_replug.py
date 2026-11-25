#!/usr/bin/env python3
"""Unplug and replug the selected port against the real workspace and backend.

A pseudo-terminal stands in for the board: no hardware, live settings, network or
compiler is used. It needs a POSIX host; Windows shares this monitor and reader code,
but its drivers and COM ports remain a physical check.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "direct"))
sys.path.insert(0, str(ROOT))
import verify_ghost_workflow as workflow  # noqa: E402  (applies import-time store guards)
import serial  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from main import web_bridge  # noqa: E402
from main.core import config, monitor_connection, port_inventory  # noqa: E402

del workflow.GhostWorkflowChecks.test_real_monaco_detached_ctrl_s_keeps_models_undo_order_and_selection


class Board:
    """One plugged-in board: a pty whose far end keeps printing."""

    def __init__(self, device: Path):
        self.device = device
        self.master, self.slave = os.openpty()
        device.symlink_to(os.ttyname(self.slave))
        self.alive = True
        self.thread = threading.Thread(target=self._chatter, daemon=True)
        self.thread.start()

    def _chatter(self):
        number = 0
        while self.alive:
            try:
                os.write(self.master, f"boot {number}\n".encode())
            except OSError:
                return
            number += 1
            time.sleep(0.004)

    def unplug(self):
        self.alive = False
        self.thread.join(1)
        self.device.unlink()
        for descriptor in (self.master, self.slave):
            os.close(descriptor)


@unittest.skipIf(os.name == "nt", "needs a pseudo-terminal")
class ReplugChecks(workflow.GhostWorkflowChecks):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory(prefix="port-", dir=workflow.CAPTURES)
        self.addCleanup(folder.cleanup)
        # Port selection treats a device as one word.
        self.device = Path(tempfile.mkdtemp(prefix="mcu-port-")) / "ttyFAKE0"
        self.addCleanup(lambda: self.device.parent.rmdir())
        self.board = None
        self.claims = {"selected_port": ""}
        real_open = monitor_connection.open_passive_serial
        self.opened = []

        def open_fixture(_factory, port, baud, **options):
            connection = real_open(serial.serialposix.Serial, port, baud, **options)
            self.opened.append(connection)
            return connection

        def claim(port):
            self.claims["selected_port"] = port or ""
            return True

        self.stack.enter_context(patch.object(monitor_connection, "open_passive_serial", side_effect=open_fixture))
        for module in (config, web_bridge):
            self.stack.enter_context(patch.object(module, "claim_serial_port", side_effect=claim))
        self.stack.enter_context(patch.object(port_inventory, "signature", side_effect=lambda *_: (
            ((self.device.name, os.lstat(self.device).st_ino),) if os.path.lexists(self.device) else ())))

    def workspace_with_board(self):
        api = self.backend()
        api._scan_ports = lambda: ([{"device": str(self.device), "description": "USB Serial",
                                     "hwid": "USB VID:PID=1A86:7523 LOCATION=1-1"}] if self.board else [])
        window, _page = self.workspace(api)
        monitor = threading.Thread(target=api._port_monitor_loop, daemon=True)
        monitor.start()

        def stop():
            api._stop_port_monitor.set()
            api._stop_serial_monitor()
            monitor.join(3)
            if self.board:
                self.board.unplug()
        self.addCleanup(stop)
        self.board = Board(self.device)
        combo = window._controls_bar.port_combo
        self.until(lambda: combo.findData(str(self.device)) >= 0, "port listed")
        combo.setCurrentIndex(combo.findData(str(self.device)))
        self.until(lambda: api.serial_running, "monitor connected")
        return api, window, combo

    def unplug(self):
        board, self.board = self.board, None
        board.unplug()

    def cycle(self, api, combo, *, between=None):
        self.unplug()
        if between:
            between()
        self.until(lambda: not api.current_port, "drop noticed", 6)
        self.assertEqual(self.claims["selected_port"], str(self.device), "A USB drop must keep this window's claim")
        self.board = Board(self.device)
        if between:
            between()
        self.until(lambda: api.current_port == str(self.device) and api.serial_running, "board reselected", 6)
        self.until(lambda: combo.currentData() == str(self.device), "controls show the returned port")

    def test_replug_restores_the_port_and_never_stalls_the_window(self):
        api, window, combo = self.workspace_with_board()
        beat, worst = [time.monotonic()], [0.0]
        timer = QTimer()

        def tick():
            now = time.monotonic()
            worst[0] = max(worst[0], now - beat[0])
            beat[0] = now
        timer.timeout.connect(tick)
        timer.start(10)
        for _ in range(4):
            beat[0] = time.monotonic()
            self.cycle(api, combo)
        timer.stop()
        self.assertLess(worst[0], 0.25, "Unplugging must not block the interface")

    def test_scan_requests_during_a_drop_cannot_lose_the_port(self):
        """Windows reports each plug event several times. A GUI-thread rescan used to
        consume the change and release the claim, so the port never came back."""
        api, _window, combo = self.workspace_with_board()
        requests = lambda: [QTimer.singleShot(delay, api.request_port_scan) for delay in (0, 80, 250, 400)]
        for _ in range(3):
            self.cycle(api, combo, between=requests)

    def test_open_dropdown_survives_unplug_and_replug(self):
        api, _window, combo = self.workspace_with_board()
        combo.showPopup()
        self.pump()
        self.cycle(api, combo)
        combo.hidePopup()

    def test_a_connection_is_closed_once_and_never_under_its_reader(self):
        api, _window, combo = self.workspace_with_board()
        connection = self.opened[-1]
        seen = []
        real_close = type(connection).close

        def close(conn):
            seen.append(conn._mcu_reader_left.is_set())
            real_close(conn)
        with patch.object(type(connection), "close", close):
            for _ in range(2):
                self.cycle(api, combo)
            api._stop_serial_monitor()
        self.assertTrue(seen and all(seen), "A port was closed while its reader was still inside the driver")
        self.assertEqual(len(seen), len({id(conn) for conn in self.opened}), "Every connection closes exactly once")


class RetirementChecks(unittest.TestCase):
    def connection(self):
        conn = SimpleNamespace(closed=0, cancelled=0)

        def close():
            conn.closed += 1

        def cancel_read():
            conn.cancelled += 1
        conn.close, conn.cancel_read = close, cancel_read
        return conn

    def test_stuck_reader_does_not_block_the_caller_and_still_closes_later(self):
        conn = self.connection()
        left = monitor_connection.track_reader(conn)
        started = time.monotonic()
        self.assertFalse(monitor_connection.retire_connection(conn, timeout=0.2))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual((conn.closed, conn.cancelled), (0, 1), "Never close under the reader")
        left.set()
        self.assertTrue(monitor_connection.retire_connection(conn, timeout=2))
        self.assertEqual(conn.closed, 1)

    def test_concurrent_retirement_closes_exactly_once(self):
        conn = self.connection()
        monitor_connection.track_reader(conn).set()
        threads = [threading.Thread(target=monitor_connection.retire_connection, args=(conn,)) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(conn.closed, 1)

    def test_driver_that_blocks_in_close_is_left_to_its_own_thread(self):
        conn = self.connection()
        release = threading.Event()
        conn.close = lambda: release.wait(5)
        started = time.monotonic()
        self.assertFalse(monitor_connection.retire_connection(conn, timeout=0.2))
        self.assertLess(time.monotonic() - started, 1.0)
        release.set()


if __name__ == "__main__":
    program = unittest.main(verbosity=2, exit=False)
    sys.stdout.flush()
    sys.stderr.flush()
    # The renderer fixtures share the Ghost workflow's native teardown constraints.
    os._exit(0 if program.result.wasSuccessful() else 1)
