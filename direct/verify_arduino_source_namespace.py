#!/usr/bin/env python3
"""Original-source namespace retention and collision checks, without hardware."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from direct import verify_arduino_source_targets as fixtures
from main.core import board_catalog, target_profile
from src.modules import arduino_cli_support as support, bootstrap_arduino_sources as preparation


class SourceNamespaceChecks(unittest.TestCase):
    setUp = fixtures.SourceTargetChecks.setUp
    ready = fixtures.SourceTargetChecks.ready
    candidates = fixtures.SourceTargetChecks.candidates

    def refresh(self, roots=None, previous=None, candidates=None):
        with patch.object(board_catalog, "_get_arduino_board_search_roots", return_value=roots or [self.download]), \
                patch.object(board_catalog, "_load_platformio_board_catalog", return_value=candidates or self.candidates()[:1]), \
                patch.object(board_catalog, "_save_board_catalog_cache"):
            return board_catalog.load_dynamic_boards(previous or {})

    def assert_original_unavailable(self, info):
        self.assertEqual(info["backend"], "arduino-cli")
        self.assertEqual(info["arduino_backend_role"], "primary")
        self.assertEqual(info["board"], "esp32s3")
        self.assertEqual(info["arduino_fqbn"], "esp32:esp32:esp32s3")
        self.assertIsNone(info["arduino_cli"])
        self.assertTrue(target_profile.target_problem(info, arduino_sketch=True))

    def test_missing_primary_cli_never_becomes_unique_physical_platformio_target(self):
        row = self.ready()
        info = support.arduino_catalog_entry(self.record, row)
        self.cli.unlink()
        actual = board_catalog.resolve_board_definition(self.record["name"], info, self.candidates()[:1])
        self.assert_original_unavailable(actual)
        self.assertNotIn("flash_mb", actual)
        self.assertNotIn("Ready through", actual["fallback_notice"])

    def test_cached_primary_retains_namespace_if_association_file_disappears(self):
        row = self.ready()
        info = support.arduino_catalog_entry(self.record, row)
        (self.core / support.TARGETS_FILE).unlink()
        actual = board_catalog.resolve_board_definition(self.record["name"], info, self.candidates()[:1])
        self.assert_original_unavailable(actual)

    def test_fresh_discovery_of_invalid_primary_keeps_original_namespace(self):
        self.ready()
        self.cli.unlink()
        self.assert_original_unavailable(self.refresh()[self.record["name"]])

    def test_unavailable_primary_publication_survives_later_fresh_refresh(self):
        row = self.ready()
        row.update(status="unavailable", reason="Exact core preparation failed.")
        support.publish_prepared_targets(self.core, self.download, [row])
        values = support.load_prepared_targets(self.core)
        self.assertEqual(values[0]["status"], "unavailable")
        self.assertEqual(support.prepared_target_index(values), {})
        self.assertIsNone(support.prepared_target_for_record(self.record, self.candidates(), core=self.core))
        self.assert_original_unavailable(self.refresh()[self.record["name"]])

    def test_invalid_primary_is_reprepared_before_new_automatic_platformio_match(self):
        self.ready()
        self.cli.unlink()
        def failure(core, directory, metadata, rows, **kwargs):
            return [dict(row, status="unavailable", reason="Simulated exact-core preparation failure.") for row in rows]
        with patch.object(board_catalog, "_load_platformio_board_catalog", return_value=self.candidates()[:1]), \
                patch.object(preparation, "_receipt_for_record", return_value=(self.metadata, self.download, "")), \
                patch.object(preparation, "_platformio_preferences", return_value={}), \
                patch.object(support, "prepare_source_boards", side_effect=failure) as prepare:
            result = preparation.prepare_sources(self.core, [self.download], emit=Mock())
        s3 = next(row for row in result["boards"] if row["arduino_id"] == "esp32s3")
        self.assertEqual(s3["status"], "unavailable")
        self.assertEqual(s3["arduino_backend_role"], "primary")
        self.assertTrue(any(row["arduino_id"] == "esp32s3" for call in prepare.call_args_list for row in call.args[3]))
        self.assert_original_unavailable(self.refresh()[self.record["name"]])

    def test_valid_explicit_platformio_mapping_can_replace_prior_primary(self):
        row = self.ready()
        info = support.arduino_catalog_entry(self.record, row)
        candidate = self.candidates()[0]
        manifest = self.core / "platforms/espressif32/boards/esp32-s3-devkitc-1.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}", encoding="utf-8")
        candidate["manifest"] = str(manifest)
        mapped = dict(row, status="ready", backend="platformio", platform="espressif32",
                      board=candidate["id"], manifest=str(manifest), manifest_sha256=support.sha256_file(manifest),
                      match_reasons=["explicit-index-board-id"])
        for key in ("arduino_backend_role", "arduino_source_proof", "arduino_fqbn", "arduino_cli"):
            mapped.pop(key)
        support.publish_prepared_targets(self.core, self.download, [mapped])
        actual = board_catalog.resolve_board_definition(self.record["name"], info, [candidate])
        self.assertEqual(actual["backend"], "platformio")
        self.assertEqual(actual["board"], candidate["id"])
        self.assertNotIn("arduino_backend_role", actual)

    def intent(self, root, *, version="3.3.11"):
        root.mkdir(parents=True, exist_ok=True)
        source = root / "boards.txt"
        source.write_bytes(self.source.read_bytes())
        record = board_catalog._parse_downloaded_arduino_board_files(root, force_read=True)[0]
        proof = support.source_declaration_proof(record, dict(self.metadata, version=version))
        row = {key: record[key] for key in ("name", "arduino_id", "source_file", "source_sha256")}
        row.update(status="unavailable", backend="arduino-cli", arduino_backend_role="primary",
                   arduino_source_proof=proof, arduino_fqbn=proof["fqbn"], platform=proof["core"],
                   board=record["arduino_id"], reason="Exact source not prepared.")
        support.publish_prepared_targets(self.core, root, [row])
        return record

    def test_three_equal_board_names_and_ids_retain_every_source_version(self):
        roots = [self.fixture / f"source-{index}" for index in range(3)]
        records = [self.intent(root, version=f"3.3.{11 + index}") for index, root in enumerate(roots)]
        actual = self.refresh(roots=roots)
        s3 = {name: row for name, row in actual.items() if row.get("arduino_board_id") == "esp32s3"}
        self.assertEqual(len(s3), 3)
        self.assertEqual({row["arduino_source_file"] for row in s3.values()}, {row["source_file"] for row in records})
        self.assertTrue(any("esp32:esp32@3.3.12" in name for name in s3))
        self.assertTrue(any("esp32:esp32@3.3.13" in name for name in s3))
        for info in s3.values():
            self.assert_original_unavailable(info)
        again = self.refresh(roots=list(reversed(roots)), previous=actual)
        self.assertEqual({name: row["arduino_source_file"] for name, row in s3.items()},
                         {name: row["arduino_source_file"] for name, row in again.items() if row.get("arduino_board_id") == "esp32s3"})

    def test_three_equal_core_versions_get_distinct_stable_source_labels(self):
        roots = [self.fixture / f"same-core-{index}" for index in range(3)]
        for root in roots:
            self.intent(root)
        actual = self.refresh(roots=roots)
        labels = {name: row["arduino_source_file"] for name, row in actual.items() if row.get("arduino_board_id") == "esp32s3"}
        self.assertEqual(len(labels), 3)
        again = self.refresh(roots=roots + roots, previous=copy.deepcopy(actual))
        self.assertEqual(labels, {name: row["arduino_source_file"] for name, row in again.items() if row.get("arduino_board_id") == "esp32s3"})

    def test_changed_declaration_bytes_cannot_borrow_primary_intent(self):
        row = self.ready()
        self.source.write_text(self.source.read_text().replace("4MB", "8MB"), encoding="utf-8")
        current = board_catalog._parse_downloaded_arduino_board_files(self.download, force_read=True)[0]
        self.assertIsNone(support.planned_source_target_for_record(current, core=self.core))
        self.assertEqual(row["arduino_source_proof"]["source_sha256"], self.record["source_sha256"])

    def test_changed_source_keeps_original_namespace_without_authorizing_old_proof(self):
        row = self.ready()
        self.source.write_text(self.source.read_text().replace("4MB", "8MB"), encoding="utf-8")
        current = board_catalog._parse_downloaded_arduino_board_files(self.download, force_read=True)[0]
        self.assertIsNone(support.prepared_target_for_record(current, self.candidates()[:1], core=self.core))
        info = self.refresh()[self.record["name"]]
        self.assert_original_unavailable(info)
        self.assertEqual(info["arduino_source_sha256"], current["source_sha256"])
        self.assertEqual(info["arduino_source_proof"], row["arduino_source_proof"])
        self.assertFalse(support.source_target_proof(info))
        self.assertNotIn("flash_mb", info)
        self.assertNotIn("Ready through", info["fallback_notice"])
        with patch.object(board_catalog, "_load_platformio_board_catalog", return_value=self.candidates()[:1]), \
                patch.object(preparation, "_receipt_for_record", return_value=({}, self.download, "Source receipt bytes changed.")), \
                patch.object(preparation, "_platformio_preferences", return_value={}), \
                patch.object(support, "prepare_source_boards") as prepare:
            result = preparation.prepare_sources(self.core, [self.download], emit=Mock())
        prepare.assert_not_called()
        unavailable = next(item for item in result["boards"] if item["arduino_id"] == "esp32s3")
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(unavailable["source_sha256"], current["source_sha256"])
        self.assertEqual(unavailable["arduino_source_proof"], row["arduino_source_proof"])
        self.assertFalse(support.source_target_proof(unavailable))
        # A failed repair publication retains only namespace intention. A later
        # fresh picker still cannot reinterpret this source as physical PIO.
        self.assert_original_unavailable(self.refresh()[self.record["name"]])
        self.assertIsNone(support.planned_source_target_for_record(current, core=self.core))

    def test_changed_source_repreparation_uses_only_new_verified_declaration_proof(self):
        self.ready()
        self.source.write_text(self.source.read_text().replace("4MB", "8MB"), encoding="utf-8")
        current = board_catalog._parse_downloaded_arduino_board_files(self.download, force_read=True)[0]
        metadata = dict(self.metadata, version="3.3.12")
        def failure(core, directory, metadata, rows, **kwargs):
            return [dict(row, status="unavailable", reason="Simulated compiler preparation failure.") for row in rows]
        with patch.object(board_catalog, "_load_platformio_board_catalog", return_value=self.candidates()[:1]), \
                patch.object(preparation, "_receipt_for_record", return_value=(metadata, self.download, "")), \
                patch.object(preparation, "_platformio_preferences", return_value={}), \
                patch.object(support, "prepare_source_boards", side_effect=failure) as prepare:
            preparation.prepare_sources(self.core, [self.download], emit=Mock())
        s3 = next(row for call in prepare.call_args_list for row in call.args[3] if row["arduino_id"] == "esp32s3")
        self.assertEqual(s3["arduino_source_proof"]["source_sha256"], current["source_sha256"])
        self.assertEqual(s3["arduino_source_proof"]["version"], "3.3.12")
        self.assertTrue(support.source_target_proof(s3))
        self.assertIsNone(support.prepared_target_for_record(current, self.candidates()[:1], core=self.core))


if __name__ == "__main__":
    unittest.main(verbosity=2)
