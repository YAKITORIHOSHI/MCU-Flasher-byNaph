#!/usr/bin/env python3
"""Every baud menu offers exactly the Arduino IDE Serial Monitor's rates, no more, no fewer."""
from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main.core.constants import ARDUINO_IDE_BAUD_RATES, MAX_UPLOAD_SPEED, VALID_BAUD_RATES

# arduino/serial-monitor, the monitor the Arduino IDE uses.
IDE = (300, 600, 750, 1200, 2400, 4800, 9600, 19200, 31250, 38400, 57600, 74880,
       115200, 230400, 250000, 460800, 500000, 921600, 1000000, 2000000)


def _numbers(text: str) -> list[int]:
    return [int(value) for value in re.findall(r"\d+", text)]


class BaudMenus(unittest.TestCase):
    def test_the_one_source_is_the_arduino_ides_list(self):
        self.assertEqual(ARDUINO_IDE_BAUD_RATES, IDE)
        self.assertEqual(VALID_BAUD_RATES, set(IDE))
        self.assertEqual(MAX_UPLOAD_SPEED, 921600)
        from main.core.constants import MAX_BAUD_RATE
        self.assertEqual(MAX_BAUD_RATE, IDE[-1])

    def test_main_menus_use_it(self):
        source = (ROOT / "main/qt/serial_panel.py").read_text(encoding="utf-8")
        self.assertIn("_BAUD_RATES = [str(rate) for rate in ARDUINO_IDE_BAUD_RATES]", source)
        toolbar = (ROOT / "main/qt/toolbar.py").read_text(encoding="utf-8")
        self.assertIn("VALID_BAUD_RATES      = list(ARDUINO_IDE_BAUD_RATES)", toolbar)

    def test_lite_offers_the_same_rates(self):
        rust = (ROOT / "LITE-MODE/src/preferences.rs").read_text(encoding="utf-8")
        block = re.search(r"pub const BAUD_RATES: \[u32; (\d+)\] = \[(.*?)\];", rust, re.S)
        self.assertEqual(tuple(_numbers(block.group(2))), IDE)
        self.assertEqual(int(block.group(1)), len(IDE))
        worker = (ROOT / "LITE-MODE/python/worker.py").read_text(encoding="utf-8")
        rates = re.search(r"MONITOR_RATES = frozenset\(\{(.*?)\}\)", worker, re.S)
        self.assertEqual(set(_numbers(rates.group(1))), set(IDE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
