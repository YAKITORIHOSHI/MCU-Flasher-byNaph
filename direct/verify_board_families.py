#!/usr/bin/env python3
"""Isolated target/framework/transport regressions; no installs or hardware writes."""
from __future__ import annotations

import json
import hashlib
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

    def prepared_fixture(self, board_id="ebyte_e77_dev", unavailable="zephyr"):
        core = self.root / "prepared-core"
        manifests = core / "platforms/ststm32/boards"
        manifests.mkdir(parents=True)
        frameworks = (["arduino", "zephyr"] if unavailable == "zephyr" else
                      ["arduino", "cmsis", "libopencm3", "mbed", "spl", "stm32cube"])
        manifest = manifests / f"{board_id}.json"
        manifest.write_text(json.dumps({
            "name": "Fixture E77", "frameworks": frameworks,
            "build": {"mcu": "stm32wle5cc", "variant": "fixture_variant"},
            "upload": {"protocol": "stlink", "require_upload_port": False},
        }), encoding="utf-8")
        (manifests.parent / "platform.json").write_text(json.dumps({"version": "20.0.0"}), encoding="utf-8")
        provider = "framework-" + unavailable
        version = "3.40402.0" if unavailable == "zephyr" else "6.61700.231105"
        framework = core / "packages" / provider
        framework.mkdir(parents=True)
        (framework / "package.json").write_text(json.dumps({"name": provider, "version": version}), encoding="utf-8")
        reason = f"Installed {unavailable} has no board definition for {board_id}."
        row = {"platform": "ststm32", "id": board_id, "name": "Fixture E77",
               "declared_frameworks": frameworks, "frameworks": [value for value in frameworks if value != unavailable],
               "unavailable_frameworks": {unavailable: reason},
               "unavailability_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
               "unavailability_platform_version": "20.0.0", "unavailability_framework_versions": {unavailable: version}}
        snapshot = core / ".mcu-offline-catalog.json"
        snapshot.write_text(json.dumps([row]), encoding="utf-8")
        return core, snapshot, row

    def test_prepared_unavailable_framework_preserves_exact_board_and_supported_choices(self):
        core, _, row = self.prepared_fixture()
        records = board_catalog._load_platformio_board_catalog(core)
        prepared = records[0]
        self.assertEqual((prepared["platform"], prepared["id"]), ("ststm32", "ebyte_e77_dev"))
        self.assertEqual(prepared["frameworks"], {"arduino"})
        self.assertEqual(prepared["declared_frameworks"], ["arduino", "zephyr"])
        self.assertEqual(prepared["unavailable_frameworks"], row["unavailable_frameworks"])
        self.assertEqual(prepared["variant"], "fixture_variant")
        self.assertEqual(prepared["upload_protocol"], "stlink")
        seed = {"Cached E77": dict(platform="ststm32", board="ebyte_e77_dev", framework="zephyr")}
        resolved = board_catalog.resolve_board_definition("Cached E77", seed["Cached E77"], records)
        self.assertEqual(resolved["framework"], "arduino")
        self.assertEqual(resolved["unavailable_frameworks"], row["unavailable_frameworks"])
        with patch.object(board_catalog, "_load_platformio_board_catalog", return_value=records), \
                patch.object(board_catalog, "_get_arduino_board_search_roots", return_value=[]), \
                patch.object(board_catalog, "_save_board_catalog_cache"):
            for initial in (seed, {}):
                refreshed = board_catalog.load_dynamic_boards(initial)
                self.assertEqual(len(refreshed), 1)
                info = next(iter(refreshed.values()))
                self.assertEqual(info["frameworks"], ["arduino"])
                self.assertEqual(info["unavailable_frameworks"], row["unavailable_frameworks"])
        with patch.object(web_bridge, "SUPPORTED_BOARDS", {"Fixture": resolved}), \
                patch.object(web_bridge, "load_gui_config", return_value={}), \
                patch.object(web_bridge, "save_gui_config") as save:
            self.api._board_frameworks = {"Fixture": "zephyr"}
            self.assertEqual(self.api._resolve_board_info()["framework"], "arduino")
            self.assertFalse(self.api.set_board_framework("Fixture", "zephyr"))
            save.assert_not_called()

    def test_exact_olimex_mbed_exclusion_keeps_all_other_frameworks_and_board_identity(self):
        core, snapshot, row = self.prepared_fixture("olimex_f103", "mbed")
        records = board_catalog._load_platformio_board_catalog(core)
        expected = {"arduino", "cmsis", "libopencm3", "spl", "stm32cube"}
        self.assertEqual(records[0]["frameworks"], expected)
        self.assertEqual(records[0]["unavailable_frameworks"], row["unavailable_frameworks"])
        resolved = board_catalog.resolve_board_definition("Olimex STM32-H103",
                    dict(platform="ststm32", board="olimex_f103", framework="mbed"), records)
        self.assertEqual(resolved["board"], "olimex_f103")
        self.assertEqual(resolved["framework"], "arduino")
        for framework in expected:
            self.assertEqual(target_problem(dict(resolved, framework=framework)), "")
        self.assertIn(row["unavailable_frameworks"]["mbed"], target_problem(dict(resolved, framework="mbed")))
        manifest = core / "platforms/ststm32/boards/olimex_f103.json"
        # The same manifest and provider do not authorize filtering a new board.
        manifest.rename(manifest.with_name("future_mbed_board.json"))
        snapshot.write_text(json.dumps([dict(row, id="future_mbed_board")]), encoding="utf-8")
        self.assertIn("mbed", board_catalog._load_platformio_board_catalog(core)[0]["frameworks"])

    def test_cached_mbed_exclusion_is_removed_after_provider_changes_or_disappears(self):
        core, _, row = self.prepared_fixture("olimex_f103", "mbed")
        package = core / "packages/framework-mbed/package.json"
        with patch.object(board_catalog, "_get_safe_platformio_core_dir", return_value=str(core)), \
                patch.object(board_catalog, "_board_catalog_cache_path", return_value=self.root / "board-catalog.json"), \
                patch.object(board_catalog, "_BOARD_CATALOG_CACHE_RAM", None), \
                patch.object(board_catalog, "_PIO_BOARD_CATALOG_RAM_CACHE", {}):
            prepared = board_catalog._load_platformio_board_catalog(core)[0]
            self.assertNotIn("mbed", prepared["frameworks"])
            board_catalog._save_board_catalog_cache({"Fixture": dict(platform="ststm32", board="olimex_f103", framework="arduino")})
            self.assertIsNotNone(board_catalog._load_board_catalog_cache())
            package.write_text(json.dumps({"name": "framework-mbed", "version": "7.00000.0"}), encoding="utf-8")
            self.assertIsNone(board_catalog._load_board_catalog_cache())
            raw = board_catalog._load_platformio_board_catalog(core)[0]
            self.assertEqual(raw["frameworks"], set(row["declared_frameworks"]))
            self.assertFalse(raw.get("unavailable_frameworks"))
            self.assertIn("mbed", board_catalog.load_registry_board_catalog()[0]["frameworks"])
            package.unlink()
            self.assertIn("mbed", board_catalog._load_platformio_board_catalog(core)[0]["frameworks"])

    def test_unavailable_framework_reports_reason_even_with_stale_or_empty_allowed_list(self):
        reason = "Installed framework has no definition for this exact target."
        info = dict(platform="ststm32", board="ebyte_e77_dev", framework="zephyr",
                    frameworks=["arduino", "zephyr"], unavailable_frameworks={"zephyr": reason})
        for available in (["arduino", "zephyr"], ["arduino"], []):
            problem = target_problem(dict(info, frameworks=available))
            self.assertIn("ststm32:ebyte_e77_dev", problem)
            self.assertIn(reason, problem)
        self.assertEqual(target_problem(dict(info, framework="arduino")), "")

    def test_invalid_or_ambiguous_prepared_exclusions_retain_upstream_declarations(self):
        core, snapshot, row = self.prepared_fixture()
        cases = [
            [dict(row, unavailable_frameworks={"zephyr": ""})],
            [dict(row, unavailable_frameworks={"zephyr": "x" * 2049})],
            [dict(row, unavailable_frameworks={"missing": "No definition"})],
            [dict(row, frameworks=["arduino", "zephyr"])],
            [dict(row, declared_frameworks=["arduino"])],
            [dict(row, declared_frameworks="arduino,zephyr")],
            [dict(row, platform="other_platform")],
            [dict(row, id="we_oceanus1ev")],
            [dict(row, unavailability_manifest_sha256="0" * 64)],
            [dict(row, unavailability_manifest_sha256=None)],
            [dict(row, unavailability_platform_version="21.0.0")],
            [dict(row, unavailability_framework_versions={"zephyr": "3.50000.0"})],
            [row, row],
            [row, dict(row, frameworks=["zephyr"])],
        ]
        for records in cases:
            with self.subTest(records=records):
                snapshot.write_text(json.dumps(records), encoding="utf-8")
                with patch.object(board_catalog, "_PIO_BOARD_CATALOG_RAM_CACHE", {}):
                    raw = board_catalog._load_platformio_board_catalog(core)[0]
                self.assertEqual(raw["frameworks"], {"arduino", "zephyr"})
                self.assertFalse(raw.get("unavailable_frameworks"))
        for invalid in ("{broken", "{}"):
            snapshot.write_text(invalid, encoding="utf-8")
            with patch.object(board_catalog, "_PIO_BOARD_CATALOG_RAM_CACHE", {}):
                self.assertEqual(board_catalog._load_platformio_board_catalog(core)[0]["frameworks"], {"arduino", "zephyr"})

    def test_changed_same_board_manifest_or_framework_version_retains_normal_preparation(self):
        core, _, _ = self.prepared_fixture()
        manifest = core / "platforms/ststm32/boards/ebyte_e77_dev.json"
        original = manifest.read_text(encoding="utf-8")
        for changes in ({"mcu": "stm32different"}, {"zephyr": {"variant": "new_same_hardware_target"}}):
            board = json.loads(original)
            board["build"].update(changes)
            manifest.write_text(json.dumps(board), encoding="utf-8")
            self.assertEqual(board_catalog._load_platformio_board_catalog(core)[0]["frameworks"], {"arduino", "zephyr"})
        manifest.write_text(original, encoding="utf-8")
        self.assertEqual(board_catalog._load_platformio_board_catalog(core)[0]["frameworks"], {"arduino"})
        package = core / "packages/framework-zephyr/package.json"
        package.write_text(json.dumps({"name": "framework-zephyr", "version": "3.50000.0"}), encoding="utf-8")
        self.assertEqual(board_catalog._load_platformio_board_catalog(core)[0]["frameworks"], {"arduino", "zephyr"})
        package.unlink()
        self.assertEqual(board_catalog._load_platformio_board_catalog(core)[0]["frameworks"], {"arduino", "zephyr"})
        package.write_text(json.dumps({"name": "framework-zephyr", "version": "3.40402.0"}), encoding="utf-8")
        self.assertEqual(board_catalog._load_platformio_board_catalog(core)[0]["frameworks"], {"arduino"})
        platform = core / "platforms/ststm32/platform.json"
        platform.write_text(json.dumps({"version": "21.0.0"}), encoding="utf-8")
        self.assertEqual(board_catalog._load_platformio_board_catalog(core)[0]["frameworks"], {"arduino", "zephyr"})
        with patch.object(board_catalog, "_get_safe_platformio_core_dir", return_value=str(core)):
            self.assertEqual(board_catalog.load_registry_board_catalog()[0]["frameworks"], {"arduino", "zephyr"})
        platform.unlink()
        self.assertEqual(board_catalog._load_platformio_board_catalog(core)[0]["frameworks"], {"arduino", "zephyr"})

    def test_prepared_catalog_replacement_invalidates_ram_and_saved_framework_choices(self):
        core, snapshot, _ = self.prepared_fixture()
        with patch.object(board_catalog, "_get_safe_platformio_core_dir", return_value=str(core)), \
                patch.object(board_catalog, "_board_catalog_cache_path", return_value=self.root / "board-catalog.json"), \
                patch.object(board_catalog, "_BOARD_CATALOG_CACHE_RAM", None), \
                patch.object(board_catalog, "_PIO_BOARD_CATALOG_RAM_CACHE", {}):
            prepared = board_catalog._load_platformio_board_catalog(core)
            self.assertEqual(prepared[0]["frameworks"], {"arduino"})
            board_catalog._save_board_catalog_cache({"Fixture": dict(platform="ststm32", board="ebyte_e77_dev", framework="arduino")})
            self.assertIsNotNone(board_catalog._load_board_catalog_cache())
            snapshot.unlink()
            self.assertIsNone(board_catalog._load_board_catalog_cache())
            with patch.object(board_catalog, "_BOARD_CATALOG_CACHE_RAM", None):
                self.assertIsNone(board_catalog._load_board_catalog_cache())
            raw = board_catalog._load_platformio_board_catalog(core)
            self.assertEqual(raw[0]["frameworks"], {"arduino", "zephyr"})
            stale = dict(platform="ststm32", board="ebyte_e77_dev", framework="arduino",
                         unavailable_frameworks={"zephyr": "stale"}, declared_frameworks=["arduino", "zephyr"])
            repaired = board_catalog.resolve_board_definition("Fixture", stale, raw)
            self.assertEqual(repaired["unavailable_frameworks"], {})

    def test_prepared_registry_and_arduino_alias_keep_availability_metadata(self):
        core, _, row = self.prepared_fixture()
        with patch.object(board_catalog, "_get_safe_platformio_core_dir", return_value=str(core)):
            registry = board_catalog.load_registry_board_catalog()
        self.assertEqual(registry[0]["unavailable_frameworks"], row["unavailable_frameworks"])
        records = board_catalog._load_platformio_board_catalog(core)
        alias = dict(name="Arduino E77 alias", arduino_id="e77", source_file="fixture/boards.txt",
                     source_core="fixture", mcu="stm32wle5cc", variant="fixture_variant")
        with patch.object(board_catalog, "_load_platformio_board_catalog", return_value=records), \
                patch.object(board_catalog, "_get_arduino_board_search_roots", return_value=[self.root]), \
                patch.object(board_catalog, "_parse_downloaded_arduino_board_files", return_value=[alias]), \
                patch.object(board_catalog, "_save_board_catalog_cache"):
            aliases = board_catalog.load_dynamic_boards({})
        self.assertEqual(aliases["Arduino E77 alias"]["unavailable_frameworks"], row["unavailable_frameworks"])
        self.assertEqual(aliases["Arduino E77 alias"]["frameworks"], ["arduino"])

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
