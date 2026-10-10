#!/usr/bin/env python3
"""Hardware-free proof and operation boundaries for exact Arduino CLI fallback."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from src.modules import arduino_cli_support as support
from src.modules import arduino_board_selection as selection
from main.core import board_catalog, target_profile, arduino_backend, toolchain
from main import web_bridge

PROOF = {"schema": 1, "kind": "registry-exact-target-absent", "catalog_count": 99,
         "catalog_sha256": "a" * 64, "checked_at": "2026-10-08T00:00:00+00:00"}


class FallbackChecks(unittest.TestCase):
    def setUp(self):
        root = ROOT / "temp/audit/arduino-fallback"
        root.mkdir(parents=True, exist_ok=True)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.fixture = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=root)))
        self.core = self.fixture / "core"
        self.download = self.fixture / "download"
        self.download.mkdir()
        self.source = self.download / "boards.txt"
        self.source.write_text("future.name=Future Board\nfuture.build.mcu=newmcu\n", encoding="utf-8")
        self.cli = self.fixture / "arduino-cli.exe"
        self.cli.write_bytes(b"fixture")
        self.platform = self.core / "arduino-cli/data/packages/vendor/hardware/newarch/2.0.0"
        self.platform.mkdir(parents=True)
        (self.platform / "boards.txt").write_text(self.source.read_text(), encoding="utf-8")
        (self.platform / "platform.txt").write_text("name=Future core\n", encoding="utf-8")
        self.tool = self.core / "arduino-cli/data/packages/vendor/tools/compiler/1.0.0/bin/compiler.exe"
        self.tool.parent.mkdir(parents=True)
        self.tool.write_bytes(b"compiler bytes")
        self.row = {"name": "Future Board", "arduino_id": "future", "source_file": str(self.source),
                    "source_sha256": support.sha256_file(self.source),
                    "status": "unavailable", "reason": "No exact PlatformIO target", "platformio_support": "unsupported",
                    "platformio_support_proof": dict(PROOF)}
        self.metadata = {"package": "vendor", "architecture": "newarch", "version": "2.0.0",
                         "index_url": "https://example.invalid/vendor_index.json"}
        self.cli_preferences = {selection.association_key(self.metadata): ["future", "future2", "second"]}
        self.stack.enter_context(patch.object(selection, "load_preferences",
                                             side_effect=lambda **_kwargs: dict(self.cli_preferences)))
        self.stack.enter_context(patch.object(support, "load_preferences",
                                             side_effect=lambda **_kwargs: dict(self.cli_preferences)))
        self.stack.enter_context(patch.object(toolchain, "find_arduino_cli_executable", return_value=str(self.cli)))
        self.stack.enter_context(patch("main.core.build_resources.get_optimal_compiler_jobs", return_value=2))
        self.stack.enter_context(patch("src.modules.package_jobs.package_core_directory", return_value=self.core))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=False))
        os.environ.pop("MCU_FLASHER_OFFLINE_RUNTIME", None)
        self.commands = []
        def run(command, **kwargs):
            self.commands.append(command)
            if "core" in command and "list" in command:
                return {"platforms": [{"id": "vendor:newarch", "installed_version": "2.0.0"}]}
            if "details" in command:
                return {"fqbn": command[command.index("--fqbn") + 1], "tools_dependencies": [{"packager": "vendor", "name": "compiler", "version": "1.0.0"}]}
            return None
        self.runner = self.stack.enter_context(patch.object(support, "_run_json", side_effect=run))
        self.emit = Mock()

    def ready(self):
        row = support.prepare_unsupported_boards(self.core, self.download, self.metadata, [self.row], emit=self.emit, jobs=2)[0]
        self.assertEqual(row["status"], "ready", row.get("reason"))
        support.publish_prepared_targets(self.core, self.download, [row])
        return row

    def record(self):
        return {"name": "Future Board", "arduino_id": "future", "source_file": str(self.source), "source_sha256": self.row["source_sha256"], "mcu": "newmcu", "variant": "", "build_board": ""}

    def info(self):
        return support.arduino_catalog_entry(self.record(), self.ready())

    def test_supported_unknown_and_missing_proof_never_install_or_fallback(self):
        for changes in ({"platformio_support": "supported"}, {"platformio_support": "unknown"}, {"platformio_support_proof": {}}, {"platformio_support_proof": dict(PROOF, catalog_count=0)}):
            row = dict(self.row, **changes)
            actual = support.prepare_unsupported_boards(self.core, self.download, self.metadata, [row], emit=self.emit)
            self.assertEqual(actual, [row])
        self.runner.assert_not_called()

    def test_default_disabled_never_installs_despite_verified_absence(self):
        self.cli_preferences.clear()
        rows = support.prepare_unsupported_boards(
            self.core, self.download, self.metadata, [self.row], emit=self.emit)
        self.assertNotEqual(rows[0]["status"], "ready")
        self.runner.assert_not_called()
        self.assertFalse((self.core / "arduino-cli/arduino-cli.yaml").exists())

    def test_one_selected_board_does_not_prepare_its_siblings(self):
        self.cli_preferences[selection.association_key(self.metadata)] = ["future"]
        rows = support.prepare_unsupported_boards(
            self.core, self.download, self.metadata,
            [self.row, dict(self.row, arduino_id="future2", name="Future Board 2")], emit=self.emit)
        self.assertEqual(rows[0]["status"], "ready")
        self.assertNotEqual(rows[1]["status"], "ready")
        probes = [command for command in self.commands if "compile" in command]
        self.assertEqual([command[command.index("--fqbn") + 1] for command in probes],
                         ["vendor:newarch:future"])

    def test_removing_selection_revokes_warm_receipt_and_runtime_without_deleting_tools(self):
        row = self.ready()
        info = support.arduino_catalog_entry(self.record(), row)
        validation_cache = {}
        self.assertIsNotNone(support.prepared_target_for_record(
            self.record(), [], core=self.core, validation_cache=validation_cache))
        self.cli_preferences.clear()
        self.assertIsNone(support.prepared_target_for_record(
            self.record(), [], core=self.core, validation_cache=validation_cache))
        with patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[]):
            with self.assertRaises(RuntimeError):
                support.runtime_command(info, core=self.core)
        self.assertTrue(self.tool.exists())
        self.assertTrue((self.core / support.TARGETS_FILE).exists())

    def test_old_namespace_free_receipt_cannot_enable_cli(self):
        row = self.ready()
        row.pop("arduino_cli_selection", None)
        support.publish_prepared_targets(self.core, self.download, [row])
        self.assertIsNone(support.prepared_target_for_record(self.record(), [], core=self.core))

    def test_warm_certificate_cannot_be_relabelled_to_another_selected_index(self):
        row = self.ready()
        another = dict(self.metadata, index_url="https://another.invalid/vendor_index.json")
        self.cli_preferences[selection.association_key(another)] = ["future"]
        validation_cache = {}
        self.assertIsNotNone(support.prepared_target_for_record(
            self.record(), [], core=self.core, validation_cache=validation_cache))
        relabelled = dict(row, arduino_cli_selection=selection.selection_identity(another))
        support.publish_prepared_targets(self.core, self.download, [relabelled])
        self.assertTrue(selection.selection_for_row(relabelled, self.cli_preferences))
        self.assertIsNone(support.prepared_target_for_record(
            self.record(), [], core=self.core, validation_cache=validation_cache))

    def test_cached_identity_without_exact_parsed_receipt_cannot_borrow_prepared_target(self):
        self.ready()
        for receipt in (None, "invalid", "0" * 64):
            record = dict(self.record(), source_sha256=receipt)
            self.assertIsNone(support.prepared_target_for_record(record, [], core=self.core))

    def test_missing_cli_guidance_matches_native_host(self):
        with patch.object(support.sys, "platform", "linux"):
            self.assertIn("Ubuntu Bootstrap", support._missing_cli_message())
            self.assertIn("bash direct/ubuntu/run.sh --repair", support._missing_cli_message())
        with patch.object(support.sys, "platform", "win32"):
            self.assertIn("Repair Bootstrap", support._missing_cli_message())

    def test_exact_core_install_and_compile_probe_without_upload(self):
        row = self.ready()
        self.assertEqual(row["arduino_fqbn"], "vendor:newarch:future")
        self.assertEqual(row["backend"], "arduino-cli")
        self.assertEqual(row["platform"], "vendor:newarch")
        install = next(command for command in self.commands if "install" in command)
        self.assertIn("vendor:newarch@2.0.0", install)
        probe = next(command for command in self.commands if "compile" in command)
        self.assertEqual(probe[probe.index("--fqbn") + 1], row["arduino_fqbn"])
        self.assertEqual(probe[probe.index("--jobs") + 1], "2")
        self.assertFalse(any("upload" in command for command in self.commands))
        self.assertTrue(any("PlatformIO does not support" in str(call) for call in self.emit.call_args_list))

    def test_fallback_board_compile_probes_run_in_parallel_with_shared_job_budget(self):
        barrier = threading.Barrier(2)
        compile_jobs = []

        def run_parallel(command, **kwargs):
            if "core" in command and "list" in command:
                return {"platforms": [{"id": "vendor:newarch", "installed_version": "2.0.0"}]}
            if "details" in command:
                return {"fqbn": command[command.index("--fqbn") + 1],
                        "tools_dependencies": [{"packager": "vendor", "name": "compiler", "version": "1.0.0"}]}
            if "compile" in command:
                compile_jobs.append(command[command.index("--jobs") + 1])
                barrier.wait(timeout=5)
            return None

        self.runner.side_effect = run_parallel
        targets = [dict(self.row), dict(self.row, arduino_id="future2", name="Future Board 2")]
        prepared = support.prepare_unsupported_boards(
            self.core, self.download, self.metadata, targets, emit=self.emit, jobs=2)

        self.assertEqual([row["status"] for row in prepared], ["ready", "ready"])
        self.assertEqual(compile_jobs, ["1", "1"])
        self.assertTrue(any("2 workers" in str(call) for call in self.emit.call_args_list))
        self.assertTrue(any("2/2" in str(call) for call in self.emit.call_args_list))

    def test_probe_jobs_respect_resource_budget_and_reject_invalid_values(self):
        rows = support.prepare_unsupported_boards(self.core, self.download, self.metadata, [self.row], emit=self.emit, jobs=80)
        self.assertEqual(rows[0]["status"], "ready")
        probe = next(command for command in self.commands if "compile" in command)
        self.assertEqual(probe[probe.index("--jobs") + 1], "2")
        for jobs in (0, -1, True, "2"):
            self.runner.reset_mock()
            rows = support.prepare_unsupported_boards(self.core, self.download, self.metadata, [self.row], emit=self.emit, jobs=jobs)
            self.assertEqual(rows[0]["status"], "unavailable")
            self.assertIn("positive integer", rows[0]["reason"])
            self.runner.assert_not_called()

    def test_one_board_cannot_borrow_another_exact_compile_certificate(self):
        row = self.ready()
        validation_cache = {}
        self.assertIsNotNone(support.prepared_target_for_record(self.record(), [], core=self.core, validation_cache=validation_cache))
        borrowed = dict(row, arduino_id="second", board="second", arduino_fqbn="vendor:newarch:second",
                        arduino_cli=dict(row["arduino_cli"], fqbn="vendor:newarch:second"))
        support.publish_prepared_targets(self.core, self.download, [borrowed])
        record = dict(self.record(), arduino_id="second")
        self.assertIsNone(support.prepared_target_for_record(record, [], core=self.core, validation_cache=validation_cache))

    def test_failure_keeps_other_ready_platformio_rows(self):
        supported = {"status": "ready", "backend": "platformio", "name": "Supported"}
        self.runner.side_effect = RuntimeError("download failed")
        rows = support.prepare_unsupported_boards(self.core, self.download, self.metadata, [supported, self.row], emit=self.emit)
        self.assertEqual(rows[0], supported)
        self.assertEqual(rows[1]["status"], "unavailable")
        self.assertIn("download failed", rows[1]["reason"])

    def test_failed_exact_compile_probe_never_certifies_ready(self):
        original = self.runner.side_effect
        self.runner.side_effect = lambda command, **kw: (_ for _ in ()).throw(RuntimeError("compiler unavailable")) if "compile" in command else original(command, **kw)
        rows = support.prepare_unsupported_boards(self.core, self.download, self.metadata, [self.row], emit=self.emit)
        self.assertEqual(rows[0]["status"], "unavailable")
        self.assertIn("compiler unavailable", rows[0]["reason"])

    def test_wrong_installed_core_version_never_becomes_ready(self):
        original = self.runner.side_effect
        self.runner.side_effect = lambda command, **kw: {"platforms": [{"id": "vendor:newarch", "installed_version": "1.0.0"}]} if "core" in command and "list" in command else original(command, **kw)
        row = support.prepare_unsupported_boards(self.core, self.download, self.metadata, [self.row], emit=self.emit)[0]
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("exact installed core", row["reason"])
        self.assertFalse(any("compile" in command for command in self.commands))

    def test_changed_installed_core_version_and_cli_require_preparation(self):
        info = self.info()
        original = self.runner.side_effect
        self.runner.side_effect = lambda command, **kw: {"platforms": [{"id": "vendor:newarch", "installed_version": "3.0.0"}]} if "core" in command and "list" in command else original(command, **kw)
        with self.assertRaisesRegex(RuntimeError, "exact installed core"):
            support.runtime_command(info, core=self.core)
        self.runner.side_effect = original
        self.cli.write_bytes(b"changed CLI")
        with self.assertRaisesRegex(RuntimeError, "executable changed"):
            support.runtime_command(info, core=self.core)

    def test_ready_proof_is_bound_to_source_core_and_tools(self):
        row = self.ready()
        resolved = support.prepared_target_for_record(self.record(), [], core=self.core)
        self.assertEqual(resolved["arduino_fqbn"], row["arduino_fqbn"])
        previous = self.source.stat()
        text = self.source.read_text().replace("newmcu", "oldmcu")
        self.source.write_text(text, encoding="utf-8")
        os.utime(self.source, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertIsNone(support.prepared_target_for_record(self.record(), [], core=self.core))

    def test_same_size_timestamp_preserved_tool_change_invalidates(self):
        self.ready()
        stat = self.tool.stat()
        self.tool.write_bytes(b"different tool")
        self.assertEqual(self.tool.stat().st_size, stat.st_size)
        os.utime(self.tool, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertIsNone(support.prepared_target_for_record(self.record(), [], core=self.core))

    def test_missing_tool_and_changed_core_invalidate(self):
        self.ready()
        self.tool.unlink()
        self.assertIsNone(support.prepared_target_for_record(self.record(), [], core=self.core))
        self.tool.write_bytes(b"compiler bytes")
        (self.platform / "platform.txt").write_text("name=Other core\n", encoding="utf-8")
        self.assertIsNone(support.prepared_target_for_record(self.record(), [], core=self.core))

    def test_new_unavailable_result_removes_previous_fallback(self):
        self.ready()
        support.publish_prepared_targets(self.core, self.download, [self.row])
        self.assertEqual(support.load_prepared_targets(self.core), [])

    def test_publisher_never_certifies_old_identity_against_changed_source(self):
        row = self.ready()
        self.source.write_text("future.name=Different Board\nfuture.build.mcu=othermcu\n", encoding="utf-8")
        support.publish_prepared_targets(self.core, self.download, [row])
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("changed during preparation", row["reason"])
        self.assertEqual(support.load_prepared_targets(self.core), [])

    def test_missing_parsed_receipt_cannot_publish_ready(self):
        row = self.ready()
        row.pop("source_sha256")
        support.publish_prepared_targets(self.core, self.download, [row])
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(support.load_prepared_targets(self.core), [])

    def test_publisher_rejects_changed_verified_platformio_manifest(self):
        manifest = self.core / "platforms/custom/boards/authorized.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{}', encoding="utf-8")
        row = dict(self.row, status="ready", backend="platformio", platformio_support="supported", platform="custom", board="authorized",
                   manifest=str(manifest), manifest_sha256=support.sha256_file(manifest))
        manifest.write_text('{ }', encoding="utf-8")
        support.publish_prepared_targets(self.core, self.download, [row])
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(support.load_prepared_targets(self.core), [])

    def test_runtime_configuration_and_environment_cannot_select_global_store(self):
        info = self.info()
        command, environment, fqbn = support.runtime_command(info, core=self.core)
        self.assertEqual(fqbn, "vendor:newarch:future")
        self.assertIn(str(self.core / "arduino-cli/arduino-cli.yaml"), command)
        self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:9")
        config = self.core / "arduino-cli/arduino-cli.yaml"
        payload = json.loads(config.read_text())
        payload["directories"]["data"] = str(self.fixture / "user-store")
        config.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "configuration changed"):
            support.runtime_command(info, core=self.core)

    def test_local_platformio_definition_supersedes_fallback(self):
        info = self.info()
        candidate = {"id": "future", "name": "Future Board", "platform": "newpio", "frameworks": {"arduino"}, "mcu": "newmcu", "manifest": "", "hwids": set()}
        actual = board_catalog.resolve_board_definition("Future Board", info, [candidate])
        self.assertEqual(actual["backend"], "platformio")
        self.assertEqual(actual["board"], "future")
        self.assertNotIn("arduino_cli", actual)

    def test_other_framework_or_ambiguity_prevents_fallback(self):
        info = self.info()
        candidate = {"id": "future", "name": "Future Board", "platform": "newpio", "frameworks": {"zephyr"}, "mcu": "newmcu", "hwids": set()}
        actual = board_catalog.resolve_board_definition("Future Board", info, [candidate])
        self.assertNotEqual(actual.get("backend"), "arduino-cli")
        self.assertTrue(target_profile.target_problem(actual, arduino_sketch=True))
        candidate["frameworks"] = {"arduino"}
        actual = board_catalog.resolve_board_definition("Future Board", info, [candidate, dict(candidate, id="future-second")])
        self.assertNotEqual(actual.get("backend"), "arduino-cli")

    def test_invalid_proof_and_framework_rejected(self):
        info = self.info()
        self.assertEqual(target_profile.target_problem(info, arduino_sketch=True), "")
        self.assertTrue(target_profile.target_problem(dict(info, platformio_support="unknown")))
        self.assertTrue(target_profile.target_problem(dict(info, framework="zephyr")))
        with self.assertRaises(RuntimeError):
            support.runtime_command(dict(info, platformio_support="unknown"), core=self.core)

    def test_exact_explicit_platformio_association_is_hash_checked(self):
        manifest = self.core / "platforms/custom/boards/authorized.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{}', encoding="utf-8")
        row = dict(self.row, status="ready", backend="platformio", platformio_support="supported", platform="custom", board="authorized",
                   manifest=str(manifest), manifest_sha256=support.sha256_file(manifest), match_reasons=["explicit-index-board-id"])
        support.publish_prepared_targets(self.core, self.download, [row])
        catalog = [{"platform": "custom", "id": "authorized", "manifest": str(manifest), "frameworks": {"arduino"}}]
        self.assertEqual(support.prepared_target_for_record(self.record(), catalog, core=self.core)["id"], "authorized")
        catalog[0]["mcu"] = "othermcu"
        self.assertIsNone(support.prepared_target_for_record(self.record(), catalog, core=self.core))
        catalog[0]["mcu"] = "newmcu"
        manifest.write_text('{ }', encoding="utf-8")
        self.assertIsNone(support.prepared_target_for_record(self.record(), catalog, core=self.core))

    def api(self):
        info = self.info()
        sketch = self.fixture / "sketch"
        sketch.mkdir(exist_ok=True)
        (sketch / "sketch.ino").write_text("void setup(){}\nvoid loop(){}\n", encoding="utf-8")
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        api.current_board = "Future Board"
        api.current_port = "SIMULATED"
        api.sketch_dir_path = sketch
        api._active_port_label = "SIMULATED"
        api._active_skip_compile = False
        api._stop_requested = False
        api._active_process = None
        api._resolve_board_info = Mock(return_value=info)
        api._effective_cache_root = Mock(return_value=self.fixture / "build-cache")
        api._board_workspace_dir = Mock(side_effect=lambda board=None: (
            self.fixture / "build-cache/boards/fixture-native-host" /
            api._board_cache_key(board)))
        api.modified_files = {}
        api.update_skip_compile_availability = Mock()
        api._get_jobs = Mock(return_value=2)
        api.emit = Mock()
        api._stop_serial_monitor = Mock()
        api._start_serial_monitor = Mock()
        api._release_requested_operation = Mock()
        api._compile_worker = Mock()
        self.operations = []
        def stream(owner, command, environment, cwd, *, upload=False):
            self.operations.append((command, upload))
            if not upload:
                build = Path(command[command.index("--build-path") + 1])
                (build / "FallbackSketch.ino.hex").write_bytes(b"exact fixture firmware")
        self.stream = self.stack.enter_context(patch.object(arduino_backend, "_stream", side_effect=stream))
        self.stack.enter_context(patch.object(board_catalog, "_get_download_dir", return_value=str(self.fixture / "libraries")))
        self.stack.enter_context(patch.object(web_bridge, "port_occupied_owner", return_value=None))
        return api

    def test_compile_stages_root_sources_and_does_not_use_platformio(self):
        api = self.api()
        libraries = self.fixture / "libraries/Libs"
        libraries.mkdir(parents=True)
        self.assertTrue(arduino_backend.run_arduino_operation(api))
        self.assertEqual(len(self.operations), 1)
        command, upload = self.operations[0]
        self.assertFalse(upload)
        self.assertEqual(command[command.index("--fqbn") + 1], "vendor:newarch:future")
        self.assertNotIn("--upload", command)
        self.assertEqual(command[command.index("--libraries") + 1], str(libraries))
        self.assertTrue(any("PlatformIO does not support" in str(call) for call in api.emit.call_args_list))
        api._compile_worker.assert_not_called()
        api._release_requested_operation.assert_called_once()

    def test_transient_staging_changes_cannot_certify_different_source_bytes(self):
        api = self.api()
        def wrong_stage(source, destination):
            destination.write_bytes(b"void setup(){int changed=1;} void loop(){}\n")
        api._stage_root_source_file = wrong_stage
        self.assertFalse(arduino_backend.run_arduino_operation(api))
        self.assertEqual(self.operations, [])
        api._release_requested_operation.assert_called_once()

    def test_source_change_then_reversion_during_staging_never_compiles_other_bytes(self):
        api = self.api()
        source_path = api.sketch_dir_path / "sketch.ino"
        original_bytes = source_path.read_bytes()
        def transient_stage(source, destination):
            changed = b"void setup(){int changed=1;} void loop(){}\n"
            source.write_bytes(changed)
            destination.write_bytes(changed)
        api._stage_root_source_file = transient_stage
        original_hash = api._hash_sources
        hash_calls = []
        def reverted_hash(*args, **kwargs):
            hash_calls.append(kwargs)
            if len(hash_calls) == 2:
                source_path.write_bytes(original_bytes)
            return original_hash(*args, **kwargs)
        api._hash_sources = reverted_hash
        self.assertFalse(arduino_backend.run_arduino_operation(api))
        self.assertEqual(self.operations, [])
        self.assertEqual(source_path.read_bytes(), original_bytes)

    def test_staged_input_change_during_compile_cannot_create_build_receipt(self):
        api = self.api()
        original = self.stream.side_effect
        def stream(owner, command, environment, cwd, **kwargs):
            original(owner, command, environment, cwd, **kwargs)
            (Path(command[command.index("--build-path") + 1]).parent / "sketch/FallbackSketch/FallbackSketch.ino").write_text("different input", encoding="utf-8")
        self.stream.side_effect = stream
        self.assertFalse(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertFalse(any(upload for _, upload in self.operations))
        self.assertEqual(list((self.fixture / "build-cache").rglob("build-receipt.json")), [])

    def test_upload_reuses_only_byte_verified_build(self):
        api = self.api()
        self.assertTrue(arduino_backend.run_arduino_operation(api))
        api._active_skip_compile = True
        self.operations.clear()
        self.assertTrue(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual(len(self.operations), 1)
        self.assertTrue(self.operations[0][1])
        self.assertIn("--input-dir", self.operations[0][0])
        api._stop_serial_monitor.assert_called_once()
        api._start_serial_monitor.assert_called_once()

    def test_cached_status_does_not_create_unbuilt_workspace(self):
        api = self.api()
        self.assertTrue(arduino_backend.cached_build_status(api)[0])
        self.assertFalse((self.fixture / "build-cache").exists())
        self.assertEqual(self.operations, [])
        api._stop_serial_monitor.assert_not_called()

    def test_each_selectable_board_retains_its_cli_build_when_switched_back(self):
        api = self.api()
        first = api.current_board
        self.assertTrue(arduino_backend.run_arduino_operation(api))
        first_build = Path(self.operations[-1][0][self.operations[-1][0].index("--build-path") + 1])
        first_receipt = first_build.parent / "build-receipt.json"
        first_receipt_bytes = first_receipt.read_bytes()
        first_firmware_bytes = (first_build / "FallbackSketch.ino.hex").read_bytes()
        self.assertFalse(arduino_backend.cached_build_status(api)[0])
        api.update_skip_compile_availability.assert_called_once()

        # Selectable aliases still require distinct folders even when their
        # certified FQBN and native toolchain are identical.
        api.current_board = "Other selectable board"
        self.assertTrue(arduino_backend.cached_build_status(api)[0])
        self.assertTrue(arduino_backend.run_arduino_operation(api))
        second_build = Path(self.operations[-1][0][self.operations[-1][0].index("--build-path") + 1])
        self.assertNotEqual(first_build, second_build)
        self.assertTrue(first_build.is_relative_to(api._board_workspace_dir(first)))
        self.assertTrue(second_build.is_relative_to(api._board_workspace_dir(api.current_board)))
        self.assertEqual(first_receipt.read_bytes(), first_receipt_bytes)
        self.assertEqual((first_build / "FallbackSketch.ino.hex").read_bytes(), first_firmware_bytes)

        api.current_board = first
        self.assertFalse(arduino_backend.cached_build_status(api)[0])
        api._active_skip_compile = True
        self.operations.clear()
        self.assertTrue(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual([upload for _, upload in self.operations], [True])
        self.assertEqual(self.operations[0][0][self.operations[0][0].index("--input-dir") + 1], str(first_build))

    def test_cached_status_rechecks_dirty_source_and_firmware_bytes(self):
        api = self.api()
        self.assertTrue(arduino_backend.run_arduino_operation(api))
        self.assertFalse(arduino_backend.cached_build_status(api)[0])
        source = api.sketch_dir_path / "sketch.ino"
        source_bytes, previous = source.read_bytes(), source.stat()
        api.modified_files = {str(source): True}
        self.assertTrue(arduino_backend.cached_build_status(api)[0])
        api.modified_files.clear()
        source.write_bytes(source_bytes.replace(b"setup", b"other"))
        os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertTrue(arduino_backend.cached_build_status(api)[0])
        source.write_bytes(source_bytes)
        self.assertFalse(arduino_backend.cached_build_status(api)[0])
        build = Path(self.operations[-1][0][self.operations[-1][0].index("--build-path") + 1])
        (build / "FallbackSketch.ino.hex").write_bytes(b"different fixture firmware")
        self.assertTrue(arduino_backend.cached_build_status(api)[0])

    def test_cached_status_rechecks_selected_library_and_prepared_cli(self):
        api = self.api()
        library = self.core / "lib/StatusLibrary/src"
        library.mkdir(parents=True)
        header = library / "StatusLibrary.h"
        header.write_bytes(b"const int value = 1;\n")
        self.assertTrue(arduino_backend.run_arduino_operation(api))
        self.assertFalse(arduino_backend.cached_build_status(api)[0])
        previous = header.stat()
        header.write_bytes(b"const int value = 2;\n")
        os.utime(header, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertTrue(arduino_backend.cached_build_status(api)[0])
        header.write_bytes(b"const int value = 1;\n")
        self.assertFalse(arduino_backend.cached_build_status(api)[0])
        self.cli.write_bytes(b"foreign CLI bytes")
        self.assertTrue(arduino_backend.cached_build_status(api)[0])
        self.assertEqual([upload for _, upload in self.operations], [False])

    def test_cli_status_does_not_accept_legacy_shared_build_receipt(self):
        api = self.api()
        self.assertTrue(arduino_backend.run_arduino_operation(api))
        build = Path(self.operations[-1][0][self.operations[-1][0].index("--build-path") + 1])
        old_workspace = self.fixture / "build-cache/arduino-cli" / build.parent.name
        old_workspace.parent.mkdir(parents=True)
        build.parent.rename(old_workspace)
        self.assertTrue(arduino_backend.cached_build_status(api)[0])
        self.assertTrue((old_workspace / "build-receipt.json").is_file())
        self.assertFalse(build.exists())

    def test_upload_failure_is_single_attempt_and_never_replayed(self):
        api = self.api()
        original = self.stream.side_effect
        def stream(*args, **kwargs):
            original(*args, **kwargs)
            if kwargs.get("upload"):
                raise RuntimeError("write failed")
        self.stream.side_effect = stream
        self.assertFalse(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual(sum(upload for _, upload in self.operations), 1)
        self.assertTrue(any("firmware may be incomplete" in str(call) for call in api.emit.call_args_list))
        api._compile_worker.assert_not_called()

    def test_source_or_port_change_during_compile_never_uploads(self):
        api = self.api()
        original = self.stream.side_effect
        def stream(*args, **kwargs):
            original(*args, **kwargs)
            (api.sketch_dir_path / "sketch.ino").write_text("void setup(){int x=1;} void loop(){}", encoding="utf-8")
        self.stream.side_effect = stream
        self.assertFalse(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertFalse(any(upload for _, upload in self.operations))

    def test_port_change_during_compile_never_uploads(self):
        api = self.api()
        original = self.stream.side_effect
        def stream(*args, **kwargs):
            original(*args, **kwargs)
            api.current_port = "CHANGED"
        self.stream.side_effect = stream
        self.assertFalse(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertFalse(any(upload for _, upload in self.operations))

    def test_port_change_during_final_cli_validation_never_uploads(self):
        api = self.api()
        original = arduino_backend.runtime_command
        calls = []
        def validate(info):
            result = original(info)
            calls.append(info)
            if len(calls) == 2:
                api.current_port = "CHANGED"
            return result
        with patch.object(arduino_backend, "runtime_command", side_effect=validate):
            self.assertFalse(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertFalse(any(upload for _, upload in self.operations))

    def test_stop_during_final_cli_validation_never_uploads(self):
        api = self.api()
        original = arduino_backend.runtime_command
        calls = []
        def validate(info):
            result = original(info)
            calls.append(info)
            if len(calls) == 2:
                api._stop_requested = True
            return result
        with patch.object(arduino_backend, "runtime_command", side_effect=validate):
            self.assertFalse(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertFalse(any(upload for _, upload in self.operations))

    def test_new_platformio_support_during_compile_prevents_fallback_write(self):
        api = self.api()
        current_catalog = []
        candidate = {"id": "future", "name": "Future Board", "platform": "newpio", "frameworks": {"arduino"}, "mcu": "newmcu"}
        original = self.stream.side_effect
        def stream(*args, **kwargs):
            original(*args, **kwargs)
            current_catalog.append(candidate)
        self.stream.side_effect = stream
        with patch.object(board_catalog, "_load_platformio_board_catalog", side_effect=lambda *args, **kw: list(current_catalog)):
            self.assertFalse(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertFalse(any(upload for _, upload in self.operations))

    def test_actual_entry_workers_only_route_certified_backend(self):
        api = self.api()
        api._resolve_requested_target = Mock(return_value=True)
        with patch("main.core.arduino_backend.run_arduino_operation", return_value=True) as operation:
            web_bridge.MCUWebBackendAPI._compile_requested_worker.__wrapped__(api)
            operation.assert_called_once_with(api)
        api._compile_worker.assert_not_called()
        info = dict(api._resolve_board_info())
        info.update(backend="platformio", platform="newpio", board="future", pio_resolved=True)
        api._resolve_board_info.return_value = info
        with patch("main.core.arduino_backend.run_arduino_operation") as operation:
            web_bridge.MCUWebBackendAPI._compile_requested_worker.__wrapped__(api)
            operation.assert_not_called()
        api._compile_worker.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
