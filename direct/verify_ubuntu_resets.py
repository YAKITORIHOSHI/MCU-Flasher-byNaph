#!/usr/bin/env python3
"""Ubuntu reset tool/gate checks; actual esptool commands inspect local bytes only."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from main.platforms import ubuntu_esptool
from main import web_bridge

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--tool-python", type=Path, default=Path(sys.executable),
                    help="Prepared isolated interpreter for actual hardware-free tool checks")
OPTIONS, UNITTEST_ARGS = parser.parse_known_args()


class ResetChecks(unittest.TestCase):
    def test_versioned_command_names_preserve_v4_and_use_current_v5_syntax(self):
        for version, erase, image in (("4.11.0", "erase_flash", "image_info"),
                                      ("5.0.0", "erase-flash", "image-info"),
                                      ("5.5.0", "erase-flash", "image-info")):
            with patch.object(ubuntu_esptool.metadata, "version", return_value=version):
                self.assertEqual(ubuntu_esptool.subcommand("erase_flash"), erase)
                self.assertEqual(ubuntu_esptool.subcommand("image_info"), image)
                self.assertEqual(ubuntu_esptool.subcommand("write-flash"), "write-flash")

    def test_missing_metadata_preserves_legacy_syntax_without_a_probe(self):
        for error in (ubuntu_esptool.metadata.PackageNotFoundError("esptool"),
                      OSError("Unreadable fixture metadata")):
            with patch.object(ubuntu_esptool.metadata, "version", side_effect=error), \
                    patch.object(subprocess, "Popen", side_effect=AssertionError("Tool launched")):
                self.assertEqual(ubuntu_esptool.subcommand("erase_flash"), "erase_flash")

    def test_native_chip_alias_retains_exact_port_and_connection_arguments(self):
        arguments = dict(serial_list=["/dev/fixture-never-open"],
                         port="/dev/fixture-never-open", connect_attempts=3, initial_baud=115200)
        legacy = Mock(return_value=object())
        modern = Mock(return_value=object())
        v5 = SimpleNamespace(connect_first_available=modern, get_default_connected_device=legacy)
        self.assertIs(ubuntu_esptool.connect_chip(v5, **arguments), modern.return_value)
        modern.assert_called_once_with(**arguments)
        legacy.assert_not_called()
        v4 = SimpleNamespace(get_default_connected_device=legacy)
        self.assertIs(ubuntu_esptool.connect_chip(v4, **arguments), legacy.return_value)
        legacy.assert_called_once_with(**arguments)

    def test_chip_probe_keeps_windows_connector_and_same_native_retry_arguments(self):
        port = "/dev/fixture-never-open"
        arguments = dict(serial_list=[port], port=port, connect_attempts=3, initial_baud=115200)
        for host in ("linux", "win32"):
            esp = Mock(CHIP_NAME="ESP fixture")
            esp.run_stub.return_value = esp
            esp.get_chip_features.return_value = ["fixture"]
            esp.read_mac.return_value = bytes(range(6))
            module = SimpleNamespace(get_default_connected_device=Mock(), connect_first_available=Mock())
            selected = module.connect_first_available if host == "linux" else module.get_default_connected_device
            unused = module.get_default_connected_device if host == "linux" else module.connect_first_available
            selected.side_effect = [RuntimeError("Initial fixture connection refused"), esp]
            api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
            api.emit, api._print_chip_info_box = Mock(), Mock()
            with patch.object(web_bridge.sys, "platform", host), \
                    patch.dict(sys.modules, {"esptool": module}), \
                    patch.object(web_bridge.time, "sleep"):
                self.assertTrue(api._probe_chip_info(port))
            self.assertEqual(selected.call_count, 2)
            for call in selected.call_args_list:
                self.assertEqual(call.kwargs, arguments)
            unused.assert_not_called()
            esp._port.close.assert_called_once_with()

    def test_private_module_is_used_without_path_or_copied_windows_fallback(self):
        with patch.object(web_bridge.sys, "platform", "linux"), \
                patch("src.modules.private_python_guard.is_running_private_python", return_value=True), \
                patch.object(ubuntu_esptool.importlib.util, "find_spec", return_value=object()), \
                patch.object(web_bridge.shutil, "which", side_effect=AssertionError("Foreign PATH tool searched")):
            command = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)._get_esptool_cmd()
        self.assertEqual(command, [sys.executable, "-m", "esptool"])

    def test_missing_dependency_or_foreign_python_stops_before_hardware_or_installers(self):
        with patch("src.modules.private_python_guard.is_running_private_python", return_value=True), \
                patch.object(ubuntu_esptool.importlib.util, "find_spec", return_value=None), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("Tool or installer launched")):
            with self.assertRaisesRegex(RuntimeError, "Bootstrap|bootstrap"):
                ubuntu_esptool.esptool_command()
        with patch("src.modules.private_python_guard.is_running_private_python", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "private Python"):
                ubuntu_esptool.esptool_command()

    def test_windows_resolver_keeps_its_original_import_path(self):
        with patch.object(web_bridge.sys, "platform", "win32"), \
                patch.object(ubuntu_esptool, "esptool_command", side_effect=AssertionError("Linux resolver on Windows")), \
                patch("importlib.util.find_spec", return_value=object()):
            command = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)._get_esptool_cmd()
        self.assertEqual(command, [sys.executable, "-m", "esptool"])

    def backend(self, **target):
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        api.is_busy, api.active_operation = False, None
        api.current_board, api.current_port = "Declared fixture target", "/dev/fixture-never-open"
        api.clear_console_on_action, api.clear_serial_on_action = False, False
        api._check_target = Mock(return_value=True)
        api._resolve_board_info = Mock(return_value=target)
        api._start_reset_worker = Mock(return_value=True)
        api.emit = Mock()
        return api

    def test_reset_gates_declared_framework_and_hard_strategy_before_worker(self):
        with patch.object(web_bridge, "load_gui_config", return_value={}):
            for framework in ("zephyr", "mbed"):
                api = self.backend(platform="ststm32", board="fixture", framework=framework)
                self.assertFalse(api.soft_reset())
                self.assertFalse(api.hard_reset())
                api._start_reset_worker.assert_not_called()
            api = self.backend(platform="ststm32", board="fixture", framework="arduino")
            self.assertTrue(api.soft_reset())
            self.assertFalse(api.hard_reset())
            self.assertEqual(api._start_reset_worker.call_count, 1)
            self.assertEqual(api._start_reset_worker.call_args.args[0], "soft")

    def test_esp_reset_reserves_only_declared_strategy_and_never_starts_in_busy_state(self):
        with patch.object(web_bridge, "load_gui_config", return_value={}):
            api = self.backend(platform="espressif32", board="esp32dev", framework="arduino")
            self.assertTrue(api.soft_reset())
            self.assertTrue(api.hard_reset())
            self.assertEqual([call.args[0] for call in api._start_reset_worker.call_args_list], ["soft", "hard"])
            api._start_reset_worker.reset_mock()
            api.is_busy = True
            api.soft_reset()
            api.hard_reset()
            api._start_reset_worker.assert_not_called()

    def tool(self, *arguments, cwd=None):
        version = subprocess.run(
            [str(OPTIONS.tool_python), "-B", "-W", "error", "-c",
             "from importlib.metadata import version; print(version('esptool'))"],
            capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(version.returncode, 0, version.stderr)
        with patch.object(ubuntu_esptool.metadata, "version", return_value=version.stdout.strip()):
            arguments = [ubuntu_esptool.subcommand(argument) for argument in arguments]
        return subprocess.run([str(OPTIONS.tool_python), "-B", "-W", "error", "-m", "esptool", *arguments],
                              capture_output=True, text=True, timeout=20, cwd=cwd or ROOT, check=False)

    def test_real_tool_accepts_recovery_flags_without_connecting_to_a_port(self):
        # --help exits during parsing, before the serial connection path.
        for before in ("default-reset", "usb-reset"):
            result = self.tool("--chip", "esp32", "--baud", "115200", "--before", before,
                               "--after", "no-reset", "--connect-attempts", "30", "erase_flash", "--help")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Erase", result.stdout)
            self.assertNotIn("Deprecated", result.stdout + result.stderr)

    def test_actual_local_firmware_image_info_verifies_bytes_without_hardware(self):
        audit = ROOT / "temp/audit/ubuntu-parity/features/reset-tool"
        audit.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=audit) as directory:
            firmware = Path(directory) / "fixture.bin"
            source = ("from esptool.bin_image import ESP32FirmwareImage, ImageSegment; "
                      "import sys; image=ESP32FirmwareImage(); image.entrypoint=0x40000000; "
                      "segment=ImageSegment(0x3ffb0000,b'isolated fixture firmware bytes!!'); "
                      "segment.name='fixture'; image.segments=[segment]; image.save(sys.argv[1])")
            built = subprocess.run([str(OPTIONS.tool_python), "-B", "-W", "error", "-c", source, str(firmware)],
                                   capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(built.returncode, 0, built.stderr)
            before = firmware.read_bytes()
            result = self.tool("--chip", "esp32", "image_info", str(firmware))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("Deprecated", result.stdout + result.stderr)
            self.assertIn("ESP32", result.stdout)
            self.assertIn("valid", result.stdout)
            self.assertEqual(firmware.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *UNITTEST_ARGS], verbosity=2)
