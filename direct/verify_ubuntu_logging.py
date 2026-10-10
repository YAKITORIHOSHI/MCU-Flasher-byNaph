#!/usr/bin/env python3
"""Hardware-free native log presentation checks and optional transcript replay.

Extract only existing pure Windows presentation/parsing helpers from their AST.
Never import the backend, open a port, run a programmer or touch live settings.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
import sys
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.platforms.ubuntu_logs import UbuntuBuildInfo, UbuntuUploadLog


def presentation_fixture():
    namespace = {"re": re, "Path": Path, "textwrap": textwrap}
    catalog = ast.parse((ROOT / "main/core/board_catalog.py").read_text(encoding="utf-8-sig"))
    names = {"_strip_terminal_escapes", "_parse_byte_size", "_parse_esptool_image_start",
             "_parse_esptool_write_progress", "_parse_esptool_compressed", "_parse_esptool_wrote"}
    parsers = [node for node in catalog.body if
               isinstance(node, ast.FunctionDef) and node.name in names or
               isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
                   (target.id.startswith("_ESPTOOL_") or target.id == "_ANSI_ESCAPE_RE")
                   for target in node.targets)]
    source = ast.parse((ROOT / "main/web_bridge.py").read_text(encoding="utf-8-sig"))
    backend = next(node for node in source.body if isinstance(node, ast.ClassDef)
                   and node.name == "MCUWebBackendAPI")
    helper_names = {"_print_info_box", "_print_chip_info_box", "_append_upload_progress",
                    "_new_upload_progress_state", "_select_upload_stage_for_source",
                    "_select_upload_stage_for_address", "_consume_esptool_upload_progress"}
    backend.body = [node for node in backend.body if isinstance(node, ast.FunctionDef)
                    and node.name in helper_names]
    backend.bases = []
    classifier = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_classify_platformio_upload_line")
    exec(compile(ast.Module(body=parsers + [backend, classifier], type_ignores=[]),
                 "isolated_log_presentation", "exec"), namespace)
    return namespace["MCUWebBackendAPI"], namespace["_classify_platformio_upload_line"]


PresentationAPI, CLASSIFY = presentation_fixture()


class Collector(PresentationAPI):
    def __init__(self, info=None, name="Fixture ESP32 Dev Module"):
        self.current_board = name
        self.info = info or {"platform": "espressif32", "board": "esp32dev", "framework": "arduino"}
        self._resolve_board_info = lambda *_: self.info
        self.events = []
        self.boxes = []

    def emit(self, event, payload):
        self.events.append((event, payload))

    def _print_info_box(self, title, fields):
        self.boxes.append((title, fields))
        super()._print_info_box(title, fields)

    def logs(self):
        return [payload for event, payload in self.events if event == "console:log"]

    def text(self):
        return "\n".join(payload["text"] for payload in self.logs())


class UbuntuLoggingChecks(unittest.TestCase):
    def test_upload_header_matches_windows_text_and_tags(self):
        api = Collector()
        log = UbuntuUploadLog(api, api.info, api.current_board, "/dev/fixture0", "921600")
        log.start()
        logs = api.logs()
        self.assertEqual([row["text"] for row in logs[:4]],
                         ["", "=" * 50, "  ⬆  UPLOADING (PlatformIO)", "=" * 50])
        self.assertEqual(logs[2]["tag"], "header")
        details = dict(api.boxes[0][1])
        self.assertEqual(api.boxes[0][0], "Upload Target")
        self.assertEqual(details["Port"], "/dev/fixture0")
        self.assertEqual(details["Board"], "Fixture ESP32 Dev Module")
        self.assertEqual(details["Upload Speed"], "921600 baud (selected)")
        log.start()
        self.assertEqual(len(api.logs()), len(logs))

    def test_actual_chip_details_precede_shared_windows_progress_and_summary(self):
        api = Collector()
        log = UbuntuUploadLog(api, api.info, api.current_board, "/dev/fixture0", "921600")
        log.start()
        lines = (
            "Connecting....", "Chip is ESP32-D0WD-V3 (revision v3.1)",
            "Features: WiFi, BT, Dual Core, 240MHz", "Crystal is 40MHz",
            "MAC: 00:11:22:33:44:55", "Uploading stub...", "Running stub...", "Stub running...",
            "Flash will be erased from 0x00001000 to 0x00005fff...",
            "Compressed 17536 bytes to 12202...", "Writing at 0x00001000... (100 %)",
            "Wrote 17536 bytes (12202 compressed) at 0x00001000 in 0.3 seconds...",
            "Hash of data verified.", "Writing at 0x00010000... (12 %)",
            "Hard resetting via RTS pin...",
        )
        for line in lines:
            self.assertTrue(log.consume(line), line)
        log.finish(True, 4.12)
        chip_boxes = [fields for title, fields in api.boxes if title.endswith(" Information")]
        self.assertEqual(len(chip_boxes), 1)
        self.assertEqual(dict(chip_boxes[0])["MAC Address"], "00:11:22:33:44:55")
        self.assertNotIn("Flash Size", dict(chip_boxes[0]))
        text = api.text()
        self.assertLess(text.index("ESP32-D0WD-V3 (revision v3.1) Information"), text.index("Flashing"))
        self.assertIn("[1/4] Bootloader", text)
        self.assertIn("[4/4] Firmware", text)
        self.assertIn("Upload Summary", text)
        self.assertIn("/dev/fixture0 @ 921600 baud", text)
        self.assertIn("4.12s", text)
        self.assertIn("Flash data verified", text)
        connection = [row for row in api.logs() if row.get("progress_key")]
        self.assertEqual(len({row["progress_key"] for row in connection}), 1)
        self.assertIn("Connected", connection[-1]["text"])
        count = len(api.events)
        log.finish(True, 9.0)
        self.assertEqual(count, len(api.events))

    def test_v5_and_late_detected_flash_details_are_retained(self):
        api = Collector()
        log = UbuntuUploadLog(api, api.info, api.current_board, "/dev/fixture0", "921600")
        for line in ("Connected to ESP32 on /dev/fixture0:", "Chip type: ESP32-D0WD-V3 (revision v3.1)",
                     "Crystal frequency: 40MHz", "MAC address: 00:11:22:33:44:55",
                     "Stub flasher running.", "Detected flash size: 4MB", "Changing baud rate to 921600..."):
            self.assertTrue(log.consume(line), line)
        text = api.text()
        self.assertIn("Crystal", text)
        self.assertIn("40MHz", text)
        self.assertIn("Flash Size: 4MB", text)
        self.assertIn("Changing baud rate to 921600", text)

    def test_failed_or_stopped_upload_never_reports_success_or_infers_chip(self):
        for stopped in (False, True):
            with self.subTest(stopped=stopped):
                api = Collector()
                log = UbuntuUploadLog(api, api.info, api.current_board, "/dev/fixture0", "921600")
                log.start()
                log.consume("Connecting....")
                self.assertFalse(log.consume("A fatal error occurred: Failed to connect to ESP32"))
                log.finish(False, 5.0, stopped=stopped)
                self.assertNotIn("Upload successful", api.text())
                self.assertNotIn("Upload Summary", api.text())
                self.assertEqual([title for title, _ in api.boxes], ["Upload Target"])
                self.assertIn("Connection stopped" if stopped else "Connection failed", api.text())

    def test_v5_image_stage_remains_locked_as_the_write_pointer_advances(self):
        api = Collector()
        log = UbuntuUploadLog(api, api.info, api.current_board, "/dev/fixture0", "921600")
        for line in ("Connected to ESP32", "Writing 'boot_app0.bin' at 0x0000e000...",
                     "Writing at 0x00010000 100.0% 8192/8192 bytes"):
            self.assertTrue(log.consume(line), line)
        self.assertIn("[3/4] Boot App", api.text())
        self.assertNotIn("[4/4] Firmware", api.text())
        self.assertIn("ESP32 Information", api.text())

    def test_native_nonserial_programmer_does_not_claim_port_speed_or_esp_chip(self):
        info = {"platform": "ststm32", "board": "fixture", "framework": "cmsis",
                "upload_protocol": "stlink", "require_upload_port": False}
        api = Collector(info, "Fixture STM32")
        log = UbuntuUploadLog(api, info, api.current_board, "", "921600")
        log.start()
        for line in ("ST-LINK serial 123456", "Chip is custom board"):
            self.assertFalse(log.consume(line), line)
        self.assertTrue(log.consume("Writing flash"))
        log.finish(True, 1.1)
        text = api.text()
        self.assertIn("stlink", text)
        self.assertNotIn("baud", text)
        self.assertNotIn("Upload Speed", text)
        self.assertNotIn("Upload Port", text)
        self.assertNotIn("Information", text)

    def test_diagnostics_and_unknown_output_are_never_swallowed(self):
        api = Collector()
        log = UbuntuUploadLog(api, api.info, api.current_board, "/dev/fixture0", "921600")
        for line in ("Features: error: unavailable", "warning: programmer firmware is outdated",
                     "Timed out waiting for packet header", "Permission denied: /dev/fixture0",
                     "custom programmer result Ω", "Connecting custom transport"):
            self.assertFalse(log.consume(line), line)

    def test_non_esp_serial_speed_comes_from_the_board_manifest(self):
        info = {"platform": "atmelavr", "board": "uno", "framework": "arduino", "upload_speed": 115200}
        api = Collector(info, "Fixture Uno")
        log = UbuntuUploadLog(api, info, api.current_board, "/dev/fixture0", "921600")
        log.start()
        log.finish(True, 1.0)
        self.assertIn("Upload Speed : 115200", api.text())
        self.assertIn("115200 baud", api.text())
        self.assertNotIn("921600", api.text())

    def test_board_info_is_actual_bounded_metadata_and_boxed_once(self):
        api = Collector()
        log = UbuntuBuildInfo(api, api.info, api.current_board)
        for line in ("PLATFORM: Espressif 32 (6.12.0) > Espressif ESP32 Dev Module",
                     "HARDWARE: ESP32 240MHz, 320KB RAM, 4MB Flash", "PACKAGES:"):
            self.assertTrue(log.consume(line), line)
        for i in range(100):
            self.assertTrue(log.consume(f" - tool-fixture-{i} @ 1.0.0"))
        self.assertFalse(log.consume("Building in release mode..."))
        log.flush()
        self.assertEqual(len(api.boxes), 1)
        details = dict(api.boxes[0][1])
        self.assertEqual(details["Target"], "espressif32:esp32dev")
        self.assertEqual(details["Hardware"], "ESP32 240MHz, 320KB RAM, 4MB Flash")
        self.assertLessEqual(len(log.packages), 24)
        self.assertLessEqual(len(details["Packages"].encode("utf-8")), 4096)
        self.assertIn("Package details", details)

    def test_custom_metadata_and_diagnostics_are_not_summarized_as_standard_records(self):
        api = Collector()
        log = UbuntuBuildInfo(api, api.info, api.current_board)
        for line in ("PLATFORM: custom transport message", "HARDWARE: custom device message",
                     "PLATFORM: Error: platform package is missing", " - tool-fixture @ warning: missing"):
            self.assertFalse(log.consume(line), line)


def replay(path: Path, output: Path):
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    marker = next(i for i, line in enumerate(lines) if line.startswith("Uploading through the selected board"))
    board = next(re.match(r"\s*Board\s+:\s*(.+)", line).group(1) for line in lines
                 if re.match(r"\s*Board\s+:\s*(.+)", line))
    target = next(re.search(r"platform: ([^;]+); board: ([^;]+); framework: ([^)]+)", line)
                  for line in lines if "Processing mcu_env (platform:" in line)
    info = dict(zip(("platform", "board", "framework"), target.groups()))
    port = next(line.split("Using manually specified: ", 1)[1] for line in lines if "Using manually specified: " in line)
    speed = next(re.search(r"Changing baud rate to (\d+)", line).group(1) for line in lines
                 if re.search(r"Changing baud rate to (\d+)", line))
    api = Collector(info, board)
    log = UbuntuUploadLog(api, info, board, port, speed)
    log.start()
    successful = False
    duration = 0.0
    for line in lines[marker + 1:]:
        if not line or log.consume(line):
            continue
        action, verdict = CLASSIFY(line)
        if action == "suppress":
            continue
        if action == "outcome":
            successful = verdict == "SUCCESS"
            duration = float(re.search(r"Took\s+([\d.]+)\s+seconds", line).group(1))
        api.emit("console:log", {"text": line, "tag": "success" if verdict == "SUCCESS" else "info", "newline": True})
    log.finish(successful, duration)
    if not output.resolve().is_relative_to((ROOT / "temp").resolve()):
        raise ValueError("Replay output must be inside the project's temp/ directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("Transcript replay only; no programmer or hardware was invoked.\n\n" + api.text() + "\n", encoding="utf-8")
    print(f"Replayed upload transcript: {output}")
    return api


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-log", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "temp/audit/ubuntu-logging/replayed-upload.txt")
    args, extra = parser.parse_known_args()
    if args.replay_log:
        replay(args.replay_log, args.output)
    unittest.main(argv=[sys.argv[0], *extra], verbosity=2)
