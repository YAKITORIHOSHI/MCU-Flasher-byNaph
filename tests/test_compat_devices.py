import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mcu_flash_gui as gui_module

FAKE_BOARDS = {
    "ESP32 Dev Module": {
        "platform": "espressif32", "board": "esp32dev", "framework": "arduino",
        "flash_mb": 4.0, "has_psram": False,
    },
    "ESP32C2 Board": {
        "platform": "espressif32", "board": "esp32c2", "framework": "arduino",
        "flash_mb": 2.0, "has_psram": False,
    },
    "WROVER Board": {
        "platform": "espressif32", "board": "esp32wrover", "framework": "arduino",
        "flash_mb": 4.0, "has_psram": True,
    },
    "Arduino Uno": {
        "platform": "atmelavr", "board": "uno", "framework": "arduino",
        "flash_mb": None, "has_psram": False,
    },
}


def _make_sketch(*contents: str) -> str:
    tmp = tempfile.mkdtemp(prefix="compat_sketch_")
    for i, content in enumerate(contents):
        (Path(tmp) / f"sketch{i}.ino").write_text(content, encoding="utf-8")
    return tmp


class CompatDevicesRulesTests(unittest.TestCase):
    def setUp(self):
        self._patcher = mock.patch.object(gui_module, "SUPPORTED_BOARDS", FAKE_BOARDS)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_no_platform_apis_keeps_all_boards(self):
        sketch = _make_sketch("void setup() {}\nvoid loop() {}\n")
        boards, reasons = gui_module.detect_board_compatibility(Path(sketch))
        self.assertEqual(boards, set(FAKE_BOARDS.keys()))
        self.assertEqual(reasons, [])

    def test_wifi_excludes_uno(self):
        sketch = _make_sketch(
            "#include <WiFi.h>\nvoid setup() {}\nvoid loop() {}\n"
        )
        boards, reasons = gui_module.detect_board_compatibility(Path(sketch))
        self.assertNotIn("Arduino Uno", boards)
        self.assertIn("ESP32 Dev Module", boards)
        self.assertTrue(any("ESP-family" in r for r in reasons))

    def test_ota_header_excludes_small_flash_boards(self):
        sketch = _make_sketch(
            "#include <WiFi.h>\n#include <Update.h>\nvoid setup() {}\nvoid loop() {}\n"
        )
        boards, reasons = gui_module.detect_board_compatibility(Path(sketch))
        self.assertNotIn("ESP32C2 Board", boards)
        self.assertIn("ESP32 Dev Module", boards)
        self.assertIn("WROVER Board", boards)
        self.assertTrue(any("≥4 MB flash" in r for r in reasons))

    def test_psram_usage_is_caution_not_exclusion(self):
        sketch = _make_sketch(
            "#include <WiFi.h>\n"
            "void setup() { char *buf = (char*)ps_malloc(1024); }\nvoid loop() {}\n"
        )
        boards, reasons = gui_module.detect_board_compatibility(Path(sketch))
        self.assertIn("ESP32 Dev Module", boards)
        self.assertIn("ESP32C2 Board", boards)
        self.assertTrue(any(r.startswith("⚠") and "PSRAM" in r for r in reasons))

    def test_board_family_bucketing(self):
        self.assertEqual(gui_module._board_family("LOLIN S3 Mini"), "ESP32-S3")
        self.assertEqual(gui_module._board_family("Heltec WiFi LoRa 32(V3)"), "ESP32")
        self.assertEqual(gui_module._board_family("LOLIN C3 Mini"), "ESP32-C3")
        self.assertEqual(gui_module._board_family("LOLIN C6"), "ESP32-C6")
        self.assertEqual(gui_module._board_family("LOLIN S2 Mini"), "ESP32-S2")
        self.assertEqual(gui_module._board_family("ESP32-C2 DevKit"), "ESP32-C2")
        self.assertEqual(gui_module._board_family("WEMOS D1 R32"), "ESP32")
        self.assertEqual(gui_module._board_family("NodeMCU V3"), "ESP8266")
        self.assertEqual(gui_module._board_family("Arduino Uno"), "Uno")
        self.assertEqual(
            gui_module._format_compat_label({"ESP32 Dev Module", "NodeMCU V3"}),
            "ESP32 and ESP8266",
        )
        self.assertEqual(gui_module._format_compat_label(set()), "Unknown / Incompatible")

    def test_load_dynamic_boards_parses_flash_and_psram(self):
        with tempfile.TemporaryDirectory() as tmp:
            boards_dir = Path(tmp) / "Boards" / "esp32-core-1.0.0"
            boards_dir.mkdir(parents=True)
            boards_dir.joinpath("boards.txt").write_text(
                "# fake core\n"
                "esp32.name=ESP32 Dev Module\n"
                "esp32.build.flash_size=4MB\n"
                "esp32.build.defines=\n"
                "esp32wrover.name=WROVER Board\n"
                "esp32wrover.build.flash_size=4MB\n"
                "esp32wrover.build.defines=-DBOARD_HAS_PSRAM -mfix-esp32-psram-cache-issue\n"
                "esp32c2.name=ESP32C2 Board\n"
                "esp32c2.build.flash_size=2MB\n"
                "esp32s3.menu.PSRAM.disabled=Disabled\n"
                "esp32s3.menu.PSRAM.disabled.build.defines=\n",
                encoding="utf-8",
            )
            with mock.patch.object(gui_module, "_get_download_dir", return_value=tmp):
                loaded = gui_module.load_dynamic_boards({})
        self.assertEqual(loaded["ESP32 Dev Module"]["flash_mb"], 4.0)
        self.assertIs(loaded["ESP32 Dev Module"]["has_psram"], False)
        self.assertEqual(loaded["WROVER Board"]["flash_mb"], 4.0)
        self.assertIs(loaded["WROVER Board"]["has_psram"], True)
        self.assertEqual(loaded["ESP32C2 Board"]["flash_mb"], 2.0)
        self.assertNotIn("ESP32S3 Dev Module", loaded)

    def test_compat_filter_narrows_board_list(self):
        class _Stub:
            def __init__(self):
                self.sketch_dir_path = Path(tempfile.mkdtemp(prefix="compat_stub_"))

        stub = _Stub()
        method = gui_module.MCUUploadGUI._build_compat_content
        lines = method(stub, set(FAKE_BOARDS.keys()), [], filter_text="wrover")
        text = "\n".join(t for t, _ in lines)
        self.assertIn("1 of 4 boards match 'wrover'", text)
        self.assertIn("WROVER Board", text)
        self.assertNotIn("ESP32 Dev Module", text)
        self.assertNotIn("Arduino Uno", text)

        lines = method(stub, set(FAKE_BOARDS.keys()), [], filter_text=None)
        text = "\n".join(t for t, _ in lines)
        self.assertIn("4 of 4 supported boards pass", text)
        self.assertIn("Arduino Uno", text)

        lines = method(stub, set(FAKE_BOARDS.keys()), [], filter_text="nope")
        text = "\n".join(t for t, _ in lines)
        self.assertIn("No boards match 'nope'", text)


if __name__ == "__main__":
    unittest.main()
