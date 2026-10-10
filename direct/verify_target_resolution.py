#!/usr/bin/env python3
"""Exercise stale Arduino selections through the real Qt build entry points.

Fixtures mock persistence, discovery and firmware operations. The optional
--compile-installed-esp32 copies the required installed packages into temp/
and builds a tiny firmware there without uploading or changing the live store.
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow
from main.core import board_catalog as catalog_module
from main import web_bridge
from main.qt.signals import MCUSignals
from main.qt.toolbar import PrimaryToolbar
from src.modules import package_jobs

APP = QApplication.instance() or QApplication([])
NAME = "ESP32 Dev Module"


def stale_selection():
    return dict(platform="espressif32", board="", framework="arduino", frameworks=["arduino"],
                pio_resolved=False, arduino_board_id="esp32", arduino_variant="esp32",
                arduino_build_board="ESP32_DEV", mcu="esp32", hwids=set(), flash_mb=4,
                source_core="esp32-core-3.3.11")


def definitions():
    base = dict(platform="espressif32", frameworks={"arduino", "espidf"}, mcu="esp32",
                variant="esp32", arduino_defines={"arduinoesp32dev"}, hwids=set(),
                flash_size="4MB", flash_mode="dio", vendor="Espressif", manifest="fixture/esp32dev.json")
    return [dict(base, id="esp32dev", name="Espressif ESP32 Dev Module"),
            dict(base, id="esp32cam", name="AI Thinker ESP32-CAM", vendor="AI Thinker"),
            dict(base, id="rymcu-esp32-devkitc", name="RYMCU ESP32-DevKitC", vendor="RYMCU")]


def neutral_record():
    info = stale_selection()
    return dict(name=NAME, arduino_id=info["arduino_board_id"], build_board=info["arduino_build_board"],
                variant=info["arduino_variant"], mcu="esp32", hwids=set(),
                source_file="fixture/boards.txt", source_core=info["source_core"])


class TargetResolutionChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = ROOT / "temp/audit/target-resolution"
        root.mkdir(parents=True, exist_ok=True)
        self.sandbox = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=root)))
        self.stack.enter_context(patch.dict(os.environ, {
            "MCU_PACKAGE_EVENTS_ROOT": str(self.sandbox / "package-events"),
        }))
        self.stack.enter_context(patch.object(package_jobs, "package_core_directory", return_value=self.sandbox / "core"))
        (self.sandbox / "probe.ino").write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
        self.catalog = catalog_module.BoardCatalog({NAME: stale_selection()})
        self.stack.enter_context(patch.object(catalog_module, "SUPPORTED_BOARDS", self.catalog))
        self.stack.enter_context(patch.object(web_bridge, "SUPPORTED_BOARDS", self.catalog))
        self.manifest_loader = catalog_module._load_platformio_board_catalog
        self.installed = self.stack.enter_context(patch.object(catalog_module, "_load_platformio_board_catalog", return_value=definitions()))
        self.registry = self.stack.enter_context(patch.object(catalog_module, "load_registry_board_catalog", return_value=definitions()))
        self.saved = self.stack.enter_context(patch.object(catalog_module, "_save_board_catalog_cache"))
        self.stack.enter_context(patch.object(catalog_module, "_get_arduino_board_search_roots", return_value=[]))
        self.stack.enter_context(patch.object(web_bridge, "load_gui_config", return_value={}))
        self.stack.enter_context(patch.object(web_bridge, "ensure_file_writable"))
        self.stack.enter_context(patch("main.core.config._load_raw_config", return_value={"shared": {}, "instances": {}}))
        self.api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        self.api.current_board, self.api.current_port = NAME, ""
        self.api.sketch_dir_path = self.sandbox
        self.api.current_baud, self.api.upload_speed = 115200, "460800"
        self.api.is_busy = False
        self.api.active_operation = self.api._current_op_phase = None
        self.api._board_frameworks = {}
        self.api._block_if_pending_ai_edits = Mock(return_value=False)
        self.bus = MCUSignals()
        self.events = []
        def emit(event, data):
            self.events.append((event, data))
            if event == "operation:phase":
                self.bus.operation_phase.emit(data)
        self.api.emit = emit
        self.window = QMainWindow()
        self.toolbar = PrimaryToolbar(self.api, self.window)
        self.window.addToolBar(self.toolbar)
        self.toolbar.connect_signals(self.bus)
        self.workers = []
        self.addCleanup(self.dispose)

    def dispose(self):
        worker = getattr(self.api, "_operation_worker", None)
        for thread in self.workers + ([worker] if worker is not None else []):
            thread.join(timeout=3)
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait_idle(self):
        deadline = time.monotonic() + 3
        worker = getattr(self.api, "_operation_worker", None)
        while (self.api.is_busy or (worker is not None and worker.is_alive())) and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertFalse(self.api.is_busy)
        self.assertFalse(worker is not None and worker.is_alive())
        QTest.qWait(10)

    def test_compile_click_repairs_selected_cached_row_and_reaches_build_worker(self):
        invoked = []
        def compile_boundary(upload=False):
            invoked.append((threading.get_ident(), self.api._resolve_board_info(), upload))
            self.api._release_requested_operation()
            return True
        self.api._compile_worker = compile_boundary
        self.toolbar._update_action_button_states()
        self.assertTrue(self.toolbar.btn_compile.isEnabled())
        self.assertFalse(self.toolbar.btn_upload.isEnabled())
        self.toolbar.btn_compile.click()
        self.wait_idle()
        self.assertEqual(len(invoked), 1)
        self.assertNotEqual(invoked[0][0], threading.get_ident())
        self.assertEqual(invoked[0][1]["board"], "esp32dev")
        self.assertEqual(invoked[0][1]["framework"], "arduino")
        self.assertFalse(invoked[0][2])
        self.registry.assert_not_called()
        self.saved.assert_called_once()
        self.assertTrue(self.toolbar.btn_compile.isEnabled())
        self.assertFalse(any(event == "notification" for event, _ in self.events))

    def test_full_refresh_replaces_empty_id_without_duplicate_row(self):
        with patch.object(catalog_module, "_get_arduino_board_search_roots", return_value=[self.sandbox]), \
                patch.object(catalog_module, "_parse_downloaded_arduino_board_files", return_value=[neutral_record()]):
            refreshed = catalog_module.load_dynamic_boards({NAME: stale_selection()})
        self.assertEqual(refreshed[NAME]["board"], "esp32dev")
        self.assertTrue(refreshed[NAME]["pio_resolved"])
        self.assertNotIn(NAME + " (esp32)", refreshed)
        self.assertEqual(refreshed[NAME]["pio_match_reasons"], ["mcu", "variant", "name", "arduino-define"])
        self.assertEqual(self.catalog[NAME]["board"], "")

    def test_cache_refresh_works_without_downloaded_core_scan(self):
        seed = {NAME: stale_selection()}
        refreshed = catalog_module.load_dynamic_boards(seed)
        self.assertEqual(refreshed[NAME]["board"], "esp32dev")
        self.assertEqual(seed[NAME]["board"], "")

    def test_incremental_native_preview_precedes_matching_and_preserves_final_catalog(self):
        batches = []
        base = definitions()[0]
        native = [dict(base, id=f"exact_{number}", name=f"Exact fixture {number}") for number in range(270)]
        original = catalog_module._resolve_arduino_board_record
        def resolve(record, rows, **kwargs):
            self.assertTrue(batches, "Native definitions should be visible before fuzzy matching")
            return original(record, rows, **kwargs)
        with patch.object(catalog_module, "_load_platformio_board_catalog", return_value=native), \
                patch.object(catalog_module, "_get_arduino_board_search_roots", return_value=[self.sandbox]), \
                patch.object(catalog_module, "_parse_downloaded_arduino_board_files", return_value=[neutral_record()]):
            expected = catalog_module.load_dynamic_boards({})
            with patch.object(catalog_module, "_resolve_arduino_board_record", side_effect=resolve):
                actual = catalog_module.load_dynamic_boards({}, on_batch=lambda batch: batches.append(batch) if batch else None)
        self.assertEqual(actual, expected)
        self.assertEqual(len(batches[0]), 16)
        self.assertTrue(all(len(batch) <= 128 for batch in batches))
        first = next(iter(batches[0]))
        batches[0][first]["frameworks"].append("mutated-preview")
        self.assertNotIn("mutated-preview", actual[first]["frameworks"])

    def test_cancelled_incremental_refresh_does_not_save_incomplete_catalog(self):
        self.saved.reset_mock()
        with self.assertRaises(InterruptedError):
            catalog_module.load_dynamic_boards({}, on_batch=lambda _batch: False)
        self.saved.assert_not_called()

    def test_batch_refresh_rechecks_changed_identity_and_ambiguity(self):
        candidates = definitions()
        candidates[0].update(id="alpha-id", name="Fixture Alpha", variant="alpha", arduino_defines=set())
        candidates[1].update(id="beta-id", name="Fixture Beta", variant="beta", arduino_defines=set())
        records = [dict(neutral_record(), name="Fixture Alpha", arduino_id="alpha-record", variant="alpha"),
                   dict(neutral_record(), name="", arduino_id="not_a_boardid", variant="beta", build_board="")]
        self.installed.return_value = candidates
        with patch.object(catalog_module, "_get_arduino_board_search_roots", return_value=[self.sandbox]), \
                patch.object(catalog_module, "_parse_downloaded_arduino_board_files", return_value=records):
            first = catalog_module.load_dynamic_boards({})
            self.assertEqual(first["Fixture Alpha"]["board"], "alpha-id")
            self.assertEqual(first["not_a_boardid"]["board"], "beta-id")
            # The same dictionaries can change after preparation. A new refresh
            # must rebuild matching evidence, rather than reuse the old variant.
            candidates[1]["variant"] = "different-variant"
            changed = catalog_module.load_dynamic_boards({})
            self.assertFalse(changed["not_a_boardid"]["pio_resolved"])
            self.assertEqual(changed["not_a_boardid"]["board"], "")
            candidates[1]["variant"] = "beta"
            candidates.append(dict(candidates[1], id="second-beta-id"))
            ambiguous = catalog_module.load_dynamic_boards({})
            self.assertFalse(ambiguous["not_a_boardid"]["pio_resolved"])
            self.assertEqual(ambiguous["not_a_boardid"]["board"], "")

    def test_unprepared_board_never_reaches_registry_or_build(self):
        self.installed.return_value = []
        invoked = []
        self.api._compile_worker = lambda upload: (invoked.append(self.api._resolve_board_info()), self.api._release_requested_operation())
        self.api.compile_sketch()
        self.wait_idle()
        self.registry.assert_not_called()
        self.assertEqual(invoked, [])
        self.assertTrue(any("bootstrap" in str(data) for _, data in self.events))

    def test_generic_s3_compile_and_upload_report_ambiguity_without_repair_loop(self):
        name = "ESP32S3 Dev Module"
        selection = dict(stale_selection(), arduino_board_id="esp32s3", arduino_variant="esp32s3",
                         arduino_build_board="ESP32S3_DEV", mcu="esp32s3", flash_mb=4)
        base = dict(definitions()[0], mcu="esp32s3", variant="esp32s3", flash_size="8MB",
                    arduino_defines={"arduinoesp32s3dev"})
        candidates = [dict(base, id="esp32-s3-devkitc-1", name="Espressif ESP32-S3-DevKitC-1-N8"),
                      dict(base, id="esp32-s3-devkitm-1", name="Espressif ESP32-S3-DevKitM-1")]
        self.api.current_board, self.api.current_port = name, "SIMULATED"
        self.installed.return_value = candidates
        self.api._compile_worker = Mock()
        self.api._upload_worker = self.api._native_upload_worker = Mock()
        equal_score = lambda *_args, **_kwargs: (391.0, ["variant"])
        with patch.object(catalog_module, "_score_arduino_to_pio_board", side_effect=equal_score):
            for request in (self.api.compile_sketch, self.api.upload_sketch):
                with self.subTest(request=request.__name__):
                    self.catalog.replace({name: selection})
                    self.events.clear()
                    request()
                    self.wait_idle()
                    self.api._compile_worker.assert_not_called()
                    self.api._upload_worker.assert_not_called()
                    logs = "\n".join(str(data.get("text", "")) for event, data in self.events
                                     if event == "console:log")
                    self.assertIn("matches multiple PlatformIO definitions", logs)
                    self.assertIn("DevKitC", logs)
                    self.assertIn("DevKitM", logs)
                    self.assertNotIn("Prepare it in bootstrap while online", logs)
                    self.assertNotIn("Offline board definition unavailable", logs)
                    self.assertEqual(self.catalog[name]["board"], "")
                    self.assertEqual(self.catalog[name]["flash_mb"], 4)
                    self.assertEqual(self.catalog[name]["pio_resolution_status"], "ambiguous")
        self.registry.assert_not_called()

        # An explicit physical model retains its own definition and memory.
        native_name = candidates[0]["name"]
        native_info = catalog_module._native_board_entry(candidates[0])
        self.catalog.replace({native_name: native_info})
        self.api.current_board = native_name
        invoked = []
        def compile_boundary(upload=False):
            invoked.append(self.api._resolve_board_info())
            self.api._release_requested_operation()
        self.api._compile_worker = compile_boundary
        self.api.compile_sketch()
        self.wait_idle()
        self.assertEqual(invoked[0]["board"], "esp32-s3-devkitc-1")
        self.assertEqual(invoked[0]["flash_mb"], 8)

    def test_generic_s3_uses_best_platformio_match_from_successful_reference(self):
        name = "ESP32S3 Dev Module"
        selection = dict(stale_selection(), arduino_board_id="esp32s3", arduino_variant="esp32s3",
                         arduino_build_board="ESP32S3_DEV", mcu="esp32s3", flash_mb=4)
        candidate_m = dict(definitions()[0], id="esp32-s3-devkitm-1",
                           name="Espressif ESP32-S3-DevKitM-1", mcu="esp32s3",
                           variant="esp32s3", arduino_defines={"arduinoesp32s3dev"},
                           flash_size="16MB", has_psram=True)
        candidate_c = dict(candidate_m, id="esp32-s3-devkitc-1",
                           name="Espressif ESP32-S3-DevKitC-1-N8",
                           flash_size="8MB", has_psram=False)
        self.api.current_board = name
        self.installed.return_value = [candidate_c, candidate_m]
        self.catalog.replace({name: selection})
        invoked = []

        def compile_boundary(upload=False):
            invoked.append(self.api._resolve_board_info())
            self.api._release_requested_operation()

        self.api._compile_worker = compile_boundary
        with patch("main.core.arduino_backend.run_arduino_operation") as arduino_operation:
            self.api.compile_sketch()
            self.wait_idle()

        self.assertEqual(len(invoked), 1)
        self.assertEqual(invoked[0]["backend"], "platformio")
        self.assertEqual(invoked[0]["board"], "esp32-s3-devkitm-1")
        self.assertEqual(invoked[0]["framework"], "arduino")
        arduino_operation.assert_not_called()

    def test_generic_s3_ambiguous_pio_models_override_prepared_cli_source_target(self):
        from src.modules.arduino_cli_support import source_declaration_proof
        name = "ESP32S3 Dev Module"
        selection = dict(stale_selection(), arduino_board_id="esp32s3", arduino_variant="esp32s3",
                         arduino_build_board="ESP32S3_DEV", mcu="esp32s3",
                         arduino_source_file=str(self.sandbox / "boards.txt"), arduino_source_sha256="a" * 64)
        record = {"arduino_id": "esp32s3", "source_sha256": "a" * 64}
        proof = source_declaration_proof(record, {"package": "esp32", "architecture": "esp32", "version": "3.3.11"})
        prepared = dict(record, status="ready", backend="arduino-cli", platform="esp32:esp32",
                        board="esp32s3", arduino_fqbn="esp32:esp32:esp32s3",
                        arduino_backend_role="primary", arduino_source_proof=proof,
                        arduino_cli={"fqbn": "esp32:esp32:esp32s3", "version": "3.3.11"})
        self.api.current_board, self.api.current_port = name, "SIMULATED"
        base = dict(definitions()[0], mcu="esp32s3", variant="esp32s3", arduino_defines={"arduinoesp32s3dev"})
        self.installed.return_value = [dict(base, id="s3-a", name="Espressif ESP32-S3 DevKit A"),
                                       dict(base, id="s3-b", name="Espressif ESP32-S3 DevKit B")]
        self.api._compile_worker = Mock()
        self.api._upload_worker = self.api._native_upload_worker = Mock()
        equal_score = lambda *_args, **_kwargs: (391.0, ["variant"])
        with patch("src.modules.arduino_cli_support.prepared_target_for_record", return_value=prepared), \
                patch.object(catalog_module, "_score_arduino_to_pio_board", side_effect=equal_score), \
                patch("main.core.arduino_backend.run_arduino_operation") as arduino_operation:
            for request in (self.api.compile_sketch, self.api.upload_sketch):
                self.catalog.replace({name: selection})
                self.events.clear()
                request()
                self.wait_idle()
                self.api._compile_worker.assert_not_called()
                self.api._upload_worker.assert_not_called()
                self.api._native_upload_worker.assert_not_called()
                logs = "\n".join(str(data.get("text", "")) for event, data in self.events
                                 if event == "console:log")
                self.assertIn("matches multiple PlatformIO definitions", logs)
                self.assertIn("DevKit A", logs)
                self.assertIn("DevKit B", logs)
                info = self.api._resolve_board_info(name)
                self.assertEqual(info["backend"], "platformio")
                self.assertEqual(info["pio_resolution_status"], "ambiguous")
                self.assertEqual(info["board"], "")
            arduino_operation.assert_not_called()
        self.registry.assert_not_called()

    def test_ambiguous_row_and_wrong_framework_do_not_reach_compile(self):
        for ambiguous in (True, False):
            self.api._board_frameworks = {} if ambiguous else {NAME: "espidf"}
            self.catalog.replace({NAME: stale_selection()})
            candidates = definitions()
            if ambiguous:
                candidates.append(dict(candidates[0], id="same-name-second-board"))
            self.installed.return_value = self.registry.return_value = candidates
            self.api._compile_worker = Mock()
            score_context = (patch.object(catalog_module, "_score_arduino_to_pio_board",
                                          side_effect=lambda *_args, **_kwargs: (200.0, ["variant"]))
                             if ambiguous else patch.object(catalog_module, "_score_arduino_to_pio_board",
                                                            wraps=catalog_module._score_arduino_to_pio_board))
            with score_context:
                self.api.compile_sketch()
                self.wait_idle()
            self.api._compile_worker.assert_not_called()
            self.assertIsNone(self.api.active_operation)
            self.assertTrue(self.toolbar.btn_compile.isEnabled())

    def test_upload_resolves_before_snapshot_and_source_hashing(self):
        self.api.current_port = "SIMULATED"
        self.api.skip_compile = True
        self.api.check_can_skip_compile_for_upload = Mock(return_value=False)
        invoked = []
        def upload_boundary(can_skip):
            invoked.append((threading.get_ident(), self.api._active_board_info, can_skip))
            self.api._release_requested_operation()
        self.api._upload_worker = self.api._native_upload_worker = upload_boundary
        self.toolbar._update_action_button_states()
        self.toolbar.btn_upload.click()
        self.wait_idle()
        self.assertEqual(invoked[0][1]["board"], "esp32dev")
        self.assertNotEqual(invoked[0][0], threading.get_ident())
        self.assertFalse(invoked[0][2])
        self.api.check_can_skip_compile_for_upload.assert_called_once_with(NAME)

    def test_unavailable_registry_reports_failure_and_releases_busy_state(self):
        self.installed.return_value = []
        self.registry.side_effect = OSError("Offline")
        self.api._compile_worker = Mock()
        self.api.compile_sketch()
        self.wait_idle()
        self.api._compile_worker.assert_not_called()
        expected_kind = "Native" if sys.platform.startswith("linux") else "Offline"
        self.assertTrue(any(f"{expected_kind} board definition unavailable" in str(data) for _, data in self.events))
        self.registry.assert_not_called()
        self.assertIsNone(self.api.active_operation)

    def test_port_lost_during_resolution_never_starts_upload(self):
        self.api.current_port = "SIMULATED"
        self.installed.return_value = []
        def disconnect():
            self.api.current_port = ""
            return definitions()
        self.registry.side_effect = disconnect
        self.api._upload_worker = self.api._native_upload_worker = Mock()
        self.api.upload_sketch()
        self.wait_idle()
        self.api._upload_worker.assert_not_called()
        self.assertIsNone(self.api.active_operation)

    def test_generated_ini_uses_canonical_target_and_selected_framework(self):
        self.assertTrue(self.api._resolve_requested_target("Compile"))
        self.api._scan_includes_for_libs = lambda: []
        with patch.object(Path, "home", return_value=self.sandbox), \
                patch.object(web_bridge.os.path, "expanduser", return_value=str(self.sandbox)), \
                patch.object(web_bridge, "_project_root", self.sandbox):
            self.api._generate_platformio_ini(self.sandbox)
        ini = (self.sandbox / "platformio.ini").read_text(encoding="utf-8")
        self.assertIn("platform = espressif32\n", ini)
        self.assertIn("board = esp32dev\n", ini)
        self.assertIn("framework = arduino\n", ini)

    def test_manifest_discovery_uses_the_build_store_despite_foreign_environment(self):
        core = self.sandbox / "our-core"
        board_dir = core / "platforms/espressif32/boards"
        board_dir.mkdir(parents=True)
        (board_dir / "esp32dev.json").write_text(json.dumps({
            "name": "Espressif ESP32 Dev Module", "vendor": "Espressif", "frameworks": ["arduino"],
            "build": {"mcu": "esp32", "variant": "esp32", "extra_flags": "-DARDUINO_ESP32_DEV"},
        }), encoding="utf-8")
        with patch.dict(os.environ, {"PLATFORMIO_CORE_DIR": str(self.sandbox / "foreign-core")}), \
                patch.object(catalog_module, "_get_safe_platformio_core_dir", return_value=str(core)):
            records = self.manifest_loader()
        self.assertEqual([record["id"] for record in records], ["esp32dev"])


def compile_installed_esp32():
    """Use independent copied packages, source, preferences and build outputs."""
    if sys.platform != "win32":
        raise RuntimeError("This optional probe uses this checkout's Windows packages.")
    store = ROOT / "src/.platformio-mcu-gui"
    needed = ["framework-arduinoespressif32", "toolchain-xtensa-esp32", "tool-esptoolpy", "tool-scons"]
    for name in needed:
        if not (store / "packages" / name / "package.json").is_file():
            raise RuntimeError(f"Missing installed package {name}; probe will not install it.")
    root = ROOT / "temp/audit/target-resolution"
    root.mkdir(parents=True, exist_ok=True)
    sandbox = Path(tempfile.mkdtemp(prefix="esp32-build-", dir=root)).resolve()
    assert sandbox.is_relative_to((ROOT / "temp").resolve())
    core, workspace = sandbox / "core", sandbox / "project"
    workspace.mkdir()
    print(f"Isolated firmware probe: {sandbox}", flush=True)
    def extended(path):
        return "\\\\?\\" + str(path.resolve())
    shutil.copytree(extended(store / "platforms/espressif32"), extended(core / "platforms/espressif32"))
    for name in needed:
        print(f"Copying installed package {name}", flush=True)
        shutil.copytree(extended(store / "packages" / name), extended(core / "packages" / name))
    # Keep GCC's command line short, just as the application uses a short
    # alias for its own package store. No external junctions are created.
    short_path = ctypes.create_unicode_buffer(32768)
    if sys.platform == "win32" and ctypes.windll.kernel32.GetShortPathNameW(str(sandbox), short_path, len(short_path)):
        core, workspace = Path(short_path.value) / "core", Path(short_path.value) / "project"
    manifests = catalog_module._load_platformio_board_catalog(core)
    resolved = catalog_module.resolve_board_definition(NAME, stale_selection(), manifests)
    if resolved.get("board") != "esp32dev" or not resolved.get("pio_resolved"):
        raise AssertionError("Installed ESP32 definition still fails to resolve")
    api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
    api.current_board, api.current_baud, api.upload_speed = NAME, 115200, "460800"
    api._resolve_board_info = lambda name=None: resolved
    api._scan_includes_for_libs = lambda: []
    api.emit = lambda *args: None
    with patch.object(Path, "home", return_value=sandbox), \
            patch.object(web_bridge.os.path, "expanduser", return_value=str(sandbox)), \
            patch.object(web_bridge, "_project_root", sandbox), \
            patch.object(web_bridge, "ensure_file_writable"):
        api._generate_platformio_ini(workspace)
    source = workspace / "src"
    source.mkdir()
    (source / "main.cpp").write_text('#include <Arduino.h>\nvoid setup() { Serial.begin(115200); }\nvoid loop() { delay(100); }\n', encoding="utf-8")
    env = os.environ.copy()
    env.update(PLATFORMIO_CORE_DIR=str(core), PLATFORMIO_CACHE_DIR=str(core / ".cache"),
               PLATFORMIO_BUILD_CACHE_DIR=str(core / ".cache/build"), PLATFORMIO_GLOBALLIB_DIR=str(core / "lib"),
               PLATFORMIO_NO_TELEMETRY="1", PLATFORMIO_DISABLE_TELEMETRY="1", PLATFORMIO_DISABLE_UPGRADE_CHECK="1",
               PYTHONDONTWRITEBYTECODE="1", TEMP=str(sandbox), TMP=str(sandbox), TMPDIR=str(sandbox))
    command = [sys.executable, "-B", "-m", "platformio", "run", "-d", str(workspace), "-j", "4"]
    log = sandbox / "compile.log"
    print("Compiling the generated espressif32:esp32dev Arduino target with copied toolchains", flush=True)
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=240,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-18:]), flush=True)
    report = {"target": "espressif32:esp32dev", "framework": "arduino", "exit_code": result.returncode,
              "firmware": str(workspace / ".pio/build/mcu_env/firmware.bin"), "log": str(log)}
    (root / "esp32-build-result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if result.returncode or not Path(report["firmware"]).is_file():
        raise AssertionError(f"Isolated compile failed; see {log}")
    print("Isolated ESP32 firmware build: OK", flush=True)
    verify_built_pipeline(sandbox)


def verify_built_pipeline(sandbox, *, display_name=NAME, selection=None, artifact="firmware.bin", workspace_name="project"):
    """Click Compile through the application worker with the copied toolchains."""
    sandbox = Path(sandbox).resolve()
    assert sandbox.is_relative_to((ROOT / "temp").resolve())
    short_path = ctypes.create_unicode_buffer(32768)
    if sys.platform == "win32" and ctypes.windll.kernel32.GetShortPathNameW(str(sandbox), short_path, len(short_path)):
        sandbox = Path(short_path.value)
    core, workspace, sketch = sandbox / "core", sandbox / workspace_name, sandbox / (workspace_name + "-sketch")
    workspace.mkdir(exist_ok=True)
    sketch.mkdir(exist_ok=True)
    (core / ".tmp").mkdir(exist_ok=True)
    (sketch / "probe.ino").write_text('void setup() { Serial.begin(115200); }\nvoid loop() { delay(100); }\n', encoding="utf-8")
    api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
    api.current_board, api.current_port = display_name, ""
    api.current_baud, api.upload_speed = 115200, "460800"
    api.sketch_dir_path = sketch
    api._board_frameworks = {}
    api.is_busy = False
    api.active_operation = api._current_op_phase = api._active_process = None
    api._block_if_pending_ai_edits = Mock(return_value=False)
    api._effective_cache_root = lambda _path: workspace
    api._scan_includes_for_libs = lambda: []
    api._get_jobs = lambda: 4
    api._unmap_unc_after_build = lambda: None
    from main.core import compiled_cache
    api._save_compile_cache = Mock(side_effect=lambda board, source_hash, build_metadata=None, build_inputs=None:
        compiled_cache.write_receipt(api._board_workspace_dir(board), board_key=api._board_cache_key(board),
                                     board_name=board, source_hash=source_hash, metadata=build_metadata,
                                     build_inputs=build_inputs))
    api.update_skip_compile_availability = Mock()
    bus = MCUSignals()
    events = []
    def emit(event, data):
        events.append((event, data))
        if event == "operation:phase":
            bus.operation_phase.emit(data)
    api.emit = emit
    isolated_catalog = catalog_module.BoardCatalog({display_name: selection or stale_selection()})
    original_popen = subprocess.Popen
    original_expanduser = os.path.expanduser
    def launch(*args, **kwargs):
        kwargs["env"]["PYTHONDONTWRITEBYTECODE"] = "1"
        return original_popen(*args, **kwargs)
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "MCU_PACKAGE_EVENTS_ROOT": str(sandbox / "package-events"),
            "PLATFORMIO_CORE_DIR": str(core), "PLATFORMIO_PACKAGES_DIR": str(core / "packages"),
            "PLATFORMIO_PLATFORMS_DIR": str(core / "platforms"), "PLATFORMIO_GLOBALLIB_DIR": str(core / "lib"),
            "PLATFORMIO_CACHE_DIR": str(core / ".cache"), "TMPDIR": str(core / ".tmp"),
            "PLATFORMIO_BUILD_CACHE_DIR": str(core / ".cache/build"),
            "TMP": str(core / ".tmp"), "TEMP": str(core / ".tmp"),
        }))
        stack.enter_context(patch.object(package_jobs, "package_core_directory", return_value=core))
        for owner, name, value in ((catalog_module, "SUPPORTED_BOARDS", isolated_catalog),
                                  (web_bridge, "SUPPORTED_BOARDS", isolated_catalog),
                                  (web_bridge, "_project_root", sandbox)):
            stack.enter_context(patch.object(owner, name, value))
        for owner, name, value in ((web_bridge, "_refresh_platformio_core_environment", (core, False)),
                                  (web_bridge, "find_pio_executable", [sys.executable, "-B", str(ROOT / "src/modules/offline_platformio.py")]),
                                  (web_bridge, "_get_safe_platformio_core_dir", str(core)),
                                  (web_bridge, "board_toolchain_ready", True),
                                  (web_bridge, "ensure_scons_ready", True),
                                  (web_bridge, "load_gui_config", {}),
                                  (catalog_module, "_get_safe_platformio_core_dir", str(core)),
                                  (catalog_module, "_save_board_catalog_cache", None),
                                  (web_bridge, "hide_generated_directory", None),
                                  (web_bridge, "ensure_file_writable", None)):
            stack.enter_context(patch.object(owner, name, return_value=value))
        stack.enter_context(patch.object(web_bridge, "prepare_platformio_board_toolchain", side_effect=AssertionError("Probe must not install")))
        stack.enter_context(patch.object(web_bridge.subprocess, "Popen", side_effect=launch))
        stack.enter_context(patch.object(Path, "home", return_value=sandbox))
        stack.enter_context(patch.object(web_bridge.os.path, "expanduser", side_effect=lambda value:
                                        str(sandbox) if value == "~" else original_expanduser(value)))
        stack.enter_context(patch("main.core.config._load_raw_config", return_value={"shared": {}, "instances": {}}))
        window = QMainWindow()
        toolbar = PrimaryToolbar(api, window)
        window.addToolBar(toolbar)
        toolbar.connect_signals(bus)
        toolbar._update_action_button_states()
        print("Clicking Compile through the application using an unresolved cached selection", flush=True)
        toolbar.btn_compile.click()
        deadline = time.monotonic() + 240
        while api.is_busy and time.monotonic() < deadline:
            QTest.qWait(10)
        if api.is_busy:
            api.stop_operation()
            raise AssertionError("Application compile exceeded probe timeout")
        QTest.qWait(20)
        messages = [str(data.get("text", "")) for event, data in events if event == "console:log"]
        log = sandbox / ("application-compile.log" if workspace_name == "project" else workspace_name + "-compile.log")
        log.write_text("\n".join(messages) + "\n", encoding="utf-8")
        assert toolbar.btn_compile.isEnabled() and not toolbar.btn_upload.isEnabled()
        succeeded = any(event == "notification" and data.get("title") == "Build Succeeded" for event, data in events)
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if not succeeded:
            print("\n".join(messages[-15:]), flush=True)
            raise AssertionError(f"Application compile failed; see {log}")
        api._save_compile_cache.assert_called_once()
        firmware = api._board_build_dir() / artifact
        assert firmware.is_file()
        assert api.check_can_skip_compile(), "Successful real compiler output must be reusable for this board"
        resolved = api._resolve_board_info()
        report_name = "application-build-result.json" if workspace_name == "project" else workspace_name + "-build-result.json"
        (sandbox / report_name).write_text(json.dumps({
            "success": True, "target": resolved["platform"] + ":" + resolved["board"], "framework": resolved["framework"],
            "source": str(sketch / "probe.ino"), "firmware": str(firmware),
            "log": str(log), "live_persistence": False, "upload": False,
        }, indent=2) + "\n", encoding="utf-8")
        print(f"Application Compile {display_name} -> convert .ino -> PlatformIO -> {artifact}: OK", flush=True)


def compile_native_esp32(core):
    """Compile an explicitly prepared native fixture; never install or upload."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("The native ESP32 fixture probe requires Ubuntu.")
    core = Path(core).resolve()
    if core.name != "core" or not core.is_relative_to((ROOT / "temp").resolve()):
        raise RuntimeError("Native ESP32 verification requires an isolated temp/ fixture named core.")
    manifest = core / "platforms/espressif32/boards/esp32dev.json"
    if not manifest.is_file():
        raise RuntimeError(f"Native ESP32 board definition is absent: {manifest}. The probe will not install it.")
    needed = ["framework-arduinoespressif32", "toolchain-xtensa-esp32", "tool-esptoolpy", "tool-scons"]
    for name in needed:
        if not (core / "packages" / name / "package.json").is_file():
            raise RuntimeError(f"Native ESP32 package {name} is absent. The probe will not install it.")
    compiler = core / "packages/toolchain-xtensa-esp32/bin/xtensa-esp32-elf-g++"
    with compiler.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise RuntimeError("The fixture requires a native ELF compiler; Windows packages cannot be used on Ubuntu.")
    verify_built_pipeline(core.parent)


def compile_installed_avr(store=None):
    """Prove three non-ESP targets with copied host-native packages and Compile clicks."""
    if store is None:
        if sys.platform.startswith("linux"):
            from src.modules.platform_runtime import native_platformio_dir
            store = native_platformio_dir()
        elif sys.platform == "win32":
            store = ROOT / "src/.platformio-mcu-gui"
        else:
            raise RuntimeError("This probe supports Windows and Ubuntu/Linux.")
    store = Path(store)
    if sys.platform.startswith("linux"):
        assert not store.resolve().is_relative_to((ROOT / "src/.platformio-mcu-gui").resolve()), "Windows packages cannot be used on Ubuntu"
    needed = ["framework-arduino-avr", "toolchain-atmelavr", "tool-avrdude", "tool-scons"]
    for package in needed:
        if not (store / "packages" / package / "package.json").is_file():
            raise RuntimeError(f"Missing installed package {package}; probe will not install it.")
    root = ROOT / "temp/audit/target-resolution"
    root.mkdir(parents=True, exist_ok=True)
    sandbox = Path(tempfile.mkdtemp(prefix="avr-build-", dir=root)).resolve()
    def extended(path):
        return "\\\\?\\" + str(path.resolve()) if sys.platform == "win32" else str(path.resolve())
    core = sandbox / "core"
    shutil.copytree(extended(store / "platforms/atmelavr"), extended(core / "platforms/atmelavr"))
    for package in needed:
        shutil.copytree(extended(store / "packages" / package), extended(core / "packages" / package))
    print(f"Isolated AVR build probes: {sandbox}", flush=True)
    for board_id in ("uno", "nanoatmega328", "megaatmega2560"):
        manifest = json.loads((core / "platforms/atmelavr/boards" / (board_id + ".json")).read_text(encoding="utf-8"))
        name = manifest["name"]
        build = manifest.get("build") or {}
        flags = build.get("extra_flags") or ""
        if isinstance(flags, list):
            flags = " ".join(str(flag) for flag in flags)
        define = re.search(r"-D\s*ARDUINO_([A-Za-z0-9_]+)", str(flags))
        selection = dict(platform="atmelavr", board="", framework="arduino", frameworks=["arduino"],
                         pio_resolved=False, arduino_board_id=board_id, mcu=build.get("mcu", ""),
                         arduino_build_board=define.group(1) if define else "",
                         arduino_variant=build.get("variant", ""))
        verify_built_pipeline(sandbox, display_name=name, selection=selection, artifact="firmware.hex", workspace_name=board_id)
    (root / "avr-build-result.json").write_text(json.dumps({
        "success": True, "sandbox": str(sandbox), "boards": ["uno", "nanoatmega328", "megaatmega2560"],
        "actual_compile_clicks": True, "upload": False, "live_persistence": False,
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--compile-installed-esp32", action="store_true")
    parser.add_argument("--compile-native-esp32", type=Path,
                        help="Compile ESP32 through the actual Qt button using an explicitly prepared temp/ fixture core")
    parser.add_argument("--compile-installed-avr", action="store_true")
    parser.add_argument("--avr-core", type=Path, help="Explicit native fixture store for the AVR probe (read-only)")
    parser.add_argument("--verify-built-pipeline", type=Path)
    parser.add_argument("--compile-button-only", type=Path,
                        help="Run only the real Compile-button integration in an explicitly prepared temp/ fixture")
    args, remaining = parser.parse_known_args()
    if args.compile_installed_esp32:
        compile_installed_esp32()
    elif args.compile_native_esp32:
        compile_native_esp32(args.compile_native_esp32)
    elif args.compile_installed_avr:
        compile_installed_avr(args.avr_core)
    elif args.compile_button_only:
        verify_built_pipeline(args.compile_button_only)
        # This one-shot first-run probe is a child process; Ubuntu's legacy
        # PySide finalizer can abort after the successful compile report.
        # The parent validates the report and firmware artifact before passing.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    elif args.verify_built_pipeline:
        verify_built_pipeline(args.verify_built_pipeline)
    checks = unittest.main(argv=[sys.argv[0], *remaining], verbosity=2, exit=False)
    # Some Linux/PySide combinations keep deferred widgets alive through
    # interpreter finalization, which can abort with none_dealloc after the
    # successful test summary. Drain Qt's deferred-delete queue while Python is
    # still active so this verifier reports its actual test result reliably.
    APP.closeAllWindows()
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()
    gc.collect()
    raise SystemExit(0 if checks.result.wasSuccessful() else 1)
