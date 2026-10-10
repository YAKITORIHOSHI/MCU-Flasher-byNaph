#!/usr/bin/env python3
"""Isolated bootstrap declaration coverage checks; no installs or hardware."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from main.core import board_catalog
from src.modules import arduino_cli_support
from src.modules import arduino_board_selection as selection
from src.modules import bootstrap_board_coverage as coverage


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CoverageChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp/audit/bootstrap-board-coverage"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.core = self.root / "store"
        self.core.mkdir()
        self.plan = {"schema": 1, "platforms": ["atmelavr", "espressif32", "espressif8266"],
                     "frameworks": ["arduino"], "libraries": []}
        self.platform_sources = {platform: platform for platform in self.plan["platforms"]}
        self.records: dict[Path, list[dict]] = {}
        self.catalog: list[dict] = []
        self.manifests: dict[str, str] = {}
        self.prepared_rows: list[dict] = []

        self.uno = self.record("avr", "uno", "Arduino Uno", "atmega328p", "standard")
        self.nodemcu = self.record("esp8266", "nodemcuv2", "NodeMCU 1.0 (ESP-12E Module)",
                                  "esp8266", "nodemcu")
        self.s3 = self.record("esp32", "esp32s3", "ESP32S3 Dev Module", "esp32s3", "esp32s3")
        self.missing = self.record("custom", "absent", "Unmatched custom board", "fixturecpu", "")
        self.outside = self.record("rp2040", "pico", "Raspberry Pi Pico", "rp2040", "pico")
        self.uno_pio = self.candidate("atmelavr", "uno", self.uno)
        self.nodemcu_pio = self.candidate("espressif8266", "nodemcuv2", self.nodemcu)
        # These definitions have indistinguishable hardware evidence. A report
        # must retain their ambiguity even when their packages are prepared.
        self.s3_first = self.candidate("espressif32", "s3-fixture-a", self.s3)
        self.s3_second = self.candidate("espressif32", "s3-fixture-b", self.s3)
        self.candidate("raspberrypi", "pico", self.outside)

        stack = ExitStack()
        self.addCleanup(stack.close)
        self.settings = self.root / "arduino-browser-settings.json"
        self.settings.write_text(json.dumps({selection.FIELD: {
            selection.association_key({"package": "esp32", "architecture": "esp32",
                "index_url": "https://fixture.invalid/exact-source.json"}): ["esp32s3"],
            selection.association_key({"package": "arduino", "architecture": "avr",
                "index_url": "https://fixture.invalid/arduino-source.json"}): ["uno"],
        }}), encoding="utf-8")
        stack.enter_context(patch.object(selection, "settings_file", return_value=self.settings))
        selection.invalidate_preferences()
        self.addCleanup(selection.invalidate_preferences)
        stack.enter_context(patch.dict(os.environ, {
            "MCU_PACKAGE_EVENTS_ROOT": str(self.root / "package-events"),
        }))
        self.parser = stack.enter_context(patch.object(
            board_catalog, "_parse_downloaded_arduino_board_files", side_effect=self.parse_fixture))
        self.loader = stack.enter_context(patch.object(
            board_catalog, "_load_platformio_board_catalog", side_effect=lambda *_a, **_k: copy.deepcopy(self.catalog)))
        stack.enter_context(patch.object(
            board_catalog, "_get_arduino_board_search_roots",
            side_effect=AssertionError("Coverage must use only injected source directories")))
        stack.enter_context(patch.object(
            arduino_cli_support, "load_prepared_targets", side_effect=lambda *_a, **_k: copy.deepcopy(self.prepared_rows)))
        stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("Coverage must remain local")))
        stack.enter_context(patch("src.modules.offline_bootstrap.prepare", side_effect=AssertionError("No fixture installation")))

    def record(self, folder: str, identifier: str, name: str, mcu: str, variant: str) -> dict:
        source = self.root / "sources" / folder
        source.mkdir(parents=True, exist_ok=True)
        path = source / "boards.txt"
        path.write_text(f"{identifier}.name={name}\n{identifier}.build.mcu={mcu}\n"
                        f"{identifier}.build.variant={variant}\n", encoding="utf-8")
        record = {"arduino_id": identifier, "name": name, "mcu": mcu, "variant": variant,
                  "build_board": "", "core": "", "flash_size": "", "memory_type": "",
                  "flash_mode": "", "has_psram": False, "hwids": set(),
                  "source_file": str(path), "source_sha256": digest(path)}
        self.records.setdefault(source, []).append(record)
        return record

    def candidate(self, platform: str, identifier: str, record: dict) -> dict:
        folder = self.core / "platforms" / platform / "boards"
        folder.mkdir(parents=True, exist_ok=True)
        manifest = folder / f"{identifier}.json"
        manifest.write_text(json.dumps({"name": record["name"], "frameworks": ["arduino"],
                                        "build": {"mcu": record["mcu"], "variant": record["variant"]}}),
                            encoding="utf-8")
        row = {"platform": platform, "id": identifier, "name": record["name"], "vendor": "",
               "frameworks": {"arduino"}, "mcu": record["mcu"], "variant": record["variant"],
               "core": "", "flash_size": "", "memory_type": "", "flash_mode": "",
               "has_psram": False, "hwids": set(), "arduino_defines": set(),
               "manifest": str(manifest), "upload_protocol": "esptool" if platform == "espressif32" else "",
               "require_upload_port": True}
        self.catalog.append(row)
        self.manifests[str(manifest.relative_to(self.core))] = digest(manifest)
        return row

    def parse_fixture(self, source, *, force_read=False):
        self.assertTrue(force_read, "Coverage cannot reuse a timestamp-only declaration parse")
        return copy.deepcopy(self.records.get(Path(source), []))

    def report(self, *, plan=None, manifests=None, sources=None, source_preparation=None):
        return coverage.report_coverage(
            self.core, list(self.records) if sources is None else sources,
            plan=self.plan if plan is None else plan,
            platform_sources=self.platform_sources,
            board_manifests=self.manifests if manifests is None else manifests,
            log=lambda _message: None,
            source_preparation=source_preparation,
        )

    def row(self, report: dict, identifier: str) -> dict:
        matches = [row for row in report["boards"] if row["arduino_id"] == identifier]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_every_requested_directory_and_failure_is_reported(self):
        report = self.report()
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["total"], 5)
        self.assertEqual(report["ready_count"], 2)
        self.assertEqual(report["unavailable_count"], 3)
        self.assertEqual(self.row(report, "uno")["status"], "ready")
        self.assertEqual(self.row(report, "nodemcuv2")["status"], "ready")
        self.assertEqual(self.row(report, "esp32s3")["status"], "ambiguous")
        self.assertEqual(self.row(report, "absent")["status"], "unavailable")
        self.assertEqual(self.row(report, "pico")["status"], "outside_plan")
        for row in report["boards"]:
            self.assertTrue(row["reason"], row)
        self.assertEqual(json.loads((self.core / coverage.REPORT).read_text(encoding="utf-8")), report)
        self.assertEqual({Path(call.args[0]) for call in self.parser.call_args_list}, set(self.records))
        self.loader.assert_called_once_with(self.core, force_read=True)

    def test_same_display_names_from_distinct_sources_are_retained(self):
        other = self.record("another-vendor", "uno", "Arduino Uno", "atmega328p", "standard")
        report = self.report(sources=(source for source in self.records))
        rows = [row for row in report["boards"] if row["name"] == "Arduino Uno"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["source_file"] for row in rows}, {self.uno["source_file"], other["source_file"]})
        self.assertEqual(report["total"], 6)
        self.assertEqual(report["ready_count"], 3)

    def test_unchanged_full_report_preserves_its_timestamp(self):
        self.report()
        path = self.core / coverage.REPORT
        before = path.stat().st_mtime_ns
        self.report()
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_prepared_packages_do_not_turn_ambiguity_into_ready(self):
        report = self.report()
        row = self.row(report, "esp32s3")
        self.assertEqual(row["status"], "ambiguous")
        self.assertFalse(row.get("board"))
        candidates = row.get("candidates") or []
        self.assertEqual({candidate["id"] for candidate in candidates}, {"s3-fixture-a", "s3-fixture-b"})

    def test_ambiguous_platformio_models_do_not_route_through_failed_cli_preparation(self):
        failure = dict(self.s3, status="unavailable", reason="Exact source compilation failed: missing fixture compiler.")
        row = self.row(self.report(source_preparation={"boards": [failure]}), "esp32s3")
        self.assertEqual(row["status"], "ambiguous")
        self.assertEqual(row["backend"], "platformio")
        self.assertNotEqual(row["reason"], failure["reason"])
        self.assertEqual({candidate["id"] for candidate in row["candidates"]}, {"s3-fixture-a", "s3-fixture-b"})

    def test_failed_preparation_cannot_overlay_another_source_or_changed_declarations(self):
        for changes in ({"source_sha256": "0" * 64}, {"source_file": str(self.root / "other/boards.txt")}):
            with self.subTest(changes=changes):
                failure = dict(self.s3, status="unavailable", reason="Unrelated fixture failure.", **changes)
                row = self.row(self.report(source_preparation={"boards": [failure]}), "esp32s3")
                self.assertEqual(row["status"], "ambiguous")
                self.assertNotEqual(row["reason"], failure["reason"])

    def test_source_results_cannot_claim_ready_or_override_a_verified_target(self):
        false_ready = dict(self.s3, status="ready", backend="arduino-cli", reason="Unverified claim.")
        self.assertEqual(self.row(self.report(source_preparation={"boards": [false_ready]}), "esp32s3")["status"], "ambiguous")
        failure = dict(self.uno, status="unavailable", reason="Previous compiler failed.")
        self.assertEqual(self.row(self.report(source_preparation={"boards": [failure]}), "uno")["status"], "ready")

    def primary_intention(self):
        proof = arduino_cli_support.source_declaration_proof(self.s3, {
            "package": "esp32", "architecture": "esp32", "version": "3.3.11",
            "index_url": "https://fixture.invalid/exact-source.json"})
        return dict(self.s3, status="unavailable", backend="arduino-cli", arduino_backend_role="primary",
                    arduino_source_proof=proof, reason="Exact Arduino compiler preparation failed.")

    def test_unique_generic_s3_platformio_target_supersedes_source_primary_intention(self):
        intent = self.primary_intention()
        self.prepared_rows = [intent]
        self.catalog.remove(self.s3_second)
        row = self.row(self.report(), "esp32s3")
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["backend"], "platformio")
        self.assertEqual(row["board"], self.s3_first["id"])
        self.assertNotIn("arduino_source_proof", row)

    def test_current_unique_platformio_target_supersedes_source_primary_intention(self):
        proof = arduino_cli_support.source_declaration_proof(self.uno, {
            "package": "arduino", "architecture": "avr", "version": "1.8.6",
            "index_url": "https://fixture.invalid/arduino-source.json"})
        self.prepared_rows = [dict(self.uno, status="unavailable", backend="arduino-cli",
                                   arduino_backend_role="primary", arduino_source_proof=proof,
                                   reason="Prior Arduino CLI preparation failed.")]
        row = self.row(self.report(), "uno")
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["backend"], "platformio")
        self.assertEqual(row["board"], self.uno_pio["id"])
        self.assertNotIn("arduino_source_proof", row)

    def test_failed_current_primary_plan_preserves_namespace_and_precise_reason_without_previous_row(self):
        failure = self.primary_intention()
        self.catalog.remove(self.s3_second)
        self.catalog.remove(self.s3_first)
        row = self.row(self.report(source_preparation={"boards": [failure]}), "esp32s3")
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(row["backend"], "arduino-cli")
        self.assertEqual(row["reason"], failure["reason"])

    def test_disabled_board_cannot_resurrect_namespace_from_failed_source_report(self):
        failure = self.primary_intention()
        self.settings.write_text("{}", encoding="utf-8")
        selection.invalidate_preferences()
        self.catalog.remove(self.s3_second)
        self.catalog.remove(self.s3_first)
        row = self.row(self.report(source_preparation={"boards": [failure]}), "esp32s3")
        self.assertEqual(row["status"], "unavailable")
        self.assertNotEqual(row.get("backend"), "arduino-cli")
        self.assertNotIn("arduino_source_proof", row)

    def test_disabled_board_cannot_resurrect_namespace_from_old_prepared_rows(self):
        self.prepared_rows = [self.primary_intention()]
        self.settings.write_text("{}", encoding="utf-8")
        selection.invalidate_preferences()
        self.catalog.remove(self.s3_second)
        self.catalog.remove(self.s3_first)
        row = self.row(self.report(), "esp32s3")
        self.assertEqual(row["status"], "unavailable")
        self.assertNotEqual(row.get("backend"), "arduino-cli")
        self.assertNotIn("arduino_source_proof", row)

    def test_verified_explicit_platformio_mapping_supersedes_failed_primary_source_plan(self):
        self.prepared_rows = [{"status": "ready", "backend": "platformio", "arduino_id": "esp32s3",
                               "source_file": self.s3["source_file"], "source_sha256": self.s3["source_sha256"],
                               "platform": self.s3_first["platform"], "board": self.s3_first["id"],
                               "manifest_sha256": digest(Path(self.s3_first["manifest"]))}]
        row = self.row(self.report(source_preparation={"boards": [self.primary_intention()]}), "esp32s3")
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["backend"], "platformio")

    def test_changed_declarations_do_not_override_a_current_unique_platformio_target(self):
        intent = self.primary_intention()
        self.prepared_rows = [intent]
        self.catalog.remove(self.s3_second)
        source = Path(self.s3["source_file"])
        source.write_text(source.read_text() + "# Updated source defaults\n")
        self.s3["source_sha256"] = digest(source)
        row = self.row(self.report(), "esp32s3")
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["backend"], "platformio")
        self.assertEqual(row["board"], self.s3_first["id"])
        self.assertEqual(row["source_sha256"], self.s3["source_sha256"])
        self.assertNotIn("arduino_source_proof", row)

    def test_matching_definition_needs_its_manifest_receipt(self):
        signatures = dict(self.manifests)
        signatures.pop(str(Path(self.uno_pio["manifest"]).relative_to(self.core)))
        report = self.report(manifests=signatures)
        self.assertEqual(self.row(report, "uno")["status"], "unavailable")
        self.assertEqual(self.row(report, "nodemcuv2")["status"], "ready")

    def test_changed_manifest_bytes_cannot_inherit_ready(self):
        manifest = Path(self.uno_pio["manifest"])
        manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assertEqual(self.row(self.report(), "uno")["status"], "unavailable")

    def test_changed_source_bytes_invalidate_even_a_cached_parsed_record(self):
        source = Path(self.uno["source_file"])
        original_stat = source.stat()
        original = source.read_bytes()
        # Keep size and mtime identical: receipt checks must compare bytes.
        changed = original.replace(b"Arduino Uno", b"Arduino Duo")
        self.assertEqual(len(changed), len(original))
        source.write_bytes(changed)
        os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        self.assertEqual(self.row(self.report(), "uno")["status"], "unavailable")

    def test_explicit_framework_scope_cannot_report_arduino_ready(self):
        plan = dict(self.plan, frameworks=["espidf"])
        report = self.report(plan=plan)
        self.assertEqual(report["ready_count"], 0)
        self.assertEqual(self.row(report, "uno")["status"], "outside_plan")

    def test_omitted_framework_filter_keeps_complete_arduino_coverage(self):
        plan = dict(self.plan)
        plan.pop("frameworks")
        self.assertEqual(self.report(plan=plan)["ready_count"], 2)

    def test_exact_prepared_association_and_changed_source_receipt(self):
        chosen = self.s3_first
        self.prepared_rows = [{"status": "ready", "backend": "platformio", "arduino_id": "esp32s3",
                               "source_file": self.s3["source_file"], "source_sha256": self.s3["source_sha256"],
                               "platform": chosen["platform"], "board": chosen["id"],
                               "manifest_sha256": digest(Path(chosen["manifest"]))}]
        selected = self.row(self.report(), "esp32s3")
        self.assertEqual(selected["status"], "ready")
        self.assertEqual(selected["board"], chosen["id"])
        source = Path(self.s3["source_file"])
        source.write_text(source.read_text(encoding="utf-8") + "# changed source\n", encoding="utf-8")
        self.assertNotEqual(self.row(self.report(), "esp32s3")["status"], "ready")

    def test_empty_injected_source_set_does_not_discover_live_sources(self):
        report = self.report(sources=[])
        self.assertEqual(report["boards"], [])
        self.assertEqual(report["total"], 0)
        self.assertEqual(report["ready_count"], 0)
        self.parser.assert_not_called()

    def native_coverage(self):
        cli = self.root / "arduino-cli.exe"
        cli.write_bytes(b"fixture CLI")
        entries = {
            "arduino-cli/data/packages/vendor/hardware/fixture/1/boards.txt": b"fixture declarations",
            "arduino-cli/data/packages/vendor/hardware/fixture/1/platform.txt": b"fixture platform",
            "arduino-cli/data/packages/vendor/tools/compiler/1/bin/compiler.exe": b"fixture compiler",
            "arduino-cli/data/packages/vendor/tools/uploader/1/bin/uploader.py": b"fixture uploader",
        }
        for relative, content in entries.items():
            path = self.core / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        tools = {f"arduino-cli/data/packages/vendor/tools/compiler/1/include/header-{index}.h": {"size": 100}
                 for index in range(40000)}
        tools.update({relative: {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                      for relative, content in entries.items() if "/tools/" in relative})
        certificate = {"proof_files": {relative: hashlib.sha256(content).hexdigest()
                                       for relative, content in entries.items() if "/hardware/" in relative},
                       "tool_files": tools, "cli_path": str(cli)}
        certificate_path = self.core / "arduino-cli/certificates/fixture.json"
        certificate_path.parent.mkdir(parents=True, exist_ok=True)
        certificate_path.write_text(json.dumps(certificate), encoding="utf-8")
        return {"boards": [{"status": "ready", "backend": "arduino-cli",
                            "arduino_cli": {"certificate": str(certificate_path.relative_to(self.core)), "cli_path": str(cli)}}]}

    def test_large_native_inventory_keeps_startup_paths_bounded(self):
        report = self.native_coverage()
        files, guards = coverage.required_native_files(self.core, report)
        self.assertLessEqual(len(files), 9)
        self.assertFalse(any("header-" in relative for relative in files))
        self.assertTrue(any(relative.endswith("compiler.exe") for relative in files))
        self.assertTrue(any(relative.endswith("uploader.py") for relative in files))
        self.assertEqual(guards, [str(self.root / "arduino-cli.exe")])

    def test_native_compiler_disappearance_rejects_required_entry_points(self):
        report = self.native_coverage()
        (self.core / "arduino-cli/data/packages/vendor/tools/compiler/1/bin/compiler.exe").unlink()
        with self.assertRaisesRegex(ValueError, "entry point became unavailable"):
            coverage.required_native_files(self.core, report)

    def test_native_uploader_disappearance_rejects_required_entry_points(self):
        report = self.native_coverage()
        (self.core / "arduino-cli/data/packages/vendor/tools/uploader/1/bin/uploader.py").unlink()
        with self.assertRaisesRegex(ValueError, "entry point became unavailable"):
            coverage.required_native_files(self.core, report)

    def test_final_downloader_coverage_updates_full_certificate_and_replaces_old_native_paths(self):
        from src.modules import offline_bootstrap as setup
        native = self.native_coverage()
        native.update(schema=1, host=sys.platform, total=1, ready_count=1, unavailable_count=0,
                      sources=[str(self.root / "sources")])
        (self.core / coverage.REPORT).write_text(json.dumps(native))
        (self.core / "package.json").write_text("{}")
        (self.core / "old-native.bin").write_bytes(b"obsolete")
        (self.core / arduino_cli_support.TARGETS_FILE).write_text("{}")
        (self.core / "arduino-cli/arduino-cli.yaml").write_text("{}")
        data = {"schema": setup.SCHEMA, "host": sys.platform, "architecture": platform.machine(),
                "plan": setup.plan_hash(self.plan), "default_plan": setup.plan_hash(setup.load_plan()),
                "prepared_plan": self.plan, "platform_sources": self.platform_sources, "board_manifests": self.manifests,
                "files": ["package.json", "old-native.bin", coverage.REPORT], "guards": [],
                "native_files": ["old-native.bin"], "native_guards": [],
                "board_coverage": {"schema": 1, "path": coverage.REPORT, "total": 0, "ready_count": 0, "unavailable_count": 0}}
        (self.core / setup.MARKER).write_text(json.dumps(data))
        with patch.object(setup, "ready", return_value=True), patch.object(coverage, "report_coverage", return_value=native) as report:
            result = setup.finalize_board_coverage(self.core, native["sources"], plan=self.plan,
                                                  source_preparation={"boards": []}, log=lambda _: None)
        self.assertEqual(result, native)
        report.assert_called_once()
        final = json.loads((self.core / setup.MARKER).read_text())
        self.assertEqual(final["board_coverage"]["ready_count"], 1)
        self.assertEqual(final["board_coverage"]["sources"], native["sources"])
        self.assertNotIn("old-native.bin", final["files"])
        compiler = str(Path("arduino-cli/data/packages/vendor/tools/compiler/1/bin/compiler.exe"))
        self.assertIn(compiler, final["native_files"])
        with patch.object(setup, "ASSETS", ()):
            self.assertTrue(setup.ready(self.core, self.plan))
            (self.core / compiler).unlink()
            self.assertFalse(setup.ready(self.core, self.plan))

    def test_failed_coverage_refresh_revokes_previous_success_marker(self):
        from src.modules import offline_bootstrap as setup
        from src.modules import bootstrap_arduino_sources as sources
        marker = self.core / setup.MARKER
        marker.write_text(json.dumps({"prepared_plan": self.plan, "platform_sources": self.platform_sources,
                                      "board_manifests": self.manifests}))
        with patch.object(setup, "ready", return_value=True), \
                patch.object(sources, "prepare_sources", side_effect=RuntimeError("Fixture publication failed")):
            with self.assertRaisesRegex(RuntimeError, "publication failed"):
                setup.refresh_board_coverage(self.core, list(self.records), log=lambda _: None)
        self.assertFalse(marker.exists())

    def test_final_report_without_package_certificate_does_not_manufacture_readiness(self):
        from src.modules import offline_bootstrap as setup
        result = setup.finalize_board_coverage(self.core, list(self.records), plan=self.plan, log=lambda _: None)
        self.assertEqual(result["ready_count"], 0)
        self.assertFalse((self.core / setup.MARKER).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
