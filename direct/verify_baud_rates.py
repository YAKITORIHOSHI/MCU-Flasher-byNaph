#!/usr/bin/env python3
"""Every baud and upload-speed menu offers exactly its list, in MCU Flasher and Lite."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main.core.constants import (  # noqa: E402
    ARDUINO_IDE_BAUD_RATES, DEFAULT_UPLOAD_SPEED, ESP_UPLOAD_SPEEDS, MAX_BAUD_RATE, MAX_UPLOAD_SPEED,
    MONITOR_BAUD_RATES, UNCOMMON_UPLOAD_SPEED, VALID_BAUD_RATES, esp_upload_speed,
)

# arduino/serial-monitor, the monitor the Arduino IDE uses.
IDE = (300, 600, 750, 1200, 2400, 4800, 9600, 19200, 31250, 38400, 57600, 74880,
       115200, 230400, 250000, 460800, 500000, 921600, 1000000, 2000000)
# The user's choice: the IDE's rates and 1500000.
MONITOR = tuple(sorted(IDE + (1500000,)))
# ESP upload speeds (esptool); 2000000 is offered after a question.
UPLOAD = (115200, 230400, 460800, 921600, 1500000, 2000000)


def _numbers(text: str) -> list[int]:
    return [int(value) for value in re.findall(r"\d+", text)]


class BaudMenus(unittest.TestCase):
    def test_the_one_source_is_the_ides_list_and_1500000(self):
        self.assertEqual(ARDUINO_IDE_BAUD_RATES, IDE)
        self.assertEqual(MONITOR_BAUD_RATES, MONITOR)
        self.assertEqual(VALID_BAUD_RATES, set(MONITOR))
        self.assertEqual(MAX_BAUD_RATE, MONITOR[-1])

    def test_main_menus_use_it(self):
        source = (ROOT / "main/qt/serial_panel.py").read_text(encoding="utf-8")
        self.assertIn("_BAUD_RATES = [str(rate) for rate in MONITOR_BAUD_RATES]", source)
        toolbar = (ROOT / "main/qt/toolbar.py").read_text(encoding="utf-8")
        self.assertIn("VALID_BAUD_RATES      = list(MONITOR_BAUD_RATES)", toolbar)
        self.assertIn("UPLOAD_SPEEDS         = list(ESP_UPLOAD_SPEEDS)", toolbar)

    def test_lite_offers_the_same_rates(self):
        rust = (ROOT / "LITE-MODE/src/preferences.rs").read_text(encoding="utf-8")
        block = re.search(r"pub const BAUD_RATES: \[u32; (\d+)\] = \[(.*?)\];", rust, re.S)
        self.assertEqual(tuple(_numbers(block.group(2))), MONITOR)
        self.assertEqual(int(block.group(1)), len(MONITOR))
        speeds = re.search(r"pub const UPLOAD_SPEEDS: \[u32; (\d+)\] = \[(.*?)\];", rust, re.S)
        self.assertEqual(tuple(_numbers(speeds.group(2))), UPLOAD)
        self.assertEqual(int(speeds.group(1)), len(UPLOAD))
        self.assertRegex(rust, rf"pub const UNCOMMON_UPLOAD_SPEED: u32 = {UNCOMMON_UPLOAD_SPEED};")
        worker = (ROOT / "LITE-MODE/python/worker.py").read_text(encoding="utf-8")
        rates = re.search(r"MONITOR_RATES = frozenset\(\{(.*?)\}\)", worker, re.S)
        self.assertEqual(set(_numbers(rates.group(1))), set(MONITOR))
        uploads = re.search(r"UPLOAD_RATES = frozenset\(\{(.*?)\}\)", worker, re.S)
        self.assertEqual(set(_numbers(uploads.group(1))), set(UPLOAD))


class UploadSpeeds(unittest.TestCase):
    def test_the_esp_list_its_cap_and_its_uncommon_speed(self):
        self.assertEqual(ESP_UPLOAD_SPEEDS, UPLOAD)
        self.assertEqual(MAX_UPLOAD_SPEED, UPLOAD[-1])
        self.assertEqual(UNCOMMON_UPLOAD_SPEED, 2000000)
        self.assertIn(DEFAULT_UPLOAD_SPEED, UPLOAD)

    def test_only_a_listed_speed_is_kept(self):
        for value, expected in ((2000000, "2000000"), ("1500000", "1500000"), (" 921600 ", "921600"),
                                ("0115200", "115200"), (512000, "460800"), ("57600", "460800"),
                                ("", "460800"), (None, "460800"), (True, "460800"), (921600.0, "460800"),
                                ("2000000x", "460800"), ("٢٠٠٠٠٠٠", "460800"), ("9" * 5000, "460800")):
            with self.subTest(value=value):
                self.assertEqual(esp_upload_speed(value), expected)
        self.assertEqual(esp_upload_speed(512000, "921600"), "921600")

    def test_the_backend_keeps_its_speed_when_asked_for_an_unlisted_one(self):
        tree = ast.parse((ROOT / "main/web_bridge.py").read_text(encoding="utf-8"))
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                      and node.name == "set_upload_speed")
        namespace = {"esp_upload_speed": esp_upload_speed}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "isolated_upload_speed", "exec"), namespace)

        class Backend:
            upload_speed = "460800"
            active = False

            def is_upload_phase_active(self):
                return self.active

        backend = Backend()
        for requested, expected in ((2000000, "2000000"), ("512000", "2000000"), ("Auto", "2000000"),
                                    ("115200", "115200")):
            namespace["set_upload_speed"](backend, requested)
            self.assertEqual(backend.upload_speed, expected)
        backend.active = True
        namespace["set_upload_speed"](backend, "921600")
        self.assertEqual(backend.upload_speed, "115200", "An upload in progress keeps its speed")

    def test_lite_shows_a_boards_own_speed_and_esp_boards_choose(self):
        sys.path.insert(0, str(ROOT / "LITE-MODE/python"))
        try:
            import worker
        finally:
            sys.path.remove(str(ROOT / "LITE-MODE/python"))
        uno = {"platform": "atmelavr", "board": "uno", "upload_protocol": "arduino", "upload_speed": 115200}
        self.assertEqual(worker.fixed_upload_speed(uno), 115200)
        self.assertIsNone(worker.fixed_upload_speed({}))
        self.assertIsNone(worker.fixed_upload_speed({"platform": "espressif32", "board": "esp32dev"}))
        self.assertIsNone(worker.fixed_upload_speed({"platform": "ESPRESSIF8266", "board": "nodemcuv2"}))
        self.assertEqual(worker.fixed_upload_speed({"platform": "ststm32", "upload_protocol": "stlink"}), 0)
        self.assertEqual(worker.fixed_upload_speed(
            {"platform": "espressif32", "backend": "arduino-cli", "upload_speed": 921600}), 0)

    def test_the_uncommon_speed_question_stays_calm(self):
        for path in ("main/qt/toolbar.py", "LITE-MODE/src/ui.rs"):
            source = (ROOT / path).read_text(encoding="utf-8")
            # The message itself: from its question to the advice that ends it.
            start = source.index("baud for uploads?")
            end = source.index("upload again.", start)
            text = re.sub(r"[\s\\]+", " ", source[start:end]).lower()
            with self.subTest(path=path):
                self.assertIn("might be", text)
                self.assertIn("unstable", text)
                for word in ("danger", "damage", "brick", "harm", "warning", "risk"):
                    self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
