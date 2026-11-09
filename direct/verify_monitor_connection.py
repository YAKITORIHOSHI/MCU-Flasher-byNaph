"""Passive tty setup and real backend reset waveforms; no physical hardware."""
from __future__ import annotations
import ast
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.core.monitor_connection import open_passive_serial, idle_after_reset


class Port:
    def __init__(self):
        self.changes = []
        self.is_open = False

    def __setattr__(self, name, value):
        if name in {"dtr", "rts"}:
            self.changes.append((name, value))
        object.__setattr__(self, name, value)

    def open(self):
        assert self.dtr is False and self.rts is False
        assert not self.dsrdtr and not self.rtscts
        self.is_open = True

    def close(self):
        self.is_open = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class MonitorConnections(unittest.TestCase):
    def test_idle_lines_are_configured_before_open(self):
        port = open_passive_serial(Port, "SIMULATED", 115200)
        self.assertEqual(port.changes, [("dtr", False), ("rts", False)])
        port.close()
        self.assertTrue(all(not value for _, value in port.changes))

    def test_failed_intentional_waveform_still_releases_both_lines(self):
        conn = Port()
        with self.assertRaisesRegex(OSError, "waveform failed"):
            with idle_after_reset(conn):
                conn.rts = True
                conn.dtr = True
                raise OSError("waveform failed")
        self.assertFalse(conn.dtr)
        self.assertFalse(conn.rts)

    def test_cleanup_attempts_the_other_line_and_keeps_the_original_error(self):
        class BrokenLine(Port):
            def __setattr__(self, name, value):
                super().__setattr__(name, value)
                if name == "dtr":
                    raise OSError("DTR unavailable")
        conn = BrokenLine()
        with self.assertRaisesRegex(OSError, "original waveform"):
            with idle_after_reset(conn):
                raise OSError("original waveform")
        self.assertIn(("rts", False), conn.changes)

    @unittest.skipUnless(os.name == "posix", "native POSIX tty flags")
    def test_real_owned_pty_disables_hangup_on_open_and_baud_reconfiguration(self):
        import serial
        import termios
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        attrs = termios.tcgetattr(slave)
        attrs[2] |= termios.HUPCL
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        conn = open_passive_serial(serial.Serial, os.ttyname(slave), 115200)
        self.addCleanup(conn.close)
        self.assertFalse(termios.tcgetattr(slave)[2] & termios.HUPCL)
        attrs = termios.tcgetattr(slave)
        attrs[2] |= termios.HUPCL
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        conn.baudrate = 57600
        self.assertFalse(termios.tcgetattr(slave)[2] & termios.HUPCL)
        conn.close()
        self.assertFalse(termios.tcgetattr(slave)[2] & termios.HUPCL)


class IntentionalResets(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "main/web_bridge.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MCUWebBackendAPI")
        methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)]
        for name in ("_require_board_restart", "_trigger_actual_board_reset", "_start_serial_monitor"):
            self.assertEqual(sum(n.name == name for n in methods), 1, "Shadowed serial/reset method")
        cls.body = [n for n in methods if n.name in {"_require_board_restart", "_trigger_actual_board_reset"}]
        cls.bases = []
        self.port = Port()
        self.factory = Mock(return_value=self.port)
        ns = dict(serial=SimpleNamespace(Serial=self.factory),
                  time=SimpleNamespace(sleep=lambda _: None), port_occupied_owner=lambda _: None)
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "backend-reset", "exec"), ns)
        self.api = ns["MCUWebBackendAPI"]()
        self.api.current_board, self.api.current_port = "Fixture", "SIMULATED"
        self.api.emit = Mock()

    def test_actual_production_reset_runs_declared_waveform(self):
        for family, line in (("atmelavr", "dtr"), ("espressif32", "rts"), ("espressif8266", "rts")):
            with self.subTest(family=family):
                self.port.changes.clear()
                self.api._require_board_restart("SIMULATED", "Fixture", {"platform": family})
                asserted = [(name, value) for name, value in self.port.changes if value]
                self.assertEqual(asserted, [(line, True)])
                self.assertFalse(self.port.dtr)
                if line == "rts":
                    self.assertFalse(self.port.rts)
                self.assertEqual(self.port.baudrate, 115200, "Post-write restart touched a 1200-baud bootloader")
                self.assertFalse(self.port.is_open)

    def test_native_programmer_completion_does_not_guess_a_pin_reset(self):
        self.api._require_board_restart("SIMULATED", "Fixture", {"platform": "ststm32"})
        self.factory.assert_not_called()

    def test_reset_failure_cannot_be_reported_as_success(self):
        self.factory.side_effect = OSError("fixture port unavailable")
        with self.assertRaisesRegex(RuntimeError, "Firmware written, but Reset"):
            self.api._require_board_restart("SIMULATED", "Fixture", {"platform": "espressif32"})
        self.assertTrue(self.api._restart_failed)
        self.assertEqual(self.factory.call_count, 1, "A failed reset was replayed")

    def test_changed_target_never_receives_a_stale_reset(self):
        self.api.current_port = "OTHER"
        with self.assertRaisesRegex(RuntimeError, "target changed"):
            self.api._require_board_restart("SIMULATED", "Fixture", {"platform": "espressif32"})
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
