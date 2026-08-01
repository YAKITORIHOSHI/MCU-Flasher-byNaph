import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mcu_flash_gui as gui_module


class _FakeProcess:
    def __init__(self, output: str, returncode: int = 0):
        self.stdout = io.StringIO(output)
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode


class UploadOutputTests(unittest.TestCase):
    def test_esptool_v5_and_v4_progress_parsing(self):
        parsed = gui_module._parse_esptool_write_progress(
            "Writing at 0x0003dd6f [==============>               ] "
            "53.0% 98304/185363 bytes..."
        )
        self.assertEqual(
            parsed,
            {
                "address": "0x0003dd6f",
                "percent": 53.0,
                "written": 98304,
                "total": 185363,
                "version": 5,
            },
        )
        self.assertEqual(
            gui_module._parse_esptool_write_progress(
                "\x1b[32mWriting at 0x00010000... (8 %)\x1b[0m\r"
            ),
            {
                "address": "0x00010000",
                "percent": 8.0,
                "written": None,
                "total": None,
                "version": 4,
            },
        )
        self.assertEqual(
            gui_module._parse_esptool_image_start(
                "Writing 'C:\\Naph's Project\\firmware.bin' at 0x00010000..."
            ),
            {
                "source": "C:\\Naph's Project\\firmware.bin",
                "address": "0x00010000",
            },
        )

    def test_custom_progress_row(self):
        self.assertEqual(
            gui_module._format_upload_progress_row(
                "Firmware", 4, 4, 53.0, 98304, 185363, 30
            ),
            "  ⚡ Flashing [4/4] Firmware "
            "[ ████████████████░░░░░░░░░░░░░░ ] "
            "| 53.0% | 98,304/185,363 bytes",
        )

    def test_boot_app_label_is_not_changed_by_overlapping_address(self):
        app = gui_module.MCUUploadGUI.__new__(gui_module.MCUUploadGUI)
        rendered = []
        app._append_upload_progress = lambda *args, **kwargs: rendered.append((args, kwargs))
        state = {
            "stages": [
                {"key": "bootloader", "label": "Bootloader", "basename": "bootloader.bin"},
                {"key": "partitions", "label": "Partitions", "basename": "partitions.bin"},
                {"key": "boot_app0", "label": "Boot App", "basename": "boot_app0.bin"},
                {"key": "firmware", "label": "Firmware", "basename": "firmware.bin"},
            ],
            "active_index": 0,
            "started": False,
            "last_percent": None,
            "compressed_total": None,
        }
        app._consume_esptool_upload_progress(
            state, "Writing 'C:\\framework\\boot_app0.bin' at 0x0000e000..."
        )
        app._consume_esptool_upload_progress(
            state,
            "Writing at 0x00010000 [==============================] "
            "100.0% 49/49 bytes...",
        )
        self.assertEqual(rendered[-1][0][0], "Boot App")
        self.assertEqual(rendered[-1][0][1:3], (3, 4))

    def test_fast_upload_restores_metadata_and_suppresses_raw_output(self):
        app = gui_module.MCUUploadGUI.__new__(gui_module.MCUUploadGUI)
        events = []
        app.board_var = SimpleNamespace(get=lambda: "ESP32 Dev Module")
        app._stop_requested = False
        app._get_esptool_cmd = lambda: ["python", "-m", "esptool"]
        app._record_fast_upload_diagnostic = lambda *args, **kwargs: None
        app._do_stop = lambda: None
        app._append = lambda text, tag="": events.append(("append", text, tag))
        app._append_connecting_progress = (
            lambda *args, **kwargs: events.append(("connection", args, kwargs))
        )
        app._print_chip_info_box = (
            lambda model, fields: events.append(("chip_box", model, fields))
        )
        app._append_upload_progress = (
            lambda *args, **kwargs: events.append(("progress", args, kwargs))
        )

        metadata = [
            ("  DEBUG: Current (cmsis-dap) External (cmsis-dap, esp-prog)", "dim"),
            ("  RAM:   [=         ]   8.3% (used 27292 bytes from 327680 bytes)", "success"),
            ("  Flash: [=         ]  10.4% (used 328425 bytes from 3145728 bytes)", "success"),
            ("  Configuring upload protocol...", "dim"),
            ("  AVAILABLE: cmsis-dap, esp-prog, espota, esptool", "dim"),
            ("  CURRENT: upload_protocol = esptool", "dim"),
        ]

        def _append_metadata(_fast_bins):
            for text, tag in metadata:
                app._append(text, tag)

        app._append_fast_upload_metadata = _append_metadata

        raw_output = "\n".join([
            "esptool v5.3.1",
            "Serial port COM6:",
            "Connected to ESP32 on COM6:",
            "Chip type:          ESP32-D0WD-V3 (revision v3.1)",
            "Features:           Wi-Fi, BT, Dual Core, 240MHz",
            "Crystal frequency:  40MHz",
            "MAC:                6c:c8:40:34:2c:40",
            "Uploading stub flasher...",
            "Stub flasher running.",
            "Auto-detected flash size: 4MB",
            "Writing 'C:\\project\\.pio\\build\\env\\firmware.bin' at 0x00010000...",
            "Compressed 328784 bytes to 185363...",
            "Writing at 0x00010000 [                              ] 0.0% 0/185363 bytes...",
            "Writing at 0x0003dd6f [==============>               ] 53.0% 98304/185363 bytes...",
            "Writing at 0x00060450 [==============================] 100.0% 185363/185363 bytes...",
            "Wrote 328784 bytes (185363 compressed) at 0x00010000 in 4.8 seconds (549.6 kbit/s).",
            "Verifying written data...",
            "Hash of data verified.",
            "Hard resetting via RTS pin...",
            "",
        ])
        fake_process = _FakeProcess(raw_output)
        fast_bins = {
            "platform": "espressif32",
            "upload_speed": "460800",
            "before": "default-reset",
            "bootloader_addr": "0x1000",
            "bootloader": Path("bootloader.bin"),
            "partitions": Path("partitions.bin"),
            "boot_app0": Path("boot_app0.bin"),
            "firmware": Path("firmware.bin"),
        }

        with mock.patch.object(gui_module.subprocess, "Popen", return_value=fake_process), \
                mock.patch.object(gui_module.time, "time", return_value=100.0), \
                mock.patch.object(gui_module.time, "perf_counter", side_effect=[100.0, 113.2]), \
                mock.patch.object(gui_module.time, "sleep", return_value=None):
            ok, error = app._soft_reset_esptool_write(fast_bins, "COM6")

        self.assertTrue(ok)
        self.assertEqual(error, "")
        kinds = [event[0] for event in events]
        chip_index = kinds.index("chip_box")
        debug_index = next(
            idx for idx, event in enumerate(events)
            if event[0] == "append" and "DEBUG: Current" in event[1]
        )
        progress_index = kinds.index("progress")
        self.assertLess(chip_index, debug_index)
        self.assertLess(debug_index, progress_index)

        progress_events = [event for event in events if event[0] == "progress"]
        self.assertTrue(all(event[1][0] == "Firmware" for event in progress_events))
        self.assertEqual([event[1][3] for event in progress_events[:3]], [0.0, 53.0, 100.0])

        console_text = "\n".join(
            event[1] for event in events if event[0] == "append"
        )
        for forbidden in (
            "esptool v5.3.1",
            "Serial port COM6",
            "Writing '",
            "Writing at 0x",
            "Compressed 328784",
            "Wrote 328784",
            "Verifying written data",
            "Hash of data verified",
            ".pio\\build",
        ):
            self.assertNotIn(forbidden, console_text)
        self.assertEqual(console_text.count("Hard resetting via RTS pin..."), 1)
        self.assertIn(
            "========================= [SUCCESS] Took 13.20 seconds =========================",
            console_text,
        )

    def test_fast_upload_uses_ten_visible_connection_attempts(self):
        app = gui_module.MCUUploadGUI.__new__(gui_module.MCUUploadGUI)
        connection_events = []
        app.board_var = SimpleNamespace(get=lambda: "ESP32 Dev Module")
        app._stop_requested = False
        app._get_esptool_cmd = lambda: ["python", "-m", "esptool"]
        app._record_fast_upload_diagnostic = lambda *args, **kwargs: None
        app._do_stop = lambda: None
        app._append = lambda *args, **kwargs: None
        app._append_fast_upload_metadata = lambda *args, **kwargs: None
        app._print_chip_info_box = lambda *args, **kwargs: None
        app._append_upload_progress = lambda *args, **kwargs: None
        app._append_connecting_progress = (
            lambda *args, **kwargs: connection_events.append((args, kwargs))
        )
        fast_bins = {
            "platform": "espressif32",
            "upload_speed": "460800",
            "before": "default-reset",
            "bootloader_addr": "0x1000",
            "bootloader": Path("bootloader.bin"),
            "partitions": Path("partitions.bin"),
            "boot_app0": Path("boot_app0.bin"),
            "firmware": Path("firmware.bin"),
        }
        failed_attempts = [
            _FakeProcess("Connecting...\nFailed to connect to ESP32\n", 1)
            for _ in range(gui_module.UPLOAD_CONNECTION_ATTEMPTS)
        ]

        with mock.patch.object(gui_module.subprocess, "Popen", side_effect=failed_attempts) as popen, \
                mock.patch.object(gui_module.time, "sleep", return_value=None):
            ok, _error = app._soft_reset_esptool_write(fast_bins, "COM6")

        self.assertFalse(ok)
        self.assertEqual(popen.call_count, gui_module.UPLOAD_CONNECTION_ATTEMPTS)
        self.assertTrue(all(args[1] == gui_module.UPLOAD_CONNECTION_ATTEMPTS
                            for args, _kwargs in connection_events))
        self.assertEqual(connection_events[-1][0][0], gui_module.UPLOAD_CONNECTION_ATTEMPTS)
        for call in popen.call_args_list:
            command = call.args[0]
            attempts_index = command.index("--connect-attempts")
            self.assertEqual(command[attempts_index + 1], "1")


if __name__ == "__main__":
    unittest.main()
