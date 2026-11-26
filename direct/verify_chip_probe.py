#!/usr/bin/env python3
"""Every board family gets an Actual Board table at upload, read from the board itself.

Bootloaders here are simulated serial ports; no real port is opened and no board is reset.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main.core import chip_probe
from main.core.upload_log import UploadLog


class Optiboot:
    """An Uno's bootloader answering STK500v1, as avrdude's `arduino` programmer speaks it."""

    def __init__(self, port, signature=b"\x1e\x95\x0f", version=(8, 0), answers=True, **options):
        self.port, self.options, self.signature, self.version, self.answers = port, options, signature, version, answers
        self.inbox, self.outbox, self.dtr, self.rts, self.closed, self.left = b"", b"", True, True, False, False
        self.resets = 0

    def __setattr__(self, name, value):
        if name == "dtr" and value and not getattr(self, "dtr", True):
            object.__setattr__(self, "resets", self.resets + 1)
        object.__setattr__(self, name, value)

    def write(self, data):
        self.inbox += data
        while b" " in self.inbox:
            command, self.inbox = self.inbox.split(b" ", 1)
            if not self.answers or not command:
                continue
            reply = {0x30: b"", 0x50: b"", 0x51: b"", 0x75: self.signature}.get(command[0])
            if command[0] == 0x41:
                reply = bytes([self.version[0] if command[1] == 0x81 else self.version[1]])
            if command[0] == 0x51:
                self.left = True
            if reply is not None:
                self.outbox += b"\x14" + reply + b"\x10"
        return len(data)

    def read(self, count):
        data, self.outbox = self.outbox[:count], self.outbox[count:]
        return data

    def reset_input_buffer(self):
        self.outbox = b""

    def close(self):
        self.closed = True


class MegaLoader(Optiboot):
    """The Mega 2560's stk500boot, answering STK500v2 frames."""

    def write(self, data):
        self.inbox += data
        while len(self.inbox) >= 6:
            size = (self.inbox[2] << 8) | self.inbox[3]
            if len(self.inbox) < 6 + size:
                break
            frame, self.inbox = self.inbox[:6 + size], self.inbox[6 + size:]
            sequence, body = frame[1], frame[5:5 + size]
            if body[0] == 0x01:
                answer = bytes([0x01, 0x00, 8]) + b"AVRISP_2"
            elif body[0] == 0x03:
                answer = bytes([0x03, 0x00, {0x91: 2, 0x92: 10}.get(body[1], 0)])
            elif body[0] == 0x1B:
                answer = bytes([0x1B, 0x00, self.signature[body[4]], 0x00])
            elif body[0] == 0x11:
                self.left = True
                answer = bytes([0x11, 0x00])
            else:
                continue
            reply = bytes([0x1B, sequence, len(answer) >> 8, len(answer) & 0xFF, 0x0E]) + answer
            check = 0
            for byte in reply:
                check ^= byte
            self.outbox += reply + bytes([check])
        return len(data)


def comports(*entries):
    return lambda: [SimpleNamespace(**entry) for entry in entries]


CH340 = {"device": "/dev/ttyFAKE0", "vid": 0x1A86, "pid": 0x7523, "manufacturer": None,
         "product": "USB Serial", "serial_number": None}
UNO_BRIDGE = {"device": "/dev/ttyFAKE0", "vid": 0x2341, "pid": 0x0043, "manufacturer": "Arduino (www.arduino.cc)",
              "product": None, "serial_number": "95735353130351D0A1C1"}


class Probes(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp"
        scratch.mkdir(exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix="chip-probe-", dir=scratch))
        self.addCleanup(shutil.rmtree, self.folder, True)
        manifest = self.folder / "uno.json"
        manifest.write_text(json.dumps({"build": {"f_cpu": "16000000L", "mcu": "atmega328p"},
                                        "upload": {"maximum_size": 32256, "maximum_ram_size": 2048,
                                                   "protocol": "arduino", "speed": 115200}}), encoding="utf-8")
        self.uno = {"platform": "atmelavr", "mcu": "atmega328p", "upload_protocol": "arduino",
                    "upload_speed": 115200, "pio_manifest": str(manifest)}
        self.ports = []

    def factory(self, kind=Optiboot, **options):
        def open_port(port, **settings):
            link = kind(port, **options, **settings)
            self.ports.append(link)
            return link
        return open_port

    def test_an_uno_reports_its_chip_bootloader_bridge_and_specs(self):
        title, fields, warning = chip_probe.board_report("/dev/ttyFAKE0", self.uno, serial_factory=self.factory(),
                                                         comports=comports(CH340))
        table = dict(fields)
        self.assertEqual(title, "Actual Board")
        self.assertEqual(table["Chip"], "ATmega328P")
        self.assertEqual(table["Signature"], "1E 95 0F")
        self.assertEqual(table["Flash"], "32 KB (31.5 KB for sketches)")
        self.assertEqual((table["SRAM"], table["EEPROM"]), ("2 KB", "1 KB"))
        self.assertEqual(table["Clock"], "16 MHz (board setting)")
        self.assertEqual(table["Bootloader"], "STK500v1, version 8.0")
        self.assertEqual(table["USB Bridge"], "WCH CH340 · 1A86:7523")
        self.assertEqual(warning, "")
        link = self.ports[0]
        self.assertEqual(link.options["baudrate"], 115200)
        self.assertTrue(link.closed and link.left, "the sketch is started again and the port released")
        self.assertEqual(link.resets, 1)

    def test_a_genuine_uno_names_its_usb_bridge_and_serial_number(self):
        fields = dict(chip_probe.usb_fields("/dev/ttyFAKE0", comports=comports(UNO_BRIDGE)))
        self.assertEqual(fields["USB Bridge"], "ATmega16U2 (Arduino USB bridge) · 2341:0043")
        self.assertEqual(fields["USB Serial"], "95735353130351D0A1C1")

    def test_a_different_chip_than_selected_is_named(self):
        _title, fields, warning = chip_probe.board_report(
            "/dev/ttyFAKE0", self.uno, serial_factory=self.factory(signature=b"\x1e\x94\x06"), comports=comports())
        self.assertEqual(dict(fields)["Chip"], "ATmega168")
        self.assertIn("Detected ATmega168", warning)

    def test_a_mega_answers_stk500v2(self):
        mega = dict(self.uno, mcu="atmega2560", upload_protocol="wiring", pio_manifest="")
        title, fields, warning = chip_probe.board_report(
            "/dev/ttyFAKE0", mega, serial_factory=self.factory(MegaLoader, signature=b"\x1e\x98\x01"), comports=comports())
        table = dict(fields)
        self.assertEqual((title, table["Chip"], table["Flash"]), ("Actual Board", "ATmega2560", "256 KB"))
        self.assertEqual(table["Bootloader"], "AVRISP_2 (STK500v2), version 2.10")
        self.assertEqual(warning, "")

    def test_a_silent_bootloader_gives_way_to_avrdudes_report_quickly(self):
        import time
        started = time.monotonic()
        title, fields, _warning = chip_probe.board_report(
            "/dev/ttyFAKE0", self.uno, serial_factory=self.factory(answers=False), comports=comports(CH340))
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertIsNone(title, "avrdude's signature fills the table")
        self.assertEqual(dict(fields)["USB Bridge"], "WCH CH340 · 1A86:7523")
        self.assertTrue(self.ports[0].closed)

    def test_other_families_show_their_definition_and_usb_device(self):
        manifest = self.folder / "pico.json"
        manifest.write_text(json.dumps({"build": {"f_cpu": "133000000L", "mcu": "rp2040"},
                                        "upload": {"maximum_size": 2097152, "maximum_ram_size": 262144}}),
                            encoding="utf-8")
        pico = {"platform": "raspberrypi", "mcu": "rp2040", "pio_manifest": str(manifest)}
        title, fields, _warning = chip_probe.board_report(
            "/dev/ttyFAKE0", pico, comports=comports(dict(CH340, vid=0x2E8A, pid=0x000A)))
        table = dict(fields)
        self.assertEqual(title, "Connected Board")
        self.assertEqual(table["Chip"], "RP2040 (board definition)")
        self.assertEqual(table["Clock"], "133 MHz (board definition)")
        self.assertEqual(table["Flash"], "2048 KB (board definition)")
        self.assertEqual(table["USB Bridge"], "RP2040 native USB · 2E8A:000A")
        title, fields, _warning = chip_probe.board_report("", pico, comports=comports())
        self.assertEqual(title, "Board Specs")


class AvrdudeFallback(unittest.TestCase):
    def test_avrdudes_signature_fills_the_actual_board_table_once(self):
        boxes = []
        api = SimpleNamespace(emit=lambda *a, **k: None, _op_session_id=1,
                              _print_info_box=lambda title, fields: boxes.append((title, dict(fields))),
                              _new_upload_progress_state=lambda: {})
        log = UploadLog(api, {"platform": "atmelavr", "mcu": "atmega328p", "upload_protocol": "arduino",
                              "require_upload_port": True}, "Arduino UNO", "/dev/ttyFAKE0", "115200")
        log.chip_extra = [("USB Bridge", "WCH CH340 · 1A86:7523")]
        self.assertTrue(log.consume("avrdude: Device signature = 0x1e950f (probably m328p)"))
        self.assertTrue(log.consume("avrdude: Device signature = 0x1e950f (probably m328p)"))
        self.assertEqual(len(boxes), 1)
        title, table = boxes[0]
        self.assertEqual((title, table["Chip"], table["USB Bridge"]), ("Actual Board", "ATmega328P", "WCH CH340 · 1A86:7523"))

    def test_a_probed_chip_is_not_shown_twice(self):
        boxes = []
        api = SimpleNamespace(emit=lambda *a, **k: None, _op_session_id=1,
                              _print_info_box=lambda title, fields: boxes.append(title),
                              _new_upload_progress_state=lambda: {})
        log = UploadLog(api, {"platform": "atmelavr"}, "Arduino UNO", "/dev/ttyFAKE0", "115200")
        log._chip_shown = True
        log.consume("avrdude: Device signature = 0x1e950f (probably m328p)")
        self.assertEqual(boxes, [])


class Wiring(unittest.TestCase):
    def test_both_upload_paths_read_every_non_esp_board(self):
        text = (ROOT / "main/web_bridge.py").read_text(encoding="utf-8")
        self.assertEqual(text.count("self._probe_board_details(port, "), 2)
        self.assertIn("fields += usb_fields(port)", text, "ESP tables name the USB bridge too")


if __name__ == "__main__":
    unittest.main(verbosity=2)
