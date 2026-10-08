#!/usr/bin/env python3
"""Hardware-free original Arduino source preparation and certificate checks."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
AUDIT = ROOT / "temp/audit/arduino-source-targets"
AUDIT.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(dir=AUDIT) as _import_directory, \
        patch("src.modules.platform_runtime.app_cache_dir", return_value=Path(_import_directory)):
    from src.modules import arduino_cli_support as support
    from main.core import board_catalog, target_profile, toolchain


class SourceTargetChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.fixture = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=AUDIT)))
        self.core = self.fixture / "store"
        self.download = self.fixture / "source"
        self.download.mkdir()
        self.source = self.download / "boards.txt"
        self.source.write_text("""esp32s3.name=ESP32S3 Dev Module
esp32s3.build.mcu=esp32s3
esp32s3.build.variant=esp32s3
esp32s3.build.board=ESP32S3_DEV
esp32s3.build.flash_size=4MB
esp32c3.name=ESP32C3 Dev Module
esp32c3.build.mcu=esp32c3
esp32c3.build.variant=esp32c3
esp32c3.build.board=ESP32C3_DEV
""", encoding="utf-8")
        self.record = board_catalog._parse_downloaded_arduino_board_files(self.download, force_read=True)[0]
        self.row = {key: self.record[key] for key in ("name", "arduino_id", "source_file", "source_sha256")}
        self.row.update(status="ambiguous", backend="platformio", platformio_support="unknown")
        self.metadata = {"package": "esp32", "architecture": "esp32", "version": "3.3.11",
                         "index_url": "https://example.invalid/verified-source-index.json"}
        self.store = support._source_store(self.core, support.source_declaration_proof(self.record, self.metadata))
        self.platform = self.store / "data/packages/esp32/hardware/esp32/3.3.11"
        self.platform.mkdir(parents=True)
        (self.platform / "boards.txt").write_bytes(self.source.read_bytes())
        (self.platform / "platform.txt").write_text("name=ESP32 Arduino\nversion=3.3.11\n", encoding="utf-8")
        self.pins = self.platform / "variants/esp32s3/pins_arduino.h"
        self.pins.parent.mkdir(parents=True)
        self.pins.write_text("#define LED_BUILTIN 48\n")
        self.core_source = self.platform / "cores/esp32/core.cpp"
        self.core_source.parent.mkdir(parents=True)
        self.core_source.write_text("void fixture_core() {}\n")
        self.bootloader = self.platform / "tools/bootloader.bin"
        self.bootloader.parent.mkdir(parents=True)
        self.bootloader.write_bytes(b"fixture bootloader")
        self.tool = self.store / "data/packages/esp32/tools/compiler/1.0.0/bin/compiler.exe"
        self.tool.parent.mkdir(parents=True)
        self.tool.write_bytes(b"compiler bytes")
        self.cli = self.fixture / "arduino-cli.exe"
        self.cli.write_bytes(b"CLI bytes")
        self.stack.enter_context(patch.object(toolchain, "find_arduino_cli_executable", return_value=str(self.cli)))
        self.stack.enter_context(patch("main.core.build_resources.get_optimal_compiler_jobs", return_value=2))
        self.stack.enter_context(patch("src.modules.package_jobs.package_core_directory", return_value=self.core))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=False))
        os.environ.pop("MCU_FLASHER_OFFLINE_RUNTIME", None)
        self.commands = []
        def run(command, **kwargs):
            self.commands.append((command, kwargs))
            if "core" in command and "list" in command:
                configuration = Path(command[command.index("--config-file") + 1])
                data = Path(json.loads(configuration.read_text())["directories"]["data"])
                versions = list((data / "packages/esp32/hardware/esp32").iterdir())
                return {"platforms": [{"id": "esp32:esp32", "installed_version": path.name} for path in versions]}
            if "details" in command:
                return {"fqbn": command[command.index("--fqbn") + 1],
                        "tools_dependencies": [{"packager": "esp32", "name": "compiler", "version": "1.0.0"}]}
            return None
        self.runner = self.stack.enter_context(patch.object(support, "_run_json", side_effect=run))
        self.emit = Mock()

    def ready(self, row=None):
        result = support.prepare_source_boards(self.core, self.download, self.metadata,
                                              [row or self.row], emit=self.emit, jobs=2)[0]
        self.assertEqual(result["status"], "ready", result.get("reason"))
        support.publish_prepared_targets(self.core, self.download, [result])
        return result

    def candidates(self):
        shared = {"platform": "espressif32", "frameworks": {"arduino", "espidf"}, "mcu": "esp32s3",
                  "variant": "esp32s3", "arduino_defines": {"arduinoesp32s3dev"}, "vendor": "Espressif",
                  "hwids": set(), "flash_size": "8MB"}
        return [dict(shared, id="esp32-s3-devkitc-1", name="Espressif ESP32-S3-DevKitC-1-N8 (8 MB QD, No PSRAM)"),
                dict(shared, id="esp32-s3-devkitm-1", name="Espressif ESP32-S3-DevKitM-1")]

    def test_original_generic_source_is_prepared_without_platformio_absence_claim(self):
        result = self.ready()
        self.assertTrue(support.source_target_proof(result))
        self.assertFalse(support.unsupported_proof(result))
        self.assertEqual(result["platformio_support"], "unknown")
        self.assertEqual(result["backend"], "arduino-cli")
        self.assertEqual(result["board"], "esp32s3")
        self.assertEqual(result["arduino_fqbn"], "esp32:esp32:esp32s3")
        self.assertIn("original Arduino source target", result["reason"])
        self.assertFalse(any("does not support" in str(call) for call in self.emit.call_args_list))
        install = next(command for command, _ in self.commands if "install" in command)
        self.assertIn("esp32:esp32@3.3.11", install)
        probe = next(command for command, _ in self.commands if "compile" in command)
        self.assertEqual(probe[probe.index("--fqbn") + 1], "esp32:esp32:esp32s3")
        self.assertFalse(any("upload" in command for command, _ in self.commands))

    def test_source_primary_does_not_override_a_resolved_platformio_definition(self):
        result = self.ready()
        candidates = self.candidates()
        match = board_catalog._resolve_arduino_board_record(self.record, candidates)
        self.assertIsNotNone(match)
        prepared = support.prepared_target_for_record(self.record, candidates, core=self.core)
        self.assertEqual(prepared["arduino_fqbn"], result["arduino_fqbn"])
        info = support.arduino_catalog_entry(self.record, prepared)
        resolved = board_catalog.resolve_board_definition(self.record["name"], info, candidates)
        self.assertEqual(resolved["backend"], "platformio")
        self.assertEqual(resolved["board"], match["id"])
        self.assertNotIn("arduino_cli", resolved)
        self.assertEqual(target_profile.target_problem(resolved, arduino_sketch=True), "")

    def test_each_source_declaration_keeps_its_own_fqbn(self):
        record = board_catalog._parse_downloaded_arduino_board_files(self.download, force_read=True)[1]
        row = dict(self.row, **{key: record[key] for key in ("name", "arduino_id", "source_file", "source_sha256")})
        result = self.ready(row)
        self.assertEqual(result["arduino_fqbn"], "esp32:esp32:esp32c3")
        self.assertTrue(support.source_target_proof(result))
        self.assertIsNotNone(support.prepared_target_for_record(record, [], core=self.core))
        self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))

    def test_installed_definition_bytes_must_equal_the_original_source(self):
        (self.platform / "boards.txt").write_text("esp32s3.name=Different defaults\n", encoding="utf-8")
        result = support.prepare_source_boards(self.core, self.download, self.metadata, [self.row], emit=self.emit)[0]
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("original source declaration bytes", result["reason"])
        self.assertFalse(any("compile" in command for command, _ in self.commands))

    def test_changed_original_declarations_never_get_a_ready_certificate(self):
        self.source.write_text(self.source.read_text().replace("4MB", "8MB"), encoding="utf-8")
        result = support.prepare_source_boards(self.core, self.download, self.metadata, [self.row], emit=self.emit)[0]
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("changed before preparation", result["reason"])
        self.assertFalse(any("compile" in command for command, _ in self.commands))

    def test_invalid_identity_receipts_never_authorize_core_installation(self):
        for changes in ({"source_sha256": None}, {"source_sha256": "invalid"}, {"arduino_id": "../../other"}):
            with self.subTest(changes=changes):
                result = support.prepare_source_boards(self.core, self.download, self.metadata,
                                                      [dict(self.row, **changes)], emit=self.emit)[0]
                self.assertEqual(result["status"], "unavailable")
        self.runner.assert_not_called()

    def test_primary_policy_cannot_replace_ready_or_explicit_platformio_mappings(self):
        for changes in ({"platformio_support": "supported"}, {"explicit_board_id": "authorized"},
                        {"status": "ready", "backend": "platformio"}):
            with self.subTest(changes=changes):
                row = dict(self.row, **changes)
                result = support.prepare_source_boards(self.core, self.download, self.metadata, [row], emit=self.emit)[0]
                self.assertEqual(result, row)
        self.runner.assert_not_called()

    def test_matcher_failure_without_primary_or_absence_proof_keeps_legacy_install_blocked(self):
        result = support.prepare_unsupported_boards(self.core, self.download, self.metadata, [self.row], emit=self.emit)[0]
        self.assertEqual(result, self.row)
        self.runner.assert_not_called()

    def test_primary_source_identity_is_bound_into_the_compile_certificate(self):
        row = self.ready()
        certificate = json.loads((self.core / row["arduino_cli"]["certificate"]).read_text(encoding="utf-8"))
        self.assertEqual(certificate["arduino_source_proof"], row["arduino_source_proof"])
        self.assertEqual(certificate["arduino_backend_role"], "primary")
        self.assertEqual(certificate["cli_path"], str(self.cli.resolve()))
        changed = copy.deepcopy(row)
        changed["arduino_source_proof"]["version"] = "4.0.0"
        changed["arduino_cli"]["version"] = "4.0.0"
        self.assertTrue(support.source_target_proof(changed))
        support.publish_prepared_targets(self.core, self.download, [changed])
        self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))

    def test_primary_role_cannot_be_added_to_an_unrelated_existing_certificate(self):
        row = self.ready()
        certificate_path = self.core / row["arduino_cli"]["certificate"]
        certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
        certificate.pop("arduino_source_proof")
        certificate.pop("arduino_backend_role")
        certificate_path.write_text(json.dumps(certificate), encoding="utf-8")
        row["arduino_cli"]["certificate_sha256"] = support.sha256_file(certificate_path)
        support.publish_prepared_targets(self.core, self.download, [row])
        self.assertIsNone(support.prepared_target_for_record(self.record, self.candidates(), core=self.core))

    def test_installed_board_receipt_cannot_certify_different_source_defaults(self):
        row = self.ready()
        installed = self.platform / "boards.txt"
        installed.write_text(installed.read_text().replace("4MB", "8MB"), encoding="utf-8")
        certificate_path = self.core / row["arduino_cli"]["certificate"]
        certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
        certificate["proof_files"][str(installed.relative_to(self.core))] = support.sha256_file(installed)
        certificate_path.write_text(json.dumps(certificate), encoding="utf-8")
        row["arduino_cli"]["certificate_sha256"] = support.sha256_file(certificate_path)
        support.publish_prepared_targets(self.core, self.download, [row])
        self.assertIsNone(support.prepared_target_for_record(self.record, self.candidates(), core=self.core))

    def test_changed_source_core_tool_and_cli_bytes_require_explicit_preparation(self):
        row = self.ready()
        original_tool = self.tool.read_bytes()
        self.tool.write_bytes(b"different tool")
        self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))
        self.tool.write_bytes(original_tool)
        info = support.arduino_catalog_entry(self.record, row)
        self.cli.write_bytes(b"changed CLI")
        self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))
        with patch.object(board_catalog, "_load_platformio_board_catalog", return_value=self.candidates()):
            with self.assertRaisesRegex(RuntimeError, "target is not verified"):
                support.runtime_command(info, core=self.core)

    def test_missing_primary_cli_requires_a_new_preparation_certificate(self):
        row = self.ready()
        validation_cache = {}
        self.assertIsNotNone(support.prepared_target_for_record(
            self.record, [], core=self.core, validation_cache=validation_cache))
        self.cli.unlink()
        self.assertIsNone(support.prepared_target_for_record(
            self.record, [], core=self.core, validation_cache=validation_cache))
        replacement = self.fixture / "moved" / "arduino-cli.exe"
        replacement.parent.mkdir()
        replacement.write_bytes(b"CLI bytes")
        with patch.object(toolchain, "find_arduino_cli_executable", return_value=str(replacement)):
            rebuilt = self.ready()
        self.assertEqual(rebuilt["arduino_cli"]["cli_path"], str(replacement.resolve()))
        self.assertIsNotNone(support.prepared_target_for_record(self.record, [], core=self.core))

    def test_primary_cli_path_must_match_its_certificate_even_with_equal_bytes(self):
        row = self.ready()
        validation_cache = {}
        self.assertIsNotNone(support.prepared_target_for_record(
            self.record, [], core=self.core, validation_cache=validation_cache))
        alternative = self.fixture / "alternative-cli.exe"
        alternative.write_bytes(self.cli.read_bytes())
        changed = copy.deepcopy(row)
        changed["arduino_cli"]["cli_path"] = str(alternative.resolve())
        support.publish_prepared_targets(self.core, self.download, [changed])
        self.assertIsNone(support.prepared_target_for_record(
            self.record, [], core=self.core, validation_cache=validation_cache))

    def test_primary_certificate_without_cli_path_cannot_be_reused(self):
        row = self.ready()
        certificate_path = self.core / row["arduino_cli"]["certificate"]
        certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
        certificate.pop("cli_path")
        certificate_path.write_text(json.dumps(certificate), encoding="utf-8")
        row["arduino_cli"]["certificate_sha256"] = support.sha256_file(certificate_path)
        support.publish_prepared_targets(self.core, self.download, [row])
        self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))

    def test_shared_builtin_tools_are_certified_alongside_declared_core_dependencies(self):
        builtin = self.store / "data/packages/builtin/tools/ctags/1.0.0/ctags.exe"
        builtin.parent.mkdir(parents=True)
        builtin.write_bytes(b"ctags bytes")
        row = self.ready()
        certificate = json.loads((self.core / row["arduino_cli"]["certificate"]).read_text(encoding="utf-8"))
        self.assertIn(str(self.tool.relative_to(self.core)), certificate["tool_files"])
        self.assertIn(str(builtin.relative_to(self.core)), certificate["tool_files"])
        self.assertIsNotNone(support.prepared_target_for_record(self.record, [], core=self.core))
        builtin.unlink()
        self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))

    def test_explicit_builtin_dependency_is_not_inventoried_twice(self):
        builtin = self.store / "data/packages/builtin/tools/ctags/1.0.0/ctags.exe"
        builtin.parent.mkdir(parents=True)
        builtin.write_bytes(b"ctags bytes")
        details = {"tools_dependencies": [{"packager": "esp32", "name": "compiler", "version": "1.0.0"},
                                          {"packager": "builtin", "name": "ctags", "version": "1.0.0"}]}
        with patch.object(support, "sha256_file", wraps=support.sha256_file) as hashes:
            inventory = support._tool_inventory(self.core, details, store=self.store)
        self.assertEqual(set(inventory), {str(self.tool.relative_to(self.core)), str(builtin.relative_to(self.core))})
        self.assertEqual(sum(Path(call.args[0]) == builtin for call in hashes.call_args_list), 1)

    def test_multiple_exact_versions_keep_independent_installed_cores_and_runtime_commands(self):
        first = self.ready()
        second_source = self.fixture / "other-source"
        second_source.mkdir()
        (second_source / "boards.txt").write_bytes(self.source.read_bytes())
        record = board_catalog._parse_downloaded_arduino_board_files(second_source, force_read=True)[0]
        row = {key: record[key] for key in ("name", "arduino_id", "source_file", "source_sha256")}
        row.update(status="ambiguous", platformio_support="unknown")
        metadata = dict(self.metadata, version="3.3.12")
        store = support._source_store(self.core, support.source_declaration_proof(record, metadata))
        platform = store / "data/packages/esp32/hardware/esp32/3.3.12"
        platform.mkdir(parents=True)
        (platform / "boards.txt").write_bytes(self.source.read_bytes())
        (platform / "platform.txt").write_text("name=ESP32 Arduino\nversion=3.3.12\n")
        tool = store / "data/packages/esp32/tools/compiler/1.0.0/bin/compiler.exe"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(self.tool.read_bytes())
        second = support.prepare_source_boards(self.core, second_source, metadata, [row], emit=self.emit)[0]
        self.assertEqual(second["status"], "ready", second["reason"])
        support.publish_prepared_targets(self.core, second_source, [second])
        self.assertNotEqual(first["arduino_cli"]["store"], second["arduino_cli"]["store"])
        commands = []
        with patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[]):
            for declaration, prepared in ((self.record, first), (record, second)):
                self.assertIsNotNone(support.prepared_target_for_record(declaration, [], core=self.core))
                info = support.arduino_catalog_entry(declaration, prepared)
                command, _, fqbn = support.runtime_command(info, core=self.core)
                commands.append(command)
                self.assertEqual(fqbn, "esp32:esp32:esp32s3")
        self.assertNotEqual(commands[0][commands[0].index("--config-file") + 1],
                            commands[1][commands[1].index("--config-file") + 1])

    def test_same_core_version_from_distinct_index_namespaces_is_isolated(self):
        proof = support.source_declaration_proof(self.record, self.metadata)
        other = dict(proof, index_url="https://other.invalid/index.json")
        self.assertNotEqual(support._source_store(self.core, proof), support._source_store(self.core, other))

    def test_primary_cannot_reuse_a_shared_or_wrong_version_store(self):
        row = self.ready()
        for store in ("arduino-cli", "arduino-cli/targets/" + "0" * 64):
            with self.subTest(store=store):
                changed = copy.deepcopy(row)
                changed["arduino_cli"]["store"] = store
                support.publish_prepared_targets(self.core, self.download, [changed])
                self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))

    def test_primary_configuration_missing_foreign_or_malformed_revokes_readiness(self):
        self.ready()
        config = self.store / "arduino-cli.yaml"
        original = config.read_text()
        for replacement in ("{broken", json.dumps({"directories": []}),
                            json.dumps({"directories": {"data": str(self.fixture / "global-data"),
                                                        "user": str(self.store / "user"),
                                                        "downloads": str(self.store / "downloads")}})):
            with self.subTest(replacement=replacement):
                config.write_text(replacement)
                self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))
        config.write_text(original)
        self.assertIsNotNone(support.prepared_target_for_record(self.record, [], core=self.core))
        config.unlink()
        self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))

    def test_extra_installed_version_in_primary_store_revokes_readiness(self):
        self.ready()
        (self.platform.parent / "9.9.9").mkdir()
        self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))

    def test_failed_primary_repair_retains_original_source_intention_without_readiness(self):
        row = self.ready()
        self.tool.unlink()
        self.assertIsNone(support.prepared_target_for_record(self.record, self.candidates(), core=self.core))
        planned = support.planned_source_target_for_record(self.record, core=self.core)
        self.assertEqual(planned["arduino_source_proof"], row["arduino_source_proof"])
        failed = dict(row, status="unavailable", reason="Fixture exact compiler repair failed")
        support.publish_prepared_targets(self.core, self.download, [failed])
        self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))
        self.assertEqual(support.planned_source_target_for_record(self.record, core=self.core)["status"], "unavailable")
        self.source.write_text(self.source.read_text() + "# changed source\n")
        self.assertIsNone(support.planned_source_target_for_record(self.record, core=self.core))

    def test_failed_first_primary_preparation_can_retain_namespace_without_a_compile_certificate(self):
        proof = support.source_declaration_proof(self.record, self.metadata)
        failed = dict(self.row, status="unavailable", backend="", arduino_backend_role="primary",
                      arduino_source_proof=proof, reason="Fixture installer failed")
        support.publish_prepared_targets(self.core, self.download, [failed])
        self.assertEqual(support.prepared_target_index(support.load_prepared_targets(self.core)), {})
        planned = support.planned_source_target_for_record(self.record, core=self.core)
        self.assertEqual(planned["arduino_source_proof"], proof)
        self.assertEqual(planned["status"], "unavailable")

    def test_changed_source_retains_only_blocked_namespace_intent_and_cannot_reuse_old_proof(self):
        original = self.ready()
        self.source.write_text(self.source.read_text().replace("4MB", "8MB"))
        current = board_catalog._parse_downloaded_arduino_board_files(self.download, force_read=True)[0]
        self.assertIsNone(support.planned_source_target_for_record(current, core=self.core))
        intent = support.source_namespace_target_for_record(current, core=self.core)
        self.assertEqual(intent["status"], "unavailable")
        self.assertEqual(intent["source_sha256"], current["source_sha256"])
        self.assertEqual(intent["arduino_source_proof"], original["arduino_source_proof"])
        self.assertTrue(support.source_namespace_proof(intent))
        self.assertFalse(support.source_target_proof(intent))
        self.assertNotIn("arduino_cli", intent)
        support.publish_prepared_targets(self.core, self.download, [intent])
        self.assertIsNone(support.prepared_target_for_record(current, [], core=self.core))
        self.assertIsNotNone(support.source_namespace_target_for_record(current, core=self.core))
        self.assertIsNone(support.planned_source_target_for_record(current, core=self.core))

    def test_namespace_intent_cannot_cross_exact_source_file_or_board_id(self):
        self.ready()
        other = self.fixture / "other-boards.txt"
        other.write_bytes(self.source.read_bytes())
        self.assertIsNone(support.source_namespace_target_for_record(dict(self.record, source_file=str(other)), core=self.core))
        self.assertIsNone(support.source_namespace_target_for_record(dict(self.record, arduino_id="esp32c3"), core=self.core))

    def test_missing_variant_header_revokes_ready_and_blocks_runtime(self):
        row = self.ready()
        certificate = json.loads((self.core / row["arduino_cli"]["certificate"]).read_text())
        self.assertIn(str(self.pins.relative_to(self.core)), certificate["tool_files"])
        self.pins.unlink()
        self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))
        with patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "not verified"):
                support.runtime_command(support.arduino_catalog_entry(self.record, row), core=self.core)

    def test_same_size_core_source_and_bootloader_changes_revoke_readiness(self):
        self.ready()
        for path in (self.core_source, self.bootloader):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                timestamp = path.stat()
                path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                os.utime(path, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))
                self.assertIsNone(support.prepared_target_for_record(self.record, [], core=self.core))
                path.write_bytes(original)
                self.assertIsNotNone(support.prepared_target_for_record(self.record, [], core=self.core))

    def test_offline_workspace_cannot_invoke_primary_preparation(self):
        with patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "1"}):
            with self.assertRaisesRegex(RuntimeError, "separate online worker"):
                support.prepare_source_boards(self.core, self.download, self.metadata, [self.row], emit=self.emit)
        self.runner.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
