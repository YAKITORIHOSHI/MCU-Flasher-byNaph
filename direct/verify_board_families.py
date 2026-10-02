#!/usr/bin/env python3
"""Isolated target/framework/transport regressions; no installs or hardware writes."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from main import web_bridge
from main.core import board_catalog
from main.core.target_profile import target_problem, requires_upload_port

# Representative contracts from PlatformIO board definitions/documentation.
# These fixtures verify app dispatch, not physical device/programmer success.
TARGETS = (
    ("Arduino Uno", "atmelavr", "uno", "arduino", "arduino", True, 115200),
    ("Arduino Nano ATmega328", "atmelavr", "nanoatmega328", "arduino", "arduino", True, 57600),
    ("Arduino Mega 2560", "atmelavr", "megaatmega2560", "arduino", "wiring", True, 115200),
    ("ST Nucleo F401RE", "ststm32", "nucleo_f401re", "arduino", "stlink", False, None),
    ("STM32 CMSIS project", "ststm32", "nucleo_f401re", "cmsis", "stlink", False, None),
    ("Raspberry Pi Pico", "raspberrypi", "pico", "arduino", "picotool", False, None),
    ("Arduino Zero programming", "atmelsam", "zero", "arduino", "cmsis-dap", False, None),
    ("Arduino Zero native USB", "atmelsam", "zeroUSB", "arduino", "sam-ba", True, None),
    ("Nordic nRF52840-DK", "nordicnrf52", "nrf52840_dk", "mbed", "jlink", False, None),
    ("Nordic Zephyr project", "nordicnrf52", "nrf52840_dk", "zephyr", "jlink", False, None),
    ("Teensy 4.1", "teensy", "teensy41", "arduino", "teensy-cli", False, None),
    ("Future declared target", "fixture_platform", "fixture_board", "fixture_sdk", "fixture_usb", False, None),
)


def info_for(row):
    name, platform, board, framework, protocol, port, speed = row
    return dict(platform=platform, board=board, framework=framework, frameworks=[framework],
                pio_resolved=True, upload_protocol=protocol, require_upload_port=port, upload_speed=speed)


class BoardFamilyChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp/audit/board-families"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit))).resolve()
        assert self.root.is_relative_to((ROOT / "temp").resolve())
        self.api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        self.api.current_board, self.api.current_port = "Fixture", "COM99"
        self.api.current_baud, self.api.upload_speed = 115200, "460800"
        self.api._active_process = None
        self.api.sketch_dir_path = self.root
        self.api._board_frameworks = {}
        self.api.emit = Mock()
        self.api._effective_cache_root = lambda path: self.root
        self.api._scan_includes_for_libs = lambda: []
        self.stack.enter_context(patch.object(web_bridge, "_project_root", self.root))
        self.stack.enter_context(patch.object(web_bridge, "ensure_file_writable"))
        original_expanduser = os.path.expanduser
        self.stack.enter_context(patch.object(web_bridge.os.path, "expanduser", side_effect=lambda value:
                                 str(self.root) if value == "~" else original_expanduser(value)))

    def test_exact_ini_across_board_families_and_frameworks(self):
        for row in TARGETS:
            with self.subTest(board=row[0]):
                info = info_for(row)
                self.api._resolve_board_info = lambda name=None: info
                self.assertEqual(target_problem(info), "")
                self.api._generate_platformio_ini(self.root)
                ini = (self.root / "platformio.ini").read_text(encoding="utf-8")
                for key in ("platform", "board", "framework", "upload_protocol"):
                    self.assertIn(f"{key} = {info[key]}\n", ini)
                self.assertNotIn("upload_speed =", ini)
                self.assertNotIn("espressif", ini)
                self.assertEqual(requires_upload_port(info), row[5])

    def test_framework_mismatch_and_arduino_source_rejected(self):
        native = info_for(TARGETS[4])
        self.assertIn("Arduino", target_problem(native, arduino_sketch=True))
        self.assertIn("not supported", target_problem(dict(native, framework="arduino")))
        self.assertTrue(target_problem(dict(native, upload_protocol="stlink\nboard = other")))
        self.assertTrue(requires_upload_port({"platform": "unknown", "upload_protocol": "unknown"}))

    def test_framework_changes_have_distinct_cache_identity(self):
        self.info = info_for(TARGETS[3])
        self.api._resolve_board_info = lambda name=None: self.info
        first = self.api._board_cache_key()
        self.info = dict(self.info, framework="cmsis", frameworks=["arduino", "cmsis"])
        second = self.api._board_cache_key()
        self.assertNotEqual(first, second)
        other = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        other.current_board = self.api.current_board
        other._resolve_board_info = lambda name=None: dict(self.info, board="another")
        self.assertNotEqual(second, other._board_cache_key())

    def test_manifest_defaults_survive_discovery_resolution_and_refresh(self):
        core = self.root / "core"
        boards = core / "platforms/fixture_platform/boards"
        boards.mkdir(parents=True)
        (boards / "fixture_board.json").write_text(json.dumps({
            "name": "Fixture native", "frameworks": ["fixture_sdk"], "build": {"mcu": "fixture"},
            "upload": {"protocol": "fixture_usb", "require_upload_port": False, "speed": 57600},
        }), encoding="utf-8")
        records = board_catalog._load_platformio_board_catalog(core)
        self.assertFalse(records[0]["require_upload_port"])
        seed = {"Fixture native": dict(platform="fixture_platform", board="fixture_board", framework="", pio_resolved=False)}
        resolved = board_catalog.resolve_board_definition("Fixture native", seed["Fixture native"], records)
        self.assertEqual(resolved["framework"], "fixture_sdk")
        with patch.object(board_catalog, "_load_platformio_board_catalog", return_value=records), \
                patch.object(board_catalog, "_get_arduino_board_search_roots", return_value=[]), \
                patch.object(board_catalog, "_save_board_catalog_cache"):
            fresh = board_catalog.load_dynamic_boards(seed)
        self.assertEqual(fresh["Fixture native"]["upload_speed"], 57600)
        self.assertFalse(fresh["Fixture native"]["require_upload_port"])

    def test_native_upload_uses_exact_protocol_and_only_serial_gets_port(self):
        for row in TARGETS[3:]:
            with self.subTest(board=row[0]):
                info = info_for(row)
                self.api._active_board_info = info
                self.api._resolve_board_info = lambda name=None: info
                self.api._active_port_label = "COM99"
                self.api._stop_requested = False
                self.api._stop_serial_monitor = Mock()
                self.api._start_serial_monitor = Mock()
                self.api._unmap_unc_after_build = Mock()
                self.api._get_jobs = lambda: 2
                self.api._compile_worker = Mock(return_value=True)
                process = SimpleNamespace(stdout=["Simulated successful write"], wait=lambda: 0, poll=lambda: 0)
                with patch.object(web_bridge, "port_occupied_owner", return_value=None), \
                        patch.object(web_bridge, "find_pio_executable", return_value=["SIMULATED_PIO"]), \
                        patch.object(web_bridge, "_refresh_platformio_core_environment", return_value=(self.root / "core", False)), \
                        patch.object(web_bridge.subprocess, "Popen", return_value=process) as launch:
                    self.api._native_upload_worker(can_skip=False)
                command = launch.call_args.args[0]
                self.assertEqual(launch.call_count, 1)
                self.assertEqual("--upload-port" in command, row[5])
                self.api._compile_worker.assert_called_once_with(is_upload=True)
                self.assertFalse(self.api.is_busy)

    def test_upload_does_not_start_after_compile_failure(self):
        self.api._active_board_info = info_for(TARGETS[3])
        self.api._active_port_label = ""
        self.api._stop_requested = False
        self.api._unmap_unc_after_build = Mock()
        self.api._compile_worker = Mock(return_value=False)
        with patch.object(web_bridge.subprocess, "Popen") as launch:
            self.api._native_upload_worker(can_skip=False)
        launch.assert_not_called()
        self.assertFalse(self.api.is_busy)

    def test_native_transport_is_rechecked_after_first_compile(self):
        before = dict(info_for(TARGETS[3]), upload_protocol="", require_upload_port=None)
        after = info_for(TARGETS[3])
        self.api._resolve_board_info = lambda name=None: self.info
        self.info = before
        self.api._active_board_info, self.api._active_port_label = before, "COM99"
        self.api._stop_requested = False
        self.api._stop_serial_monitor = self.api._start_serial_monitor = Mock()
        self.api._unmap_unc_after_build = Mock()
        self.api._get_jobs = lambda: 2
        def compile_target(**kwargs):
            self.info = after
            return True
        self.api._compile_worker = compile_target
        process = SimpleNamespace(stdout=[], wait=lambda: 0, poll=lambda: 0)
        with patch.object(web_bridge, "port_occupied_owner", return_value=None), \
                patch.object(web_bridge, "find_pio_executable", return_value=["SIMULATED_PIO"]), \
                patch.object(web_bridge, "_refresh_platformio_core_environment", return_value=(self.root, False)), \
                patch.object(web_bridge.subprocess, "Popen", return_value=process) as launch:
            self.api._native_upload_worker(can_skip=False)
        self.assertNotIn("--upload-port", launch.call_args.args[0])

    def test_serial_port_loss_during_compile_cancels_native_worker_upload(self):
        self.info = info_for(TARGETS[7])
        self.api._resolve_board_info = lambda name=None: self.info
        self.api._active_port_label = "COM99"
        self.api._stop_requested = False
        self.api._unmap_unc_after_build = Mock()
        def compile_target(**kwargs):
            self.api.current_port = ""
            return True
        self.api._compile_worker = compile_target
        with patch.object(web_bridge, "port_occupied_owner", return_value=None), \
                patch.object(web_bridge.subprocess, "Popen") as launch:
            self.api._native_upload_worker(can_skip=False)
        launch.assert_not_called()
        self.assertFalse(self.api.is_busy)

    def test_reset_image_keeps_declared_bootloader_defaults(self):
        for row in TARGETS[:8]:
            if row[3] != "arduino":
                continue
            with self.subTest(board=row[0]):
                ini, source, _ = self.api._reset_project_contents(row[0], info_for(row))
                self.assertNotIn("upload_speed =", ini)
                self.assertIn("upload_protocol = " + row[4], ini)
                self.assertIn("Arduino.h", source)

    def test_upload_button_and_public_pipeline_support_native_boards_without_com(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication, QMainWindow
        from main.qt.toolbar import PrimaryToolbar
        app = QApplication.instance() or QApplication([])
        class InlineThread:
            def __init__(self, target, args=(), **kwargs):
                self.target, self.args = target, args
            def start(self):
                self.target(*self.args)
        self.api._block_if_pending_ai_edits = Mock(return_value=False)
        self.api.check_can_skip_compile_for_upload = Mock(return_value=False)
        self.api._compile_worker = Mock(return_value=True)
        self.api._get_jobs = lambda: 2
        self.api._unmap_unc_after_build = Mock()
        self.api.current_port = ""
        self.api._stop_requested = False
        self.api.is_busy = False
        self.api.active_operation = self.api._current_op_phase = None
        for row in TARGETS[3:]:
            with self.subTest(board=row[0]), ExitStack() as stack:
                info = info_for(row)
                self.api.current_board = row[0]
                stack.enter_context(patch.object(web_bridge, "SUPPORTED_BOARDS", board_catalog.BoardCatalog({row[0]: info})))
                stack.enter_context(patch.object(web_bridge, "load_gui_config", return_value={}))
                stack.enter_context(patch("main.core.config._load_raw_config", return_value={"shared": {}, "instances": {}}))
                stack.enter_context(patch.object(web_bridge.threading, "Thread", InlineThread))
                stack.enter_context(patch.object(web_bridge, "find_pio_executable", return_value=["SIMULATED_PIO"]))
                stack.enter_context(patch.object(web_bridge, "_refresh_platformio_core_environment", return_value=(self.root, False)))
                process = SimpleNamespace(stdout=[], wait=lambda: 0, poll=lambda: 0)
                launch = stack.enter_context(patch.object(web_bridge.subprocess, "Popen", return_value=process))
                window = QMainWindow()
                toolbar = PrimaryToolbar(self.api, window)
                window.addToolBar(toolbar)
                toolbar._update_action_button_states()
                self.assertEqual(toolbar.btn_upload.isEnabled(), not row[5])
                if not row[5]:
                    self.assertIn("native", toolbar.btn_upload.toolTip())
                    toolbar.btn_upload.click()
                    self.assertEqual(launch.call_count, 1)
                    self.assertNotIn("--upload-port", launch.call_args.args[0])
                else:
                    self.api.upload_sketch()
                    launch.assert_not_called()
                window.close()
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_arduino_family_marker_does_not_certify_other_frameworks(self):
        if sys.platform != "win32":
            self.skipTest("Windows bootstrap certificate; Ubuntu resolves native packages")
        from src.modules import bootstrap
        core = self.root / "marker-core"
        scons = core / "packages/tool-scons"
        scons.mkdir(parents=True)
        (scons / "package.json").write_text("{}", encoding="utf-8")
        (scons / ".piopm").write_text("{}", encoding="utf-8")
        with patch.object(bootstrap, "_platform_already_installed", return_value=True):
            self.assertTrue(bootstrap.board_toolchain_ready(str(core), "ststm32", "fixture", "arduino"))
            self.assertFalse(bootstrap.board_toolchain_ready(str(core), "ststm32", "fixture", "cmsis"))

    def test_uf2_cache_is_recognized(self):
        self.api._resolve_board_info = lambda name=None: info_for(TARGETS[5])
        self.api._last_compiled_board = self.api.current_board
        build = self.root / ".pio/build/mcu_env"
        build.mkdir(parents=True)
        (build / "firmware.uf2").write_bytes(b"fixture" * 512)
        self.assertEqual(self.api._find_cached_firmware_binary().name, "firmware.uf2")
        self.assertTrue(self.api._has_prior_build())

    def test_non_arduino_preparation_resolves_packages_without_arduino_probe(self):
        if sys.platform != "win32":
            self.skipTest("Windows first-use probe; Ubuntu uses native PIO resolution")
        from src.modules import bootstrap
        for framework in ("arduino", "cmsis", "mbed", "zephyr", "fixture_sdk"):
            with self.subTest(framework=framework), ExitStack() as stack:
                prep = self.root / ("prepare_" + framework)
                prep.mkdir()
                seen = []
                def stream(command, env, **kwargs):
                    if kwargs.get("cwd"):
                        directory = Path(kwargs["cwd"])
                        source = directory / "src/main.cpp"
                        seen.append((command, source.read_text(encoding="utf-8") if source.exists() else "",
                                     (directory / "platformio.ini").read_text(encoding="utf-8")))
                    return True
                for name, value in (("find_pio", ["SIMULATED_PIO"]), ("ensure_platformio_scons", True),
                                    ("board_toolchain_ready", False), ("_check_and_extract_pio_zip_bundle", None),
                                    ("_ensure_platform_upload_tools", True), ("_write_board_toolchain_marker", None)):
                    stack.enter_context(patch.object(bootstrap, name, return_value=value))
                stack.enter_context(patch.object(bootstrap, "_stream_platformio_setup", side_effect=stream))
                stack.enter_context(patch.object(bootstrap.tempfile, "mkdtemp", return_value=str(prep)))
                stack.enter_context(patch.dict(os.environ, {"PLATFORMIO_CORE_DIR": str(self.root / "core")}))
                self.assertTrue(bootstrap.prepare_platformio_board_toolchain("fixture_platform", "fixture_board", framework))
                command, source, ini = seen[0]
                self.assertIn(f"framework = {framework}", ini)
                if framework == "arduino":
                    self.assertIn("Arduino.h", source)
                    self.assertIn("run", command)
                else:
                    self.assertEqual(source, "")
                    self.assertEqual(command[1:3], ["pkg", "install"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
