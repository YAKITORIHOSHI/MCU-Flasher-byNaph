"""Upload presentation and baud regressions using fixtures, never hardware."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" and "--render" in sys.argv else "offscreen")
from main.core.target_profile import serial_upload_speed, upload_configuration
from main.core.upload_log import UploadLog
from direct.verify_ubuntu_logging import Collector
from direct.verify_upload_workers import UploadWorkerChecks

UNO = dict(platform="atmelavr", board="uno", framework="arduino",
           upload_protocol="arduino", upload_speed=115200, require_upload_port=True)
AVR_OUTPUT = (
    "DEBUG: Current (avr-stub) External (avr-stub, simavr)", "No dependencies",
    "RAM:   [==        ]  16.5% (used 338 bytes from 2048 bytes)",
    "Flash: [==        ] 16.0% (used 5168 bytes from 32256 bytes)",
    "Configuring upload protocol...", "AVAILABLE: arduino", "CURRENT: upload_protocol = arduino",
    "Using manually specified: COM99", r"Uploading .pio\build\mcu_env\firmware.hex",
    "avrdude: AVR device initialized and ready to accept instructions",
    "Reading | ################################################## | 100% 0.00s",
    "avrdude: Device signature = 0x1e950f",
    'avrdude: reading input file ".pio/build/mcu_env/firmware.hex"',
    "avrdude: writing flash (5168 bytes):",
    "Writing | #########################                          | 50% 0.39s",
    "Writing | ################################################## | 100% 0.78s",
    "avrdude: 5168 bytes of flash written",
    "avrdude: verifying flash memory against .pio/build/mcu_env/firmware.hex:",
    "avrdude: load data flash data from input file .pio/build/mcu_env/firmware.hex:",
    "avrdude: input file .pio/build/mcu_env/firmware.hex contains 5168 bytes",
    "avrdude: reading on-chip flash data:",
    "Reading | ################################################## | 100% 0.60s",
    "avrdude: verifying ...", "avrdude: 5168 bytes of flash verified",
    "avrdude: safemode: Fuses OK (E:00, H:00, L:00)", "avrdude done.  Thank you.",
)


def avr_fixture():
    api = Collector(UNO, "Arduino UNO")
    log = UploadLog(api, UNO, api.current_board, "COM99", "460800")
    log.start()
    for line in AVR_OUTPUT:
        if not log.consume(line):
            api.emit("console:log", {"text": line, "tag": "info", "newline": True})
    log.finish(True, 8.4)
    return api


class UploadPresentationChecks(unittest.TestCase):
    def test_board_defaults_esp_overrides_and_nonserial_transports(self):
        for info, requested, expected in (
            (UNO, "460800", "115200"), (dict(UNO, upload_speed=57600), "921600", "57600"),
            (dict(UNO, platform="espressif32"), "460800", "460800"),
            (dict(UNO, platform="espressif8266"), "999999", "921600"),
            (dict(UNO, require_upload_port=False, upload_protocol="stlink"), "460800", ""),
            (dict(UNO, backend="arduino-cli"), "460800", ""),
            (dict(UNO, upload_speed=None), "460800", ""),
            (dict(UNO, upload_speed="9" * 5000), "460800", ""),
        ):
            with self.subTest(info=info, requested=requested):
                self.assertEqual(serial_upload_speed(info, requested), expected)
        self.assertNotIn("upload_speed", upload_configuration(UNO, "460800"))
        self.assertIn("upload_speed = 460800", upload_configuration(dict(UNO, platform="espressif32"), "460800"))

    def test_avr_stages_have_one_connection_and_actual_board_rate(self):
        api = avr_fixture()
        text = api.text()
        self.assertIn("115200 baud (board default)", text)
        self.assertNotIn("460800", text)
        self.assertEqual(text.count("Connected to Arduino UNO"), 1)
        for noisy in ("DEBUG:", "No dependencies", "AVAILABLE:", "reading input file", "avrdude done"):
            self.assertNotIn(noisy, text)
        summary = dict(api.boxes[-1][1])
        self.assertEqual(summary["Upload Port"], "COM99 @ 115200 baud")
        self.assertEqual(summary["Result"], "Written and verified")
        self.assertEqual(summary["Firmware"], "5168 bytes")
        bars = [row for row in api.logs() if row.get("progress_key", "").endswith(":upload-Writing")]
        self.assertTrue(any("50.0%" in row["text"] for row in bars))
        self.assertTrue(any("100.0%" in row["text"] for row in bars))
        self.assertEqual(len({row["progress_key"] for row in bars}), 1)
        self.assertIn("Verifying", text)

    def test_programmer_reported_rate_updates_summary(self):
        api = Collector(UNO, "Arduino UNO")
        log = UploadLog(api, UNO, api.current_board, "COM99", "460800")
        log.start()
        self.assertTrue(log.consume("Overriding Baud Rate          : 57600"))
        log.finish(True, 1)
        self.assertEqual(dict(api.boxes[-1][1])["Upload Port"], "COM99 @ 57600 baud")
        self.assertIn("baud reported by programmer: 57600", api.text())

    def test_eeprom_counts_do_not_replace_firmware_or_prove_flash_verification(self):
        api = Collector(UNO, "UNO")
        log = UploadLog(api, UNO, "UNO", "COM99", "460800")
        for line in ("avrdude: 5168 bytes of flash written", "avrdude: writing eeprom (16 bytes):",
                     "Writing | ################ | 100% 0.1s", "avrdude: 16 bytes of eeprom written",
                     "avrdude: verifying eeprom memory against image.hex:", "Reading | ################ | 100% 0.1s",
                     "avrdude: 16 bytes of eeprom verified"):
            self.assertTrue(log.consume(line), line)
        log.finish(True, 1)
        summary = dict(api.boxes[-1][1])
        self.assertEqual(summary["Firmware"], "5168 bytes")
        self.assertEqual(summary["Result"], "Uploader completed successfully")
        self.assertIn("Verifying EEPROM", api.text())

    def test_native_stages_never_invent_serial_speed_or_verification(self):
        info = dict(UNO, platform="ststm32", upload_protocol="stlink", require_upload_port=False)
        api = Collector(info, "STM32")
        log = UploadLog(api, info, api.current_board, "COM99", "460800")
        log.start()
        for line in ("** Programming Started **", "** Programming Finished **", "** Verify Started **", "** Verified OK **"):
            self.assertTrue(log.consume(line), line)
        log.finish(True, 2)
        self.assertNotIn("COM99", api.text())
        self.assertNotIn("baud", api.text())
        self.assertEqual(dict(api.boxes[-1][1])["Result"], "Written and verified")

    def test_dfu_and_bossa_progress_remain_stage_specific(self):
        info = dict(platform="atmelsam", board="fixture", upload_protocol="sam-ba")
        api = Collector(info, "Native USB board")
        log = UploadLog(api, info, api.current_board, "COM99", "460800")
        for line in ("Write 1234 bytes to flash", "Writing flash", "[================] 50% (617/1234 bytes)",
                     "Download [================] 100% 1234 bytes", "Verifying flash", "[================] 100%"):
            if line.startswith("Write 1234"):
                self.assertFalse(log.consume(line))  # unknown detail stays visible
            else:
                self.assertTrue(log.consume(line), line)
        self.assertIn("50.0%", api.text())
        self.assertIn("Verifying", api.text())
        log.finish(True, 2)
        self.assertEqual(dict(api.boxes[-1][1])["Result"], "Uploader completed successfully")

    def test_cli_recipe_defaults_and_failure_do_not_claim_baud_or_success(self):
        info = dict(backend="arduino-cli", arduino_fqbn="rp2040:rp2040:rpipico2", upload_speed=115200)
        api = Collector(info, "Raspberry Pi Pico 2 / RP2350")
        log = UploadLog(api, info, api.current_board, "COM99", "460800", backend="Arduino CLI")
        log.start()
        self.assertIn("UPLOADING (Arduino CLI)", api.text())
        self.assertIn("Board recipe default", api.text())
        self.assertNotIn("115200", api.text())
        log.finish(False, 3)
        self.assertNotIn("Upload Summary", api.text())
        self.assertNotIn("Upload successful", api.text())
        self.assertNotIn("verified", api.text())

    def test_unknown_records_and_diagnostics_remain_available(self):
        log = UploadLog(Collector(UNO), UNO, "UNO", "COM99", "460800")
        for line in ("avrdude: error: writing flash failed", "warning: Device signature = 0x000000",
                     "Reading | ERROR | 100%", "avrdude: verification error, first mismatch at byte 0x0000",
                     "custom programmer result Ω", "avrdude: safemode: Fuses changed!"):
            self.assertFalse(log.consume(line), line)

    def test_cli_stream_formats_progress_and_retains_failed_diagnostics(self):
        import io
        from main import web_bridge
        from main.core import arduino_backend
        info = dict(backend="arduino-cli", arduino_fqbn="rp2040:rp2040:rpipico2")
        api = Collector(info, "Raspberry Pi Pico 2 / RP2350")
        api.current_port = api._active_port_label = "COM99"
        api._active_board_info = info
        api._stop_requested = False
        api._kill_active_process_tree = Mock(side_effect=AssertionError("Logging killed a child"))
        process = SimpleNamespace(stdout=io.StringIO("Download [================] 50% 1024 bytes\nwarning: slow USB\nerror: device disconnected\n"),
                                  poll=lambda: 2, wait=lambda: 2)
        guard = SimpleNamespace(poll=lambda: None, wait=lambda: 2, close=lambda: None)
        log = UploadLog(api, info, api.current_board, "COM99", "", backend="Arduino CLI")
        with patch.object(arduino_backend.subprocess, "Popen", return_value=process) as launch, \
                patch("main.core.connection_loss.SerialConnectionGuard", return_value=guard):
            with self.assertRaisesRegex(RuntimeError, "exited with code 2"):
                arduino_backend._stream(api, ["fixture-cli"], {}, ROOT / "temp", upload=True, upload_log=log)
        self.assertEqual(launch.call_count, 1)
        self.assertIn("50.0%", api.text())
        self.assertIn("slow USB", api.text())
        self.assertIn("device disconnected", api.text())
        self.assertIsNone(api._active_process)
        self.assertNotIn("Upload Summary", api.text())

    def test_controls_follow_board_rate_and_preserve_esp_preference(self):
        from PySide6.QtWidgets import QApplication, QComboBox
        from main.core import board_catalog
        global _QT_APP
        _QT_APP = QApplication.instance() or QApplication([])
        tree = ast.parse((ROOT / "main/qt/toolbar.py").read_text(encoding="utf-8"))
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                      and node.name == "_update_hardware_defaults_for_board")
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "isolated_speed_controls", "exec"), namespace)
        combo = QComboBox()
        combo.addItems(["115200", "460800", "921600"])
        backend = SimpleNamespace(upload_speed="460800", set_baud_rate=Mock())
        controls = SimpleNamespace(upload_speed_combo=combo, _backend=backend)
        targets = {"UNO": UNO, "ESP": dict(UNO, platform="espressif32"),
                   "USB programmer": dict(UNO, platform="ststm32", require_upload_port=False, upload_protocol="stlink")}
        with patch.dict(board_catalog.SUPPORTED_BOARDS, targets, clear=True):
            for name, expected, enabled in (("UNO", "115200", False), ("USB programmer", "Auto", False),
                                            ("ESP", "460800", True), ("UNO", "115200", False)):
                namespace["_update_hardware_defaults_for_board"](controls, name, update_monitor=False)
                self.assertEqual(combo.currentText(), expected)
                self.assertEqual(combo.isEnabled(), enabled)
                self.assertEqual(backend.upload_speed, "460800")
        backend.set_baud_rate.assert_not_called()
        combo.deleteLater()

    def test_saved_hardware_details_use_current_target_not_last_esp_upload(self):
        tree = ast.parse((ROOT / "main/web_bridge.py").read_text(encoding="utf-8"))
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                      and node.name == "_sync_project_hardware_state")
        from datetime import datetime
        namespace = dict(Path=Path, datetime=datetime, json=json, sys=type("Host", (), {"platform": "win32"}),
                         DEFAULT_UPLOAD_SPEED="460800", Optional=__import__("typing").Optional)
        exec(compile(ast.Module(body=[method], type_ignores=[]), "isolated_hardware_report", "exec"), namespace)
        api = Mock()
        api.current_board, api.current_port, api.upload_speed = "UNO", "COM99", "460800"
        api.current_baud, api._active_upload_speed = 9600, "921600"
        api._extract_port_device.return_value = "COM99"
        api._resolve_board_info.return_value = UNO
        api._queue_hardware_state_write = Mock()
        namespace["_sync_project_hardware_state"](api, ROOT / "temp/audit/upload-report-fixture")
        payload = json.loads(api._queue_hardware_state_write.call_args.args[2])
        self.assertEqual(payload["hardware"]["upload_speed"], 115200)
        self.assertEqual(payload["hardware"]["baud_rate"], 9600)
        api._resolve_board_info.return_value = dict(UNO, require_upload_port=False)
        namespace["_sync_project_hardware_state"](api, ROOT / "temp/audit/upload-report-fixture")
        self.assertIsNone(json.loads(api._queue_hardware_state_write.call_args.args[2])["hardware"]["upload_speed"])


class WindowsAVRWorkerChecks(UploadWorkerChecks):
    def test_optimized_avr_worker_reports_115200_despite_esp_preference(self):
        self.prepare_upload_caller(fast=False)
        api = self.api
        api.current_board = api._last_compiled_board = "Arduino UNO"
        api.upload_speed = "460800"
        api._resolve_board_info = Mock(return_value=UNO)
        tool = self.root / "fixture-core/packages/tool-avrdude/package.json"
        tool.parent.mkdir(parents=True)
        tool.write_text("{}", encoding="utf-8")
        self.outputs = [(0, "\n".join(AVR_OUTPUT) + "\n")]
        api._upload_worker(can_skip=True)
        self.assertEqual(len(self.processes), 1)
        self.assert_upload_caller_completed(success=True)
        boxes = {call.args[0]: dict(call.args[1]) for call in api._print_info_box.call_args_list}
        self.assertEqual(boxes["Upload Target"]["Upload Speed"], "115200 baud (board default)")
        self.assertEqual(boxes["Upload Summary"]["Upload Port"], "COM99 @ 115200 baud")
        api._locate_soft_reset_fast_binaries.assert_not_called()
        self.assertFalse(any("460800" in row["text"] for row in self.console_messages()))


def render_fixtures():
    """Capture real themed Qt logs and check copied final progress, without services."""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QCoreApplication, QEvent
    from main.core import config
    from main.qt.console_panel import ConsolePanel
    app = QApplication.instance() or QApplication([])
    output = ROOT / "temp/audit/upload-logging"
    output.mkdir(parents=True, exist_ok=True)
    api = avr_fixture()
    with patch.object(config, "load_gui_config", return_value={}), \
            patch.object(config, "_load_raw_config", return_value={"shared": {}, "instances": {}}), \
            patch.object(config, "save_gui_config", side_effect=AssertionError("live settings write")), \
            patch.object(config, "_save_raw_config", side_effect=AssertionError("live settings write")):
        for theme in ("default", "light", "solarized_dark"):
            panel = ConsolePanel()
            panel.resize(1040, 920)
            panel.apply_theme(theme)
            for row in api.logs():
                panel.append_log(row)
            while panel._queue:
                panel._flush_queue()
            panel.show()
            app.processEvents()
            text = panel.get_content_for_clipboard(False)
            assert text.count("Connected to Arduino UNO") == 1
            assert text.count("▰" * 24) >= 2
            assert "50.0%" not in text and "115200 baud" in text and "460800" not in text
            panel.grab().save(str(output / f"avr-{theme}.png"))
            (output / f"avr-{theme}.txt").write_text(text, encoding="utf-8")
            panel.close()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    print(f"Upload log captures: {output}")


if __name__ == "__main__":
    if "--render" in sys.argv:
        sys.argv.remove("--render")
        render_fixtures()
    # Imported worker checks run separately; this suite adds only its AVR case.
    suite = unittest.TestLoader().loadTestsFromTestCase(UploadPresentationChecks)
    suite.addTest(WindowsAVRWorkerChecks("test_optimized_avr_worker_reports_115200_despite_esp_preference"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
