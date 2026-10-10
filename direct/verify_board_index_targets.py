#!/usr/bin/env python3
"""Hardware-free user-index discovery checks; no real registry or installation."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
from src.modules import board_index_targets as targets
from src.modules import board_preparation as worker
from src.modules import arduino_board_selection as selection
from src.modules import arduino_cli_support
from src.modules import offline_bootstrap
from main.core import board_catalog

BASE = {"schema": 1, "platforms": ["atmelavr"], "frameworks": ["arduino"], "libraries": []}
CUSTOM = {"package": "new-vendor", "architecture": "newarch", "name": "Future custom boards",
          "index_url": "https://fixture.invalid/boards.json", "boards": ["Future Board"]}


def declaration(identifier="future", name="Future Board", mcu="custommcu"):
    return {"arduino_id": identifier, "name": name, "mcu": mcu, "variant": "",
            "build_board": "", "hwids": set(), "source_file": ""}


def candidate(identifier="future", name="Future Board", platform="futureplatform", mcu="custommcu", frameworks=None):
    return {"id": identifier, "name": name, "mcu": mcu, "vendor": "", "platform": platform,
            "frameworks": set(frameworks or ["arduino"]), "variant": "", "hwids": set(),
            "arduino_defines": set(), "manifest": "", "platform_spec": "platformio/" + platform}


class CustomIndexChecks(unittest.TestCase):
    def setUp(self):
        # Native source preparation has its own byte-bound fixtures. Keep this
        # registry/association suite's new online entry point isolated too.
        original_sources = patch("src.modules.arduino_cli_support.prepare_source_boards",
            side_effect=lambda core, directory, metadata, rows, **kwargs: rows)
        original_sources.start()
        self.addCleanup(original_sources.stop)
        scratch = ROOT / "temp/audit/board-index-targets"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.core = self.root / "store"
        self.core.mkdir()
        self.archive = self.root / "archive"
        self.archive.mkdir()
        (self.archive / "boards.txt").write_text("future.name=Future Board\nfuture.build.mcu=custommcu\n", encoding="utf-8")
        self.records = board_catalog._parse_downloaded_arduino_board_files(self.archive, force_read=True)
        self.cli_preferences = {selection.association_key(metadata): ["future"] for metadata in
            (CUSTOM, dict(CUSTOM, package="arduino", architecture="avr"))}
        # Only the fabricated association namespaces may bypass real board policy.
        production_allowed = selection.board_allowed
        production_ids = selection.allowed_board_ids
        fixture_keys = frozenset(self.cli_preferences)
        fixture_ids = frozenset(("future",))
        for name, callback in (
                ("allowed_board_ids", lambda metadata: fixture_ids
                 if selection.association_key(metadata) in fixture_keys else production_ids(metadata)),
                ("board_allowed", lambda metadata, identifier: identifier in fixture_ids
                 if selection.association_key(metadata) in fixture_keys else production_allowed(metadata, identifier))):
            policy = patch.object(selection, name, side_effect=callback)
            policy.start()
            self.addCleanup(policy.stop)
        for module in (selection, arduino_cli_support):
            preference_reader = patch.object(module, "load_preferences",
                                             side_effect=lambda **_kwargs: dict(self.cli_preferences))
            preference_reader.start()
            self.addCleanup(preference_reader.stop)

    def test_unknown_family_resolves_from_exact_registered_board(self):
        report = worker.preparation_plan(self.records, package_metadata=CUSTOM, registry_catalog=[candidate()], base_plan=BASE)
        self.assertIn("platformio/futureplatform", report["plan"]["platforms"])
        self.assertEqual(report["boards"][0]["board"], "future")
        self.assertEqual(report["boards"][0]["platformio_support"], "supported")
        self.assertEqual(report["boards"][0]["status"], "preparation_required")
        self.assertEqual(report["boards"][0]["arduino_fqbn"], "new-vendor:newarch:future")

    def test_same_mcu_and_fuzzy_name_do_not_authorize_install(self):
        report = worker.preparation_plan([declaration("new", "Future Board Alternate")], package_metadata=CUSTOM,
                                         registry_catalog=[candidate()], base_plan=BASE)
        self.assertEqual(report["plan"], BASE)
        self.assertEqual(report["boards"][0]["board"], "")
        self.assertEqual(report["boards"][0]["platformio_support"], "unknown")

    def test_ambiguous_platforms_never_authorize_fallback(self):
        catalog = [candidate(platform="one"), candidate(platform="two")]
        proof = targets.registered_catalog_proof(catalog)
        report = worker.preparation_plan(self.records, package_metadata=CUSTOM,
            registry_catalog=catalog, registry_proof=proof, base_plan=BASE)
        self.assertEqual(report["plan"], BASE)
        self.assertEqual(report["boards"][0]["platformio_support"], "unknown")
        self.assertNotIn("platformio_support_proof", report["boards"][0])

    def test_declared_nonarduino_target_never_authorizes_fallback(self):
        catalog = [candidate(frameworks=["zephyr"])]
        report = worker.preparation_plan(self.records, package_metadata=CUSTOM, registry_catalog=catalog,
            registry_proof=targets.registered_catalog_proof(catalog), base_plan=BASE)
        self.assertEqual(report["boards"][0]["platformio_support"], "unknown")

    def test_conflicting_mcu_is_unknown_instead_of_false_unsupported(self):
        catalog = [candidate(mcu="differentmcu")]
        report = worker.preparation_plan(self.records, package_metadata=CUSTOM, registry_catalog=catalog,
            registry_proof=targets.registered_catalog_proof(catalog), base_plan=BASE)
        self.assertEqual(report["boards"][0]["platformio_support"], "unknown")

    def test_cosmetic_registry_name_divergence_remains_unknown_without_selecting(self):
        record = declaration(identifier="acme_custom", name="Acme Development Board")
        catalog = [candidate(identifier="acme_pio", name="Acme Dev Board")]
        report = worker.preparation_plan([record], package_metadata=dict(CUSTOM, boards=[]), registry_catalog=catalog,
            registry_proof=targets.registered_catalog_proof(catalog), base_plan=BASE)
        row = report["boards"][0]
        self.assertEqual(row["platformio_support"], "unknown")
        self.assertEqual(row["board"], "")
        self.assertNotIn("platformio_support_proof", row)
        self.assertEqual(report["plan"], BASE)

    def test_near_names_with_conflicting_or_absent_mcu_block_unsupported_proof(self):
        record = declaration(identifier="myboard", name="Acme Super Widget Rev 1", mcu="chipone")
        for mcu in ("chiptwo", ""):
            with self.subTest(mcu=mcu):
                catalog = [candidate(identifier="otherboard", name="Acme Super Widget Revision 1", mcu=mcu)]
                self.assertTrue(targets.hardware_identity_present(record, catalog))
                self.assertIsNone(targets.exact_arduino_match(record, catalog))

    def test_declared_vendor_can_explain_a_missing_display_prefix(self):
        record = declaration(identifier="acme_custom", name="Acme Development Board")
        row = candidate(identifier="alternate", name="Dev Board")
        row["vendor"] = "Acme"
        self.assertTrue(targets.hardware_identity_present(record, [row]))
        self.assertIsNone(targets.exact_arduino_match(record, [row]))

    def test_matching_chip_without_meaningful_name_evidence_does_not_block_absence(self):
        record = declaration(identifier="custom", name="Orion Manufacturing Board", mcu="esp32")
        catalog = [candidate(identifier="devkit", name="ESP32 Development Kit", mcu="esp32")]
        report = worker.preparation_plan([record], package_metadata=dict(CUSTOM, boards=[]), registry_catalog=catalog,
            registry_proof=targets.registered_catalog_proof(catalog), base_plan=BASE)
        self.assertEqual(report["boards"][0]["platformio_support"], "unsupported")
        self.assertFalse(targets.hardware_identity_present(record, catalog))

    def test_complete_absent_registry_target_has_fallback_proof(self):
        catalog = [candidate(identifier="unrelated", name="Unrelated Hardware", platform="other", mcu="othermcu")]
        report = worker.preparation_plan(self.records, package_metadata=CUSTOM, registry_catalog=catalog,
            registry_proof=targets.registered_catalog_proof(catalog), base_plan=BASE)
        row = report["boards"][0]
        self.assertEqual(row["platformio_support"], "unsupported")
        self.assertEqual(row["platformio_support_proof"]["kind"], "registry-exact-target-absent")
        self.assertEqual(len(row["platformio_support_proof"]["catalog_sha256"]), 64)
        self.assertEqual(report["plan"], BASE)

    def test_index_only_name_has_no_fallback_identity(self):
        catalog = [candidate(identifier="unrelated", name="Unrelated Hardware")]
        report = worker.preparation_plan([], package_metadata=CUSTOM, registry_catalog=catalog,
            registry_proof=targets.registered_catalog_proof(catalog), base_plan=BASE)
        self.assertEqual(report["boards"][0]["platformio_support"], "unknown")
        self.assertEqual(report["boards"][0]["arduino_id"], "")

    def test_explicit_registry_pin_overrides_unpinned_base_and_preserves_prior_scope(self):
        metadata = dict(CUSTOM, platformio={"platform": "platformio/futureplatform@2.3.4", "board_ids": {"future": "special"}})
        previous = dict(BASE, platforms=["ststm32"], libraries=["fixture/Other"])
        report = worker.preparation_plan(self.records, package_metadata=metadata, base_plan=BASE, previous_plan=previous)
        self.assertIn("platformio/futureplatform@2.3.4", report["plan"]["platforms"])
        self.assertIn("ststm32", report["plan"]["platforms"])
        self.assertIn("fixture/Other", report["plan"]["libraries"])
        self.assertEqual(report["boards"][0]["board"], "special")
        self.assertEqual(report["boards"][0]["platformio_support"], "unknown")

    def test_explicit_url_needs_actual_platform_binding(self):
        spec = "https://fixture.invalid/custom-source.git#release"
        metadata = dict(CUSTOM, platformio={"platform": spec, "board_ids": {"future": "canonical"}})
        catalog = [candidate(identifier="canonical", name="Very Different Name", platform="actualplatform")]
        before = worker.preparation_plan(self.records, package_metadata=metadata, catalog=catalog, base_plan=BASE)
        self.assertEqual(before["boards"][0]["platformio_support"], "unknown")
        after = worker.preparation_plan(self.records, package_metadata=metadata, catalog=catalog,
            base_plan=BASE, platform_sources={spec: "actualplatform"})
        self.assertEqual(after["boards"][0]["platform"], "actualplatform")
        self.assertEqual(after["boards"][0]["platformio_support"], "supported")
        self.assertEqual(after["boards"][0]["match_reasons"], ["explicit-index-board-id"])
        self.assertIn(spec, after["plan"]["platforms"])

    def test_explicit_mapping_cannot_override_mcu_conflict(self):
        spec = "platformio/futureplatform"
        metadata = dict(CUSTOM, platformio={"platform": spec, "board_ids": {"future": "canonical"}})
        report = worker.preparation_plan(self.records, package_metadata=metadata,
            catalog=[candidate(identifier="canonical", mcu="differentmcu")], base_plan=BASE)
        self.assertEqual(report["boards"][0]["platformio_support"], "unknown")

    def test_metadata_api_preserves_valid_source_but_rejects_executable_mapping(self):
        metadata = dict(CUSTOM, platformio={"platform": "vendor/custom@1.0", "board_ids": {"future": "canonical"}})
        parsed = worker._small_metadata(metadata)
        self.assertEqual(parsed["index_url"], CUSTOM["index_url"])
        self.assertEqual(parsed["platformio"], metadata["platformio"])
        for configuration in ({"platform": "--flag"}, {"platform": "https://user:secret@fixture.invalid/source"},
            {"platform": "vendor/custom\nmore"}, {"platform": "vendor/custom", "board_ids": {"future": "../board"}},
            {"board_ids": {"future": "canonical"}}):
            with self.subTest(configuration=configuration), self.assertRaises(ValueError):
                targets.normalize_platformio_configuration(configuration)

    def test_registered_catalog_failure_cannot_be_successful_local_subset(self):
        with patch.object(targets.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["fixture"])), \
                self.assertRaises(subprocess.CalledProcessError):
            targets.fetch_preparation_catalog(self.core)
        with patch.object(targets.subprocess, "run", return_value=Mock(stdout="[]")), self.assertRaises(ValueError):
            targets.fetch_preparation_catalog(self.core)

    def test_registered_query_uses_separate_child_and_app_store(self):
        raw = [{"id": "future", "name": "Future Board", "platform": "futureplatform", "mcu": "custommcu", "frameworks": ["arduino"]}]
        with patch.object(targets.subprocess, "run", return_value=Mock(stdout=json.dumps(raw))) as run:
            catalog = targets.fetch_preparation_catalog(self.core)
        command = run.call_args.args[0]
        self.assertIn("--registry-catalog", command)
        self.assertEqual(run.call_args.kwargs["env"]["PLATFORMIO_CORE_DIR"], str(self.core))
        self.assertEqual(catalog[0]["platform_spec"], "platformio/futureplatform")
        self.assertEqual(catalog[0]["manifest"], "")

    def test_workspace_role_cannot_query_registered_targets(self):
        with patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "1"}), \
                patch.object(targets.subprocess, "run") as run, self.assertRaises(RuntimeError):
            targets.fetch_preparation_catalog(self.core)
        run.assert_not_called()

    def test_installed_external_source_keeps_verified_identity(self):
        folder = self.core / "platforms/futureplatform"
        folder.mkdir(parents=True)
        url = "https://fixture.invalid/custom.git#exact"
        (folder / ".piopm").write_text(json.dumps({"type": "platform", "name": "futureplatform", "spec": {"uri": url}}), encoding="utf-8")
        sources = targets.installed_platform_specifications(self.core, [candidate()])
        report = worker.preparation_plan(self.records, package_metadata=CUSTOM, catalog=[candidate()],
                                        base_plan=BASE, installed_sources=sources)
        self.assertIn(url, report["plan"]["platforms"])
        self.assertEqual(report["boards"][0]["platform_spec"], url)

    def test_certificate_source_binding_retains_source_and_real_platform(self):
        url = "https://fixture.invalid/repo.git#exact"
        (self.core / offline_bootstrap.MARKER).write_text(json.dumps({"schema": offline_bootstrap.SCHEMA,
            "host": sys.platform, "platform_sources": {url: "actualplatform"}}), encoding="utf-8")
        bindings = targets.platform_source_bindings(self.core, BASE)
        self.assertEqual(bindings[url], "actualplatform")
        self.assertEqual(bindings["atmelavr"], "atmelavr")

    def test_nonobject_saved_preparation_metadata_cannot_grant_readiness(self):
        platform = self.core / "platforms/futureplatform"
        platform.mkdir(parents=True)
        marker = self.core / offline_bootstrap.MARKER
        for value in ([], None, "invalid"):
            marker.write_text(json.dumps(value), encoding="utf-8")
            (platform / ".piopm").write_text(json.dumps(value), encoding="utf-8")
            self.assertIsNone(worker._previous_plan(self.core))
            self.assertEqual(worker._certified_definitions(self.core, []), set())
            bindings = targets.platform_source_bindings(self.core, BASE)
            self.assertEqual(bindings["atmelavr"], "atmelavr")
            self.assertEqual(targets.installed_platform_specifications(self.core, [candidate()]), {})

    def test_complete_pipeline_verifies_new_platform_bytes(self):
        folder = self.core / "platforms/futureplatform/boards"
        folder.mkdir(parents=True)
        manifest = folder / "future.json"
        (folder.parent / "platform.json").write_text('{"name":"futureplatform"}', encoding="utf-8")
        manifest.write_text(json.dumps({"name": "Future Board", "frameworks": ["arduino"], "build": {"mcu": "custommcu"}}), encoding="utf-8")
        catalog = board_catalog._load_platformio_board_catalog(self.core, force_read=True)
        plan = dict(BASE, platforms=BASE["platforms"] + ["platformio/futureplatform"])
        certificate = {"schema": offline_bootstrap.SCHEMA, "host": sys.platform,
            "prepared_plan": plan, "platform_sources": {"platformio/futureplatform": "futureplatform"},
            "files": [str(manifest.relative_to(self.core))], "guards": [],
            "exact_builder_targets": [{"platform": "futureplatform", "board": "future", "framework": "arduino"}],
            "board_manifests": {str(manifest.relative_to(self.core)): hashlib.sha256(manifest.read_bytes()).hexdigest()}}
        events = []
        installed = False
        def prepare(*args, **kwargs):
            nonlocal installed
            (self.core / offline_bootstrap.MARKER).write_text(json.dumps(certificate), encoding="utf-8")
            installed = True
        with patch.object(offline_bootstrap, "load_plan", return_value=BASE), \
                patch.object(offline_bootstrap, "prepare", side_effect=prepare), \
                patch.object(offline_bootstrap, "ready", side_effect=lambda *args, **kwargs: (self.core / offline_bootstrap.MARKER).is_file()), \
                patch.object(board_catalog, "_load_platformio_board_catalog", side_effect=lambda *args, **kwargs: catalog if installed else []), \
                patch.object(targets, "fetch_preparation_catalog", return_value=[candidate()]), \
                patch("src.modules.arduino_cli_support.publish_prepared_targets") as publish:
            result = worker.run_preparation(self.core, self.archive, package_metadata=CUSTOM,
                emit=lambda stage, **details: events.append((stage, details)))
        self.assertEqual(events[-1][0], "ready")
        self.assertEqual(result["boards"][0]["manifest_sha256"], certificate["board_manifests"][str(manifest.relative_to(self.core))])
        publish.assert_called_once()
        final = json.loads((self.core / offline_bootstrap.MARKER).read_text(encoding="utf-8"))
        self.assertEqual(final["board_coverage"]["ready_count"], 1)
        self.assertIn(final["board_coverage"]["path"], final["files"])

    def test_exact_requested_aliases_extend_shared_builder_coverage(self):
        one = candidate(identifier="one", name="Board One")
        two = candidate(identifier="two", name="Board Two")
        requested = [{"platform_spec": "vendor/futureplatform@1.0", "platform": "futureplatform",
                      "board": "two", "record": declaration(identifier="two", name="Board Two")}]
        with patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[one, two]):
            probes = offline_bootstrap._requested_builder_probes(self.core, "vendor/futureplatform@1.0",
                "futureplatform", [("one", "arduino")], requested, {"arduino"})
        self.assertEqual(probes, [("one", "arduino"), ("two", "arduino")])

    def test_external_source_requested_match_waits_for_installed_exact_identity(self):
        url = "https://fixture.invalid/board-source.git"
        requested = [{"platform_spec": url, "platform": "", "board": "", "record": declaration()}]
        with patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[candidate()]):
            probes = offline_bootstrap._requested_builder_probes(self.core, url, "futureplatform", [], requested, {"arduino"})
        self.assertEqual(probes, [("future", "arduino")])

    def test_hash_without_exact_builder_receipt_cannot_be_ready(self):
        manifest = self.core / "target.json"
        manifest.write_text("{}", encoding="utf-8")
        row = {"platform": "futureplatform", "board": "future", "manifest": str(manifest), "status": "preparation_required"}
        payload = {"board_manifests": {"target.json": hashlib.sha256(manifest.read_bytes()).hexdigest()}}
        (self.core / offline_bootstrap.MARKER).write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(worker._certified_definitions(self.core, [row]), set())

    def test_network_failure_stays_unknown_and_never_invokes_cli_fallback(self):
        with patch.object(offline_bootstrap, "load_plan", return_value=BASE), \
                patch.object(offline_bootstrap, "ready", return_value=False), \
                patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[]), \
                patch.object(targets, "fetch_preparation_catalog", side_effect=OSError("fixture offline")), \
                patch("src.modules.arduino_cli_support.prepare_unsupported_boards") as fallback, \
                patch("src.modules.arduino_cli_support.publish_prepared_targets"):
            result = worker.run_preparation(self.core, self.archive, package_metadata=CUSTOM, emit=Mock())
        self.assertEqual(result["boards"][0]["platformio_support"], "unknown")
        self.assertIn("catalog discovery was unavailable", result["boards"][0]["reason"])
        fallback.assert_not_called()

    def test_confirmed_absent_target_invokes_cli_and_retains_notice(self):
        unrelated = candidate(identifier="unrelated", name="Other Hardware", mcu="othermcu")
        def fallback(core, directory, metadata, rows, **kwargs):
            self.assertEqual(metadata["index_url"], CUSTOM["index_url"])
            self.assertEqual(rows[0]["platformio_support"], "unsupported")
            return [dict(rows[0], backend="arduino-cli", status="ready", reason="Ready via Arduino CLI")]
        events = []
        with patch.object(offline_bootstrap, "load_plan", return_value=BASE), \
                patch.object(offline_bootstrap, "ready", return_value=False), \
                patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[]), \
                patch.object(targets, "fetch_preparation_catalog", return_value=[unrelated]), \
                patch("src.modules.arduino_cli_support.prepare_unsupported_boards", side_effect=fallback), \
                patch("src.modules.arduino_cli_support.publish_prepared_targets"):
            result = worker.run_preparation(self.core, self.archive, package_metadata=CUSTOM,
                emit=lambda stage, **details: events.append((stage, details)))
        self.assertEqual(result["boards"][0]["backend"], "arduino-cli")
        self.assertEqual(events[-1][0], "ready")
        self.assertIn("prepared Arduino compiler targets", events[-1][1]["message"])
        self.assertEqual(events[-1][1]["arduino_cli_count"], 1)

    def test_confirmed_absence_and_index_flags_cannot_opt_in_a_board(self):
        self.cli_preferences.clear()
        metadata = dict(CUSTOM, version="1.2.3", arduino_cli_selected=True,
                        board_arduino_cli_selections={selection.association_key(CUSTOM): ["future"]})
        unrelated = candidate(identifier="unrelated", name="Other Hardware", mcu="othermcu")
        with patch.object(offline_bootstrap, "load_plan", return_value=BASE), \
                patch.object(offline_bootstrap, "ready", return_value=False), \
                patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[]), \
                patch.object(targets, "fetch_preparation_catalog", return_value=[unrelated]), \
                patch.object(arduino_cli_support, "_run_json") as native, \
                patch("main.core.toolchain.find_arduino_cli_executable") as find_cli, \
                patch("src.modules.arduino_cli_support.publish_prepared_targets"):
            result = worker.run_preparation(self.core, self.archive, package_metadata=metadata, emit=Mock())
        self.assertEqual(result["boards"][0]["platformio_support"], "unsupported")
        self.assertNotEqual(result["boards"][0]["status"], "ready")
        native.assert_not_called()
        find_cli.assert_not_called()

    def test_known_avr_family_absent_exact_board_queries_registry_and_authorizes_cli(self):
        metadata = dict(CUSTOM, package="arduino", architecture="avr", version="1.2.3")
        unrelated = candidate(identifier="unrelated", name="Other Hardware", mcu="othermcu", platform="atmelavr")
        initial = worker.preparation_plan(self.records, package_metadata=metadata, base_plan=BASE)
        self.assertEqual(initial["boards"][0]["status"], "preparation_required")
        self.assertEqual(initial["boards"][0]["board"], "")
        self.assertEqual(initial["boards"][0]["platformio_support"], "unknown")
        def fallback(core, directory, package, rows, **kwargs):
            self.assertEqual(rows[0]["platformio_support"], "unsupported")
            self.assertEqual(rows[0]["arduino_fqbn"], "arduino:avr:future")
            return [dict(rows[0], status="ready", backend="arduino-cli")]
        with patch.object(offline_bootstrap, "load_plan", return_value=BASE), \
                patch.object(offline_bootstrap, "ready", return_value=False), \
                patch.object(offline_bootstrap, "prepare") as pio_prepare, \
                patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[]), \
                patch.object(targets, "fetch_preparation_catalog", return_value=[unrelated]) as registry, \
                patch("src.modules.arduino_cli_support.prepare_unsupported_boards", side_effect=fallback) as cli_prepare, \
                patch("src.modules.arduino_cli_support.publish_prepared_targets"):
            result = worker.run_preparation(self.core, self.archive, package_metadata=metadata, emit=Mock())
        registry.assert_called_once()
        pio_prepare.assert_not_called()
        cli_prepare.assert_called_once()
        self.assertEqual(result["boards"][0]["backend"], "arduino-cli")

    def test_known_family_registry_exact_board_without_install_prepares_platformio(self):
        metadata = dict(CUSTOM, package="arduino", architecture="avr", version="1.2.3")
        registered = candidate(platform="atmelavr")
        folder = self.core / "platforms/atmelavr/boards"
        folder.mkdir(parents=True)
        manifest = folder / "future.json"
        (folder.parent / "platform.json").write_text('{"name":"atmelavr"}', encoding="utf-8")
        manifest.write_text(json.dumps({"name": "Future Board", "frameworks": ["arduino"], "build": {"mcu": "custommcu"}}), encoding="utf-8")
        installed = board_catalog._load_platformio_board_catalog(self.core, force_read=True)
        prepared = False
        def prepare(*args, **kwargs):
            nonlocal prepared
            (self.core / offline_bootstrap.MARKER).write_text(json.dumps({"schema": offline_bootstrap.SCHEMA,
                "host": sys.platform, "prepared_plan": BASE,
                "platform_sources": {"atmelavr": "atmelavr"}, "files": [str(manifest.relative_to(self.core))], "guards": [],
                "exact_builder_targets": [{"platform": "atmelavr", "board": "future", "framework": "arduino"}],
                "board_manifests": {str(manifest.relative_to(self.core)): hashlib.sha256(manifest.read_bytes()).hexdigest()}}), encoding="utf-8")
            prepared = True
        with patch.object(offline_bootstrap, "load_plan", return_value=BASE), \
                patch.object(offline_bootstrap, "ready", side_effect=lambda *args, **kwargs: (self.core / offline_bootstrap.MARKER).is_file()), \
                patch.object(offline_bootstrap, "prepare", side_effect=prepare) as pio_prepare, \
                patch.object(board_catalog, "_load_platformio_board_catalog", side_effect=lambda *args, **kwargs: installed if prepared else []), \
                patch.object(targets, "fetch_preparation_catalog", return_value=[registered]) as registry, \
                patch("src.modules.arduino_cli_support.prepare_unsupported_boards") as cli_prepare, \
                patch("src.modules.arduino_cli_support.publish_prepared_targets"):
            result = worker.run_preparation(self.core, self.archive, package_metadata=metadata, emit=Mock())
        registry.assert_called_once()
        pio_prepare.assert_called_once()
        cli_prepare.assert_not_called()
        self.assertEqual(result["boards"][0]["backend"], "platformio")
        self.assertEqual(result["boards"][0]["status"], "ready")
        final = json.loads((self.core / offline_bootstrap.MARKER).read_text(encoding="utf-8"))
        self.assertEqual(final["board_coverage"]["ready_count"], 1)

    def test_known_family_registry_failure_never_authorizes_cli(self):
        metadata = dict(CUSTOM, package="arduino", architecture="avr", version="1.2.3")
        with patch.object(offline_bootstrap, "load_plan", return_value=BASE), \
                patch.object(offline_bootstrap, "ready", side_effect=[False, True, True]), \
                patch.object(offline_bootstrap, "prepare") as pio_prepare, \
                patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[]), \
                patch.object(targets, "fetch_preparation_catalog", side_effect=OSError("fixture offline")) as registry, \
                patch("src.modules.arduino_cli_support.prepare_unsupported_boards") as cli_prepare, \
                patch("src.modules.arduino_cli_support.publish_prepared_targets"):
            result = worker.run_preparation(self.core, self.archive, package_metadata=metadata, emit=Mock())
        registry.assert_called_once()
        pio_prepare.assert_called_once()
        cli_prepare.assert_not_called()
        self.assertEqual(result["boards"][0]["platformio_support"], "unknown")
        self.assertEqual(result["boards"][0]["status"], "unavailable")
        self.assertIn("online catalog discovery was unavailable", result["boards"][0]["reason"])

    def test_known_family_hint_does_not_hide_unique_exact_definition_on_other_platform(self):
        metadata = dict(CUSTOM, package="arduino", architecture="avr")
        catalog = [candidate(platform="verifiedother")]
        report = worker.preparation_plan(self.records, package_metadata=metadata, registry_catalog=catalog,
            registry_proof=targets.registered_catalog_proof(catalog), base_plan=BASE)
        self.assertEqual(report["boards"][0]["platform"], "verifiedother")
        self.assertEqual(report["boards"][0]["platformio_support"], "supported")
        self.assertIn("platformio/verifiedother", report["plan"]["platforms"])

    def test_integrated_worker_cli_preparation_publishes_source_bound_ready_target(self):
        from src.modules import arduino_cli_support
        metadata = dict(CUSTOM, version="1.2.3")
        cli = self.root / "fixture-arduino-cli.exe"
        cli.write_bytes(b"Fixture executable")
        platform = self.core / "arduino-cli/data/packages/new-vendor/hardware/newarch/1.2.3"
        platform.mkdir(parents=True)
        (platform / "boards.txt").write_text((self.archive / "boards.txt").read_text(encoding="utf-8"), encoding="utf-8")
        (platform / "platform.txt").write_text("name=Fixture Core\n", encoding="utf-8")
        compiler = self.core / "arduino-cli/data/packages/new-vendor/tools/compiler/1.0.0/bin/compiler.exe"
        compiler.parent.mkdir(parents=True)
        compiler.write_bytes(b"Fixture compiler")
        unrelated = candidate(identifier="unrelated", name="Other Hardware", mcu="othermcu")
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if "core" in command and "list" in command:
                return {"platforms": [{"id": "new-vendor:newarch", "installed_version": "1.2.3"}]}
            if "details" in command:
                return {"fqbn": command[command.index("--fqbn") + 1],
                        "tools_dependencies": [{"packager": "new-vendor", "name": "compiler", "version": "1.0.0"}]}
            return None
        events = []
        with patch.object(offline_bootstrap, "load_plan", return_value=BASE), \
                patch.object(offline_bootstrap, "ready", return_value=False), \
                patch.object(board_catalog, "_load_platformio_board_catalog", return_value=[]), \
                patch.object(targets, "fetch_preparation_catalog", return_value=[unrelated]), \
                patch("main.core.toolchain.find_arduino_cli_executable", return_value=str(cli)), \
                patch.object(arduino_cli_support, "_run_json", side_effect=run):
            result = worker.run_preparation(self.core, self.archive, package_metadata=metadata,
                emit=lambda stage, **details: events.append((stage, details)))
        self.assertEqual(result["boards"][0]["status"], "ready", result["boards"][0]["reason"])
        self.assertEqual(result["boards"][0]["backend"], "arduino-cli")
        self.assertEqual(result["boards"][0]["arduino_fqbn"], "new-vendor:newarch:future")
        self.assertFalse(any("upload" in command for command in commands))
        self.assertTrue(any("compile" in command for command in commands))
        self.assertEqual(events[-1][0], "ready")
        prepared = arduino_cli_support.prepared_target_for_record(self.records[0], [], core=self.core)
        self.assertEqual(prepared["backend"], "arduino-cli")
        self.assertEqual(prepared["arduino_fqbn"], "new-vendor:newarch:future")


if __name__ == "__main__":
    unittest.main(verbosity=2)
