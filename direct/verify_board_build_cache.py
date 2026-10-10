#!/usr/bin/env python3
"""Verify retained exact-board builds using disposable, hardware-free fixtures.

The real compile worker stages fixture sources and launches a mocked compiler
that writes small images only into its captured working directory. Persistence,
package checks, GUI services and hardware access stay isolated from live state.
"""
from __future__ import annotations

import io
import json
import os
import stat
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
AVR = "Fixture Uno"
ESP = "Fixture ESP32"
BOARDS = {
    AVR: {"platform": "atmelavr", "board": "uno", "framework": "arduino",
          "frameworks": ["arduino"], "pio_resolved": True, "backend": "platformio"},
    ESP: {"platform": "espressif32", "board": "esp32dev", "framework": "arduino",
          "frameworks": ["arduino", "espidf"], "pio_resolved": True, "backend": "platformio"},
}


class FixtureCompiler:
    """Finite output and an exit code, with no child process or serial port."""

    def __init__(self, returncode: int):
        self.returncode = returncode
        self.stdout = io.StringIO("RAM: [====] 12.0%\nFlash: [====] 23.0%\n"
                                 + ("[SUCCESS]\n" if returncode == 0 else "error: fixture failure\n"))

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -1


class BoardBuildCacheChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp" / "audit" / "board-build-cache"
        audit.mkdir(parents=True, exist_ok=True)
        self.sandbox = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit)))
        self.project = self.sandbox / "project"
        self.project.mkdir()
        self.source = self.project / "probe.cpp"
        self.source.write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
        self.cache = self.sandbox / "cache"
        self.cache.mkdir()
        self.core = self.sandbox / "core"
        self.core.mkdir()
        self.library = self.sandbox / "libraries"
        self.calls = []
        self.returncode = 0
        self.compiler_effect = None
        self.stack.enter_context(patch.object(web_bridge, "SUPPORTED_BOARDS", BOARDS))
        self.stack.enter_context(patch.object(web_bridge, "load_gui_config", return_value={}))
        self.stack.enter_context(patch.object(web_bridge, "get_project_remembered_board", return_value=""))
        self.remember = self.stack.enter_context(patch.object(web_bridge, "set_project_remembered_board"))
        self.stack.enter_context(patch.object(web_bridge, "_refresh_platformio_core_environment",
                                             return_value=(self.core, {})))
        self.stack.enter_context(patch.object(web_bridge, "find_pio_executable", return_value=["fixture-pio"]))
        self.stack.enter_context(patch.object(web_bridge, "board_toolchain_ready", return_value=True))
        self.stack.enter_context(patch.object(web_bridge, "ensure_scons_ready", return_value=True))
        self.stack.enter_context(patch.object(web_bridge, "is_unc_or_network_path", return_value=False))
        self.stack.enter_context(patch.object(web_bridge, "hide_generated_directory"))
        self.stack.enter_context(patch.object(web_bridge, "_analyze_gpio_compatibility", return_value={"excluded": set()}))
        self.stack.enter_context(patch.object(web_bridge, "detect_board_compatibility", return_value=([], {})))
        self.stack.enter_context(patch.object(web_bridge, "classify_platformio_failure", return_value="code"))
        self.stack.enter_context(patch.object(web_bridge, "_iter_process_output",
                                             side_effect=lambda process, *_args, **_options: iter(process.stdout)))
        self.stack.enter_context(patch.object(web_bridge.subprocess, "Popen", side_effect=self.compile_fixture))
        # An accidental hardware call should fail the verifier immediately.
        self.stack.enter_context(patch.object(web_bridge.serial, "Serial",
                                             side_effect=AssertionError("Fixture opened a serial port")))
        self.api = self.new_api()

    def new_api(self):
        api = API.__new__(API)
        api.sketch_dir_path = self.project
        api.current_board, api.current_port = AVR, ""
        api.current_baud, api.upload_speed = 115200, "460800"
        api._board_frameworks = {}
        api.modified_files = {}
        api._fixture_libraries = self.library
        api._fixture_direct_library = False
        api._build_metadata_by_board = {}
        api._last_source_hash = api._last_compiled_board = ""
        api._active_process = None
        api._stop_requested = False
        api.is_busy = False
        api.active_operation = api._current_op_phase = None
        api.emit = Mock()
        api._effective_cache_root = lambda *_args: self.cache
        api._validate_entry_points = Mock(return_value=(True, "probe.cpp"))
        api._get_jobs = Mock(return_value=2)
        api._generate_platformio_ini = lambda workspace: self.generate_ini(api, workspace)
        api._refresh_compatible_devices = Mock()
        api._print_info_box = Mock()
        api.update_skip_compile_availability = Mock()
        api._unmap_unc_after_build = Mock()
        return api

    @staticmethod
    def generate_ini(api, workspace):
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        info = api._resolve_board_info()
        libraries = ""
        if api._fixture_libraries.is_dir():
            libraries = (f"lib_deps =\n    symlink://{api._fixture_libraries.as_posix()}\n"
                         if api._fixture_direct_library else
                         f"lib_extra_dirs = {api._fixture_libraries.as_posix()}\n")
        (workspace / "platformio.ini").write_text(
            "[platformio]\ndefault_envs = mcu_env\n[env:mcu_env]\n"
            f"platform = {info['platform']}\nboard = {info['board']}\n"
            f"framework = {info['framework']}\nmonitor_speed = 115200\nupload_speed = 460800\n{libraries}",
            encoding="utf-8")

    def compile_fixture(self, command, **options):
        workspace = Path(options["cwd"])
        self.assertTrue(workspace.is_relative_to(self.sandbox), "Compiler escaped its fixture")
        info = self.api._resolve_board_info()
        build = workspace / ".pio" / "build" / "mcu_env"
        build.mkdir(parents=True, exist_ok=True)
        image = "firmware.hex" if info["platform"] == "atmelavr" else "firmware.bin"
        (build / image).write_bytes((self.api.current_board.encode() + b"\n") * 256)
        if info["platform"] == "espressif32":
            (build / "bootloader.bin").write_bytes(b"boot fixture" * 128)
            (build / "partitions.bin").write_bytes(b"partition fixture" * 128)
        self.calls.append((workspace, list(command), dict(options)))
        if self.compiler_effect is not None:
            self.compiler_effect()
        return FixtureCompiler(self.returncode)

    def compile_board(self, board):
        self.api.current_board = board
        self.api._stop_requested = False
        self.assertTrue(self.api._compile_worker(False), self.logs())
        return self.api._board_build_dir(board)

    def logs(self):
        return "\n".join(str(call.args[1].get("text", "")) for call in self.api.emit.call_args_list
                         if call.args[0] == "console:log")

    def test_switching_a_to_b_and_back_retains_both_real_worker_outputs(self):
        a = self.compile_board(AVR)
        a_bytes = (a / "firmware.hex").read_bytes()
        b = self.compile_board(ESP)
        self.assertNotEqual(a, b)
        self.assertEqual(self.calls[0][0], self.api._board_workspace_dir(AVR))
        self.assertEqual(self.calls[1][0], self.api._board_workspace_dir(ESP))
        self.assertEqual(a, self.calls[0][0] / ".pio/build/mcu_env")
        self.assertEqual(b, self.calls[1][0] / ".pio/build/mcu_env")
        self.assertEqual((a / "firmware.hex").read_bytes(), a_bytes)
        self.assertTrue((b / "firmware.bin").is_file())
        self.api.current_board = AVR
        self.assertTrue(self.api.check_can_skip_compile())
        self.assertEqual(self.api._find_cached_firmware_binary(), a / "firmware.hex")
        self.api.current_board = ESP
        self.assertTrue(self.api.check_can_skip_compile_for_upload())
        self.assertEqual(self.api._find_cached_firmware_binary(), b / "firmware.bin")
        self.assertFalse((self.cache / ".pio/build/mcu_env").exists())

    def test_restart_loads_requested_board_even_when_another_was_last(self):
        self.compile_board(AVR)
        self.compile_board(ESP)
        fresh = self.new_api()
        fresh.current_board = AVR
        self.assertTrue(fresh._load_compile_cache(AVR))
        self.assertEqual(fresh._last_compiled_board, AVR)
        self.assertTrue(fresh.check_can_skip_compile())
        self.assertTrue(fresh._find_cached_firmware_binary().is_relative_to(fresh._board_workspace_dir(AVR)))
        data = json.loads((self.cache / ".mcu_gui_cache.json").read_text(encoding="utf-8"))
        self.assertNotIn(AVR, data.get("boards", {}))
        self.assertNotIn(ESP, data.get("boards", {}))

    def test_framework_changes_use_another_workspace_and_can_return(self):
        self.compile_board(ESP)
        arduino_dir = self.api._board_workspace_dir()
        self.api._board_frameworks[ESP] = "espidf"
        self.assertNotEqual(self.api._board_workspace_dir(), arduino_dir)
        self.assertFalse(self.api.check_can_skip_compile())
        self.compile_board(ESP)
        self.api._board_frameworks[ESP] = "arduino"
        self.assertTrue(self.api.check_can_skip_compile())
        self.assertEqual(self.api._board_workspace_dir(), arduino_dir)

    def test_windows_and_ubuntu_workspaces_cannot_share_receipts(self):
        with patch.object(compiled_cache, "host_namespace", return_value="windows-x86_64"):
            self.compile_board(AVR)
            windows = self.api._board_workspace_dir()
        with patch.object(compiled_cache, "host_namespace", return_value="ubuntu-x86_64"):
            ubuntu = self.api._board_workspace_dir()
            self.assertNotEqual(windows, ubuntu)
            self.assertFalse(self.api.check_can_skip_compile())
            self.compile_board(AVR)
        with patch.object(compiled_cache, "host_namespace", return_value="windows-x86_64"):
            self.assertTrue(self.api.check_can_skip_compile())

    def test_source_same_size_and_timestamp_change_rejects_older_firmware(self):
        self.compile_board(AVR)
        old = self.source.stat()
        original = self.source.read_bytes()
        self.source.write_bytes(original.replace(b"setup", b"setUp"))
        os.utime(self.source, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertFalse(self.api.check_can_skip_compile())
        self.assertIn("changed", self.api._needs_recompile()[1].lower())

    def test_unsaved_editor_changes_reject_reuse(self):
        self.compile_board(AVR)
        self.api.modified_files = {str(self.source): True}
        self.assertFalse(self.api.check_can_skip_compile())

    def prepare_library(self):
        self.library.mkdir()
        header = self.library / "Library.h"
        header.write_bytes(b"#define FIXTURE_VALUE 1\n")
        return header

    def test_library_same_size_and_timestamp_edit_rejects_older_firmware(self):
        header = self.prepare_library()
        build = self.compile_board(AVR)
        stale_object = build / "old-library.o"
        stale_object.write_bytes(b"previous library object")
        self.assertTrue(self.api.check_can_skip_compile())
        old = header.stat()
        header.write_bytes(header.read_bytes().replace(b"1", b"2"))
        os.utime(header, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertFalse(self.api.check_can_skip_compile())
        self.assertIn("library", self.api._needs_recompile()[1].lower())
        self.compile_board(AVR)
        self.assertFalse(stale_object.exists())
        self.assertTrue(self.api.check_can_skip_compile())

    def test_failed_source_build_preserves_objects_with_unchanged_libraries(self):
        self.prepare_library()
        build = self.compile_board(AVR)
        previous_object = build / "library.o"
        previous_object.write_bytes(b"unchanged library object")
        self.returncode = 1
        self.assertFalse(self.api._compile_worker(False))
        self.assertTrue(previous_object.exists())
        self.returncode = 0
        self.compile_board(AVR)
        self.assertTrue(previous_object.exists())
        self.assertTrue(self.api.check_can_skip_compile())

    def test_failed_library_object_cleanup_never_records_new_input_signature(self):
        header = self.prepare_library()
        build = self.compile_board(AVR)
        workspace = self.api._board_workspace_dir()
        previous_inputs = compiled_cache.read_input_signature(workspace)
        stale_object = build / "old-library.o"
        stale_object.write_bytes(b"previous object")
        header.write_bytes(b"#define FIXTURE_VALUE 2\n")
        calls = len(self.calls)
        with patch.object(web_bridge, "robust_rmtree", return_value=False):
            self.assertFalse(self.api._compile_worker(False))
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(compiled_cache.read_input_signature(workspace), previous_inputs)
        self.assertFalse(self.api.check_can_skip_compile())
        self.compile_board(AVR)
        self.assertFalse(stale_object.exists())

    def test_library_change_during_build_cannot_certify_output(self):
        header = self.prepare_library()
        self.compiler_effect = lambda: header.write_bytes(b"#define FIXTURE_VALUE 2\n")
        self.assertFalse(self.api._compile_worker(False))
        self.assertFalse(self.api.check_can_skip_compile())
        self.assertIsNone(compiled_cache.read_receipt(self.api._board_workspace_dir()))

    def test_direct_header_only_library_inventory_is_bound(self):
        header = self.prepare_library()
        self.api._fixture_direct_library = True
        self.compile_board(AVR)
        added = self.library / "Added.h"
        added.write_bytes(b"// new header\n")
        self.assertFalse(self.api.check_can_skip_compile())
        added.unlink()
        self.assertTrue(self.api.check_can_skip_compile())
        header.unlink()
        self.assertFalse(self.api.check_can_skip_compile())

    def test_custom_library_script_and_asset_bytes_are_bound(self):
        self.prepare_library()
        extras = self.library / "extras"
        extras.mkdir()
        asset = extras / "build-data.csv"
        asset.write_bytes(b"value,1\n")
        script = extras / "prepare.py"
        script.write_bytes(b"VALUE = 1\n")
        self.compile_board(AVR)
        script.write_bytes(b"VALUE = 2\n")
        self.assertFalse(self.api.check_can_skip_compile())
        script.write_bytes(b"VALUE = 1\n")
        self.assertTrue(self.api.check_can_skip_compile())
        asset.write_bytes(b"value,2\n")
        self.assertFalse(self.api.check_can_skip_compile())

    def test_special_library_file_is_rejected_before_reading(self):
        self.prepare_library()
        special = self.library / "device-input"
        special.write_bytes(b"fixture")
        self.api._generate_platformio_ini(self.api._board_workspace_dir())
        original_stat = Path.stat

        def fixture_stat(path, *args, **kwargs):
            if path == special:
                return os.stat_result((stat.S_IFIFO, 0, 0, 0, 0, 0, 0, 0, 0, 0))
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", new=fixture_stat):
            with self.assertRaisesRegex(RuntimeError, "regular file"):
                compiled_cache.input_signature(self.api._board_workspace_dir())

    def test_same_size_and_timestamp_firmware_tamper_rejects_reuse(self):
        build = self.compile_board(AVR)
        firmware = build / "firmware.hex"
        old = firmware.stat()
        firmware.write_bytes(b"x" * old.st_size)
        os.utime(firmware, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertFalse(self.api.check_can_skip_compile())
        self.assertIsNone(self.api._find_cached_firmware_binary())

    def test_missing_firmware_or_esp_partition_rejects_reuse(self):
        build = self.compile_board(ESP)
        (build / "partitions.bin").unlink()
        self.assertFalse(self.api.check_can_skip_compile())
        self.compile_board(AVR)
        (self.api._board_build_dir() / "firmware.hex").unlink()
        self.assertFalse(self.api.check_can_skip_compile())

    def test_failed_rebuild_never_reuses_its_previous_success(self):
        a = self.compile_board(AVR)
        self.compile_board(ESP)
        b_bytes = (self.api._board_build_dir() / "firmware.bin").read_bytes()
        self.api.current_board = AVR
        self.returncode = 1
        self.assertFalse(self.api._compile_worker(False))
        self.assertFalse(self.api.check_can_skip_compile())
        self.assertIsNone(compiled_cache.read_receipt(self.api._board_workspace_dir()))
        self.api.current_board = ESP
        self.assertTrue(self.api.check_can_skip_compile())
        self.assertEqual((self.api._board_build_dir() / "firmware.bin").read_bytes(), b_bytes)

    def test_source_changed_during_compilation_cannot_certify_result(self):
        self.compiler_effect = lambda: self.source.write_text("void setup() { int x = 1; }\nvoid loop() {}\n", encoding="utf-8")
        self.api.current_board = AVR
        self.assertFalse(self.api._compile_worker(False))
        self.assertFalse(self.api.check_can_skip_compile())
        self.assertIsNone(compiled_cache.read_receipt(self.api._board_workspace_dir()))

    def test_recovery_cleanup_touches_selected_workspace_only(self):
        a = self.compile_board(AVR)
        b = self.compile_board(ESP)
        b_bytes = (b / "firmware.bin").read_bytes()
        a_object = a / "probe.cpp.o"
        a_object.write_bytes(b"object fixture")
        self.api.current_board = AVR
        self.assertTrue(self.api._clean_board_cache())
        self.assertFalse(a_object.exists())
        self.assertFalse(self.api.check_can_skip_compile())
        self.api.current_board = ESP
        self.assertEqual((b / "firmware.bin").read_bytes(), b_bytes)
        self.assertTrue(self.api.check_can_skip_compile())

    def test_partial_compile_cleanup_preserves_other_board(self):
        a = self.compile_board(AVR)
        b = self.compile_board(ESP)
        self.api.current_board = AVR
        self.api._clean_temporary_compile_artifacts(self.api._board_workspace_dir())
        self.assertFalse((a / "firmware.hex").exists())
        self.assertTrue((b / "firmware.bin").exists())
        self.assertTrue(self.api.check_can_skip_compile(ESP))

    def test_legacy_flat_images_and_name_only_cache_never_enable_skip(self):
        flat = self.cache / ".pio/build/mcu_env"
        flat.mkdir(parents=True)
        (flat / "firmware.hex").write_bytes(b"legacy image" * 256)
        source_hash = self.api._hash_sources()
        (self.cache / ".mcu_gui_cache.json").write_text(json.dumps({
            "last_board": AVR, "last_source_hash": source_hash,
            "boards": {AVR: {"board": AVR, "source_hash": source_hash}},
        }), encoding="utf-8")
        self.assertFalse(self.api._load_compile_cache())
        self.assertFalse(self.api.check_can_skip_compile())
        self.assertFalse(self.api._has_prior_build())
        self.assertIsNone(self.api._find_cached_firmware_binary())

    def test_only_upload_and_monitor_ini_options_are_ignored(self):
        self.compile_board(AVR)
        ini = self.api._board_workspace_dir() / "platformio.ini"
        original = ini.read_text(encoding="utf-8")
        ini.write_text(original.replace("monitor_speed = 115200", "monitor_speed = 9600")
                      .replace("upload_speed = 460800", "upload_speed = 115200") + "upload_port = COM99\n", encoding="utf-8")
        self.assertTrue(self.api.check_can_skip_compile())
        ini.write_text(ini.read_text(encoding="utf-8") + "build_flags = -DNEW_CONFIGURATION\n", encoding="utf-8")
        self.assertFalse(self.api.check_can_skip_compile())

    def prepare_native_upload(self):
        self.api._active_port_label = self.api.current_port = "COM-FIXTURE"
        self.api._active_board_info = self.api._resolve_board_info()
        self.api._check_write_connection = Mock()
        self.api._stop_serial_monitor = Mock()
        self.api._start_serial_monitor = Mock()
        self.api._compile_worker = Mock(side_effect=AssertionError("Reusable upload recompiled"))
        self.stack.enter_context(patch.object(web_bridge, "port_occupied_owner", return_value=None))

    def test_native_upload_after_switchback_uses_saved_board_folder(self):
        self.compile_board(AVR)
        self.compile_board(ESP)
        self.api.current_board = AVR
        workspace = self.api._board_workspace_dir()
        self.prepare_native_upload()
        self.api._native_upload_worker(can_skip=True)
        self.api._compile_worker.assert_not_called()
        self.assertEqual(self.calls[-1][0], workspace)
        self.assertIn("upload", self.calls[-1][1])
        self.assertTrue(any(call.args[0] == "notification" and call.args[1].get("title") == "Upload completed"
                            for call in self.api.emit.call_args_list))

    def test_change_after_skip_check_stops_before_native_programmer_launch(self):
        self.compile_board(AVR)
        self.assertTrue(self.api.check_can_skip_compile())
        self.source.write_text("void setup() { int changed = 1; }\nvoid loop() {}\n", encoding="utf-8")
        self.prepare_native_upload()
        previous_calls = len(self.calls)
        self.api._native_upload_worker(can_skip=True)
        self.assertEqual(len(self.calls), previous_calls)
        self.api._stop_serial_monitor.assert_not_called()
        self.assertIn("Saved build is unavailable", self.logs())

    def test_empty_successful_compiler_output_cannot_be_certified(self):
        self.compiler_effect = lambda: (self.api._board_build_dir() / "firmware.hex").unlink()
        self.assertFalse(self.api._compile_worker(False))
        self.assertFalse(self.api.check_can_skip_compile())
        self.assertIsNone(compiled_cache.read_receipt(self.api._board_workspace_dir()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
