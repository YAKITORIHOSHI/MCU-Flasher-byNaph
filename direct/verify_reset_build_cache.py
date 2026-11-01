#!/usr/bin/env python3
"""Exercise shared reset builds in owned fixtures without serial or compilers."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from main import web_bridge
from main.core import compiled_cache

API = web_bridge.MCUWebBackendAPI
BOARDS = {
    "Fixture ESP32": {"platform": "espressif32", "board": "esp32dev", "framework": "arduino",
                      "pio_resolved": True, "backend": "platformio"},
    "Fixture Uno": {"platform": "atmelavr", "board": "uno", "framework": "arduino",
                    "pio_resolved": True, "backend": "platformio"},
}


class ResetBuildCacheChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp/audit/reset-build-cache"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit)))
        self.core = self.root / "core"
        self.core.mkdir()
        self.boot_app = self.core / "boot_app0.bin"
        self.boot_app.write_bytes(b"boot app fixture" * 128)
        self.calls = []
        self.compile_effect = None
        self.stack.enter_context(patch.object(web_bridge, "SCRIPT_DIR", self.root))
        self.stack.enter_context(patch.object(web_bridge, "SUPPORTED_BOARDS", BOARDS))
        self.stack.enter_context(patch.object(web_bridge, "load_gui_config", return_value={}))
        self.stack.enter_context(patch.object(web_bridge, "port_occupied_owner", return_value=None))
        self.stack.enter_context(patch.object(web_bridge, "_try_acquire_reset_cache_lock", return_value=object()))
        self.stack.enter_context(patch.object(web_bridge, "_release_reset_cache_lock"))
        self.stack.enter_context(patch.object(web_bridge, "hide_generated_directory"))
        self.stack.enter_context(patch.object(web_bridge, "find_pio_executable", return_value=["fixture-pio"]))
        self.stack.enter_context(patch.object(web_bridge, "_refresh_platformio_core_environment",
                                             return_value=(self.core, {})))
        self.stack.enter_context(patch.object(web_bridge.time, "sleep"))
        self.stack.enter_context(patch.object(web_bridge.subprocess, "Popen", side_effect=self.process))
        self.stack.enter_context(patch.object(web_bridge.serial, "Serial",
                                             side_effect=AssertionError("Reset fixture opened hardware")))
        self.api = self.new_api("Fixture ESP32")

    def new_api(self, board):
        api = API.__new__(API)
        api.current_board, api.current_port = board, "FIXTURE_PORT"
        api.sketch_dir_path = self.root / "sketch-one"
        api.is_busy, api.active_operation = False, None
        api._board_frameworks = {}
        api.emit = Mock()
        api._check_target = Mock(return_value=True)
        api._check_write_connection = Mock()
        api._start_reset_worker = lambda _kind, worker: worker()
        api._stop_serial_monitor = Mock()
        api._start_serial_monitor = Mock()
        api._trigger_actual_board_reset = Mock()
        api._get_jobs = Mock(return_value=2)
        api._is_native_usb_port = Mock(return_value=False)
        api._locate_esp32_boot_app0 = Mock(return_value=self.boot_app)
        api._soft_reset_esptool_write = Mock(return_value=(True, "", 1))
        api._iter_hardware_output = lambda process, *_, **__: iter(process.stdout.readline, "")
        return api

    def process(self, command, **options):
        workspace = Path(options["cwd"])
        self.assertTrue(workspace.resolve().is_relative_to(self.root.resolve()))
        self.calls.append((list(command), workspace))
        if "nobuild" not in command and "bootloader" not in command:
            output = workspace / ".pio/build/mcu_flash"
            output.mkdir(parents=True, exist_ok=True)
            names = ("firmware.hex",) if self.api.current_board == "Fixture Uno" else (
                "firmware.bin", "bootloader.bin", "partitions.bin")
            for name in names:
                (output / name).write_bytes((name.encode("ascii") + b" fixture\n") * 128)
            if self.compile_effect:
                self.compile_effect(workspace)
        result = Mock(returncode=0, stdout=io.StringIO("[SUCCESS]\n"))
        result.wait.return_value = result.poll.return_value = 0
        return result

    def test_soft_then_hard_and_another_sketch_share_one_exact_reset_build(self):
        self.api.soft_reset()
        self.assertEqual(len(self.calls), 1)
        first_workspace = self.calls[0][1]
        self.assertTrue(self.api._locate_hard_reset_recovery_images()[0])
        other = self.new_api("Fixture ESP32")
        other.sketch_dir_path = self.root / "sketch-two"
        other.soft_reset()
        self.assertEqual(other._soft_reset_project_dir(), first_workspace)
        self.assertEqual(len(self.calls), 1, "Switching sketches rebuilt the shared reset")
        other._soft_reset_esptool_write.assert_called_once()

    def test_hard_reset_preparation_is_reused_by_soft_reset(self):
        images, reason = self.api._build_hard_reset_recovery_images(
            self.api.current_board, self.api._resolve_board_info())
        self.assertIsNotNone(images, reason)
        self.assertEqual(len(self.calls), 1)
        self.api.soft_reset()
        self.assertEqual(len(self.calls), 1)
        self.api._soft_reset_esptool_write.assert_called_once()

    def test_unchanged_template_keeps_source_and_configuration_write_times(self):
        self.api.soft_reset()
        workspace = self.api._soft_reset_project_dir()
        files = [workspace / name for name in ("main.cpp", "platformio.ini")]
        original = [path.stat().st_mtime_ns for path in files]
        self.api._prepare_reset_project(self.api.current_board, self.api._resolve_board_info())
        self.assertEqual(original, [path.stat().st_mtime_ns for path in files])

    def test_reset_firmware_tamper_with_same_timestamp_forces_rebuild(self):
        self.api.soft_reset()
        workspace = self.api._soft_reset_project_dir()
        firmware = workspace / ".pio/build/mcu_flash/firmware.bin"
        before = firmware.stat()
        firmware.write_bytes(b"x" * before.st_size)
        os.utime(firmware, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertFalse(self.api._reset_build_status(workspace, self.api.current_board,
                                                     self.api._resolve_board_info())[0])
        self.api.soft_reset()
        self.assertEqual(len(self.calls), 2)

    def test_failed_flash_preserves_a_successfully_compiled_reset(self):
        self.api._soft_reset_esptool_write.return_value = (False, "fixture connection failure", 1)
        self.api.soft_reset()
        self.assertEqual(len(self.calls), 1)
        self.api._soft_reset_esptool_write.return_value = (True, "", 1)
        self.api.soft_reset()
        self.assertEqual(len(self.calls), 1)

    def test_template_change_during_compilation_never_flashes(self):
        self.compile_effect = lambda workspace: (workspace / "main.cpp").write_text("changed", encoding="utf-8")
        self.api.soft_reset()
        self.api._soft_reset_esptool_write.assert_not_called()
        self.assertIsNone(compiled_cache.read_receipt(self.api._soft_reset_project_dir()))

    def test_reset_allocations_are_separate_for_native_hosts(self):
        with patch.object(compiled_cache, "host_namespace", return_value="windows-x86_64"):
            self.api.soft_reset()
            windows = self.api._soft_reset_project_dir()
        with patch.object(compiled_cache, "host_namespace", return_value="ubuntu-arm64"):
            ubuntu = self.api._soft_reset_project_dir()
            self.assertNotEqual(windows, ubuntu)
            self.api.soft_reset()
        self.assertEqual(len(self.calls), 2)

    def test_cached_avr_soft_reset_uploads_without_compiling(self):
        self.api = self.new_api("Fixture Uno")
        self.api.soft_reset()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("nobuild", self.calls[-1][0])
        self.api.soft_reset()
        self.assertEqual(len(self.calls), 2)
        self.assertIn("nobuild", self.calls[-1][0])
        self.assertEqual(self.calls[0][1], self.calls[1][1])

    def test_avr_hard_reset_uses_dedicated_reset_project(self):
        self.api = self.new_api("Fixture Uno")
        # AVR burn-bootloader is a retained backend strategy, deliberately
        # hidden from the normal UI. Exercise that branch without expanding
        # the application's public reset capability policy.
        capabilities = web_bridge.board_reset_capabilities
        with patch.object(web_bridge, "board_reset_capabilities", side_effect=lambda *args: {
                **capabilities(*args), "hard_reset_ui": True}):
            self.api.hard_reset()
        self.assertEqual(len(self.calls), 1)
        self.assertIn("bootloader", self.calls[0][0])
        self.assertEqual(self.calls[0][1], self.api._soft_reset_project_dir())
        self.assertFalse(self.calls[0][1].is_relative_to(self.api.sketch_dir_path))
        self.assertTrue((self.calls[0][1] / "platformio.ini").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
