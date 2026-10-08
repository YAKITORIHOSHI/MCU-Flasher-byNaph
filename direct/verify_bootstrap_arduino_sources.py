#!/usr/bin/env python3
"""Isolated exact source-target preparation checks; installers are mocked."""
from __future__ import annotations

import copy
import ast
import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from main.core import board_catalog
from src.modules import arduino_cli_support
from src.modules import bootstrap_arduino_sources as worker

_REAL_PREPARE = arduino_cli_support.prepare_source_boards
_REAL_LOAD = arduino_cli_support.load_prepared_targets
_REAL_LOOKUP = arduino_cli_support.prepared_target_for_record
_REAL_PUBLISH = arduino_cli_support.publish_prepared_targets
_REAL_PREFERENCES = worker._platformio_preferences


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SourceChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/bootstrap-arduino-sources"
        audit.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.core = self.root / "store"
        self.core.mkdir()
        self.archive = self.root / "downloaded"
        self.source = self.archive / "nested-core"
        self.source.mkdir(parents=True)
        self.boards = self.source / "boards.txt"
        self.platform = self.source / "platform.txt"
        self.boards.write_text(
            "ambig.name=Generic fixture board\nambig.build.mcu=fixturecpu\nambig.build.variant=shared\n"
            "absent.name=Unmatched vendor board\nabsent.build.mcu=absentcpu\n"
            "native.name=Exact PlatformIO Board\nnative.build.mcu=nativecpu\nnative.build.variant=native\n"
            "hidden.name=Hidden internal board\nhidden.hide=true\nhidden.build.mcu=hidden\n", encoding="utf-8")
        self.platform.write_text("name=Fixture Core\nversion=1.2.3\n", encoding="utf-8")
        self.receipt = {"schema": 1, "authority": "official-index", "package": "fixture-vendor",
                        "architecture": "fixturecpu", "version": "1.2.3", "name": "Fixture Core",
                        "index_url": "https://vendor.invalid/package_index.json", "archive_sha256": "a" * 64,
                        "source_files": {"nested-core/boards.txt": digest(self.boards),
                                         "nested-core/platform.txt": digest(self.platform)}}
        self.write_receipt()
        self.catalog = [self.candidate("a", "Generic fixture board", "fixturecpu", "shared"),
                        self.candidate("b", "Generic fixture board", "fixturecpu", "shared"),
                        self.candidate("native", "Exact PlatformIO Board", "nativecpu", "native")]
        self.prepared = None
        self.events = []
        self.preferences = {}
        self.archives = {self.archive}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {"MCU_PACKAGE_EVENTS_ROOT": str(self.root / "events")}))
        self.loader = stack.enter_context(patch.object(board_catalog, "_load_platformio_board_catalog",
                                                       side_effect=lambda *_a, **_k: copy.deepcopy(self.catalog)))
        self.parser = stack.enter_context(patch.object(board_catalog, "_parse_downloaded_arduino_board_files",
                                                       wraps=board_catalog._parse_downloaded_arduino_board_files))
        stack.enter_context(patch.object(board_catalog, "_get_arduino_board_search_roots",
                                         side_effect=AssertionError("No live source discovery")))
        stack.enter_context(patch.object(worker, "_platformio_preferences", side_effect=lambda: self.preferences))
        self.previous = stack.enter_context(patch.object(arduino_cli_support, "load_prepared_targets", return_value=[]))
        self.prepared_lookup = stack.enter_context(patch.object(
            arduino_cli_support, "prepared_target_for_record", side_effect=self.lookup_prepared))
        self.installer = stack.enter_context(patch.object(arduino_cli_support, "prepare_source_boards",
                                                          side_effect=self.prepare_fixture))
        self.publisher = stack.enter_context(patch.object(arduino_cli_support, "publish_prepared_targets"))
        stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("No fixture network")))
        stack.enter_context(patch("subprocess.run", side_effect=AssertionError("No native installer execution")))

    def candidate(self, identifier, name, mcu, variant):
        return {"platform": "fixture-platform", "id": identifier, "name": name, "vendor": "",
                "frameworks": {"arduino"}, "mcu": mcu, "variant": variant, "hwids": set(),
                "arduino_defines": set(), "manifest": str(self.core / f"{identifier}.json")}

    def write_receipt(self):
        (self.archive / worker.RECEIPT).write_text(json.dumps(self.receipt), encoding="utf-8")

    def lookup_prepared(self, record, _catalog, **_kwargs):
        return copy.deepcopy(self.prepared) if self.prepared and record["arduino_id"] == "ambig" else None

    def prepare_fixture(self, core, directory, metadata, rows, *, emit, jobs=None):
        self.assertEqual(Path(core), self.core)
        self.assertIn(Path(directory), self.archives)
        self.assertEqual(metadata["version"], "1.2.3")
        output = copy.deepcopy(rows)
        for row in output:
            self.assertTrue(arduino_cli_support.source_target_proof(row))
            self.assertEqual(row["platformio_support"], "unknown")
            row.update(status="ready", backend="arduino-cli", platform="fixture-vendor:fixturecpu",
                       board=row["arduino_id"], reason="Mocked exact core and builder certificates verified.")
        return output

    def run_worker(self, sources=None):
        return worker.prepare_sources(self.core, [self.archive] if sources is None else sources,
                                      emit=lambda stage, **details: self.events.append((stage, details)), jobs=2)

    def row(self, report, identifier):
        return next(row for row in report["boards"] if row["arduino_id"] == identifier)

    def test_all_visible_unrepresented_boards_prepare_exact_primary_fqbns(self):
        report = self.run_worker()
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["ready_count"], 2)
        self.assertEqual(report["unavailable_count"], 0)
        self.assertEqual(report["not_required_count"], 1)
        self.installer.assert_called_once()
        requested = self.installer.call_args.args[3]
        self.assertEqual({row["arduino_id"] for row in requested}, {"ambig", "absent"})
        self.assertEqual({row["arduino_source_proof"]["fqbn"] for row in requested},
                         {"fixture-vendor:fixturecpu:ambig", "fixture-vendor:fixturecpu:absent"})
        self.assertEqual(self.row(report, "native")["status"], "not_required")
        self.assertEqual(self.row(report, "ambig")["arduino_backend_role"], "primary")
        self.publisher.assert_called_once()
        self.loader.assert_called_once_with(self.core, force_read=True)
        self.parser.assert_called_once_with(self.archive, force_read=True)

    def test_missing_receipt_exposes_each_unavailable_board_without_installing(self):
        (self.archive / worker.RECEIPT).unlink()
        report = self.run_worker()
        self.assertEqual(report["ready_count"], 0)
        self.assertEqual(report["unavailable_count"], 2)
        self.assertIn("No verified", self.row(report, "ambig")["reason"])
        self.installer.assert_not_called()

    def test_changed_platform_source_receipt_blocks_primary_preparation(self):
        self.platform.write_text("name=changed\n", encoding="utf-8")
        report = self.run_worker()
        self.assertEqual(report["unavailable_count"], 2)
        self.installer.assert_not_called()

    def test_mismatched_board_receipt_cannot_authorize_current_declarations(self):
        self.boards.write_text(self.boards.read_text(encoding="utf-8") + "# changed bytes\n", encoding="utf-8")
        report = self.run_worker()
        self.assertEqual(report["unavailable_count"], 2)
        self.installer.assert_not_called()

    def test_valid_existing_primary_target_reuses_preparation(self):
        record = next(row for row in board_catalog._parse_downloaded_arduino_board_files(self.archive, force_read=True)
                      if row["arduino_id"] == "ambig")
        self.prepared = {"status": "ready", "backend": "arduino-cli", "arduino_id": "ambig",
                         "source_file": str(self.boards), "source_sha256": digest(self.boards),
                         "arduino_backend_role": "primary",
                         "arduino_source_proof": arduino_cli_support.source_declaration_proof(record, self.receipt),
                         "platform": "fixture-vendor:fixturecpu", "board": "ambig"}
        report = self.run_worker()
        self.assertEqual(self.row(report, "ambig")["status"], "ready")
        requested = self.installer.call_args.args[3]
        self.assertEqual([row["arduino_id"] for row in requested], ["absent"])

    def test_explicit_platformio_receipt_preserves_requested_backend(self):
        self.receipt["platformio"] = {"platform": "owner/custom@1.0", "board_ids": {"ambig": "exact"}}
        self.write_receipt()
        report = self.run_worker()
        self.assertEqual(report["not_required_count"], 3)
        self.assertEqual(report["ready_count"], 0)
        self.installer.assert_not_called()

    def test_explicit_saved_platformio_preference_takes_precedence(self):
        key = json.dumps([self.receipt["index_url"], self.receipt["package"], self.receipt["architecture"]],
                         ensure_ascii=False, separators=(",", ":"))
        self.preferences[key] = {"platform": "owner/custom@1.0", "board_ids": {"ambig": "exact"}}
        report = self.run_worker()
        self.assertEqual(report["not_required_count"], 3)
        self.installer.assert_not_called()

    def test_explicit_preference_preserves_existing_verified_platformio_association(self):
        old = {"status": "ready", "backend": "platformio", "arduino_id": "ambig",
               "source_file": str(self.boards), "source_sha256": digest(self.boards),
               "platform": "owner/custom", "board": "exact"}
        self.previous.return_value = [old]
        self.prepared = dict(old, id="exact")
        self.receipt["platformio"] = {"platform": "owner/custom@1.0", "board_ids": {"ambig": "exact"}}
        self.write_receipt()
        self.assertEqual(self.run_worker()["not_required_count"], 3)
        self.installer.assert_not_called()
        self.publisher.assert_not_called()

    def test_explicit_preference_revokes_only_conflicting_primary_source_association(self):
        old = {"status": "ready", "backend": "arduino-cli", "arduino_backend_role": "primary",
               "arduino_id": "ambig", "source_file": str(self.boards), "source_sha256": digest(self.boards)}
        other = {"status": "ready", "backend": "platformio", "arduino_id": "native",
                 "source_file": str(self.boards), "source_sha256": digest(self.boards), "board": "native"}
        self.previous.return_value = [old, other]
        self.prepared = old
        self.receipt["platformio"] = {"platform": "owner/custom@1.0", "board_ids": {"ambig": "exact"}}
        self.write_receipt()
        self.run_worker()
        self.installer.assert_not_called()
        self.publisher.assert_called_once()
        published = self.publisher.call_args.args[2]
        self.assertNotIn(old, published)
        self.assertIn(other, published)

    def test_traversal_receipt_cannot_escape_requested_root(self):
        self.receipt["source_files"]["../outside.txt"] = "b" * 64
        self.write_receipt()
        self.assertEqual(self.run_worker()["unavailable_count"], 2)
        self.installer.assert_not_called()

    def test_install_failure_stays_unavailable_and_never_claims_primary_ready(self):
        self.installer.side_effect = RuntimeError("Fixture exact builder failed")
        report = self.run_worker()
        self.assertEqual(report["ready_count"], 0)
        self.assertEqual(report["unavailable_count"], 2)
        self.assertIn("Fixture exact builder failed", self.row(report, "ambig")["reason"])

    def test_multiple_source_roots_use_exact_file_identity_and_do_not_duplicate(self):
        report = self.run_worker([self.archive, self.archive])
        self.assertEqual(report["total"], 3)
        self.installer.assert_called_once()

    def test_identical_core_folder_names_are_grouped_by_exact_source_file(self):
        other_archive = self.root / "another-download"
        other_source = other_archive / "nested-core"
        other_source.mkdir(parents=True)
        (other_source / "boards.txt").write_bytes(self.boards.read_bytes())
        (other_source / "platform.txt").write_bytes(self.platform.read_bytes())
        (other_archive / worker.RECEIPT).write_text(json.dumps(self.receipt), encoding="utf-8")
        self.archives.add(other_archive)
        report = self.run_worker([self.archive, other_archive])
        self.assertEqual(report["total"], 6)
        self.assertEqual(report["ready_count"], 4)
        self.assertEqual(self.installer.call_count, 2)
        self.assertEqual({Path(call.args[1]) for call in self.installer.call_args_list}, self.archives)

    def test_untouched_source_associations_survive_publication(self):
        old = {"status": "ready", "backend": "arduino-cli", "arduino_id": "other",
               "source_file": str(self.source / "other-source/boards.txt"), "source_sha256": "c" * 64}
        self.previous.return_value = [old]
        self.run_worker()
        self.assertIn(old, self.publisher.call_args.args[2])

    def test_unverified_installer_completion_cannot_claim_ready(self):
        self.installer.side_effect = lambda _core, _directory, _metadata, rows, **_kwargs: copy.deepcopy(rows)
        report = self.run_worker()
        self.assertEqual(report["ready_count"], 0)
        self.assertEqual(report["unavailable_count"], 2)

    def test_workspace_runtime_cannot_invoke_preparation(self):
        with patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "1"}):
            with self.assertRaisesRegex(RuntimeError, "separate bootstrap worker"):
                self.run_worker()
        self.installer.assert_not_called()
        self.loader.assert_not_called()

    def test_empty_sources_never_scan_or_prepare_live_packages(self):
        self.assertEqual(self.run_worker([])["total"], 0)
        self.installer.assert_not_called()
        self.publisher.assert_not_called()

    def test_source_receipt_authority_uses_exact_builtin_index_identity(self):
        receipt = dict(self.receipt, package="esp32", architecture="esp32",
                       index_url="https://espressif.github.io/arduino-esp32/package_esp32_index.json")
        self.assertTrue(worker.source_receipt_metadata(receipt))
        receipt["index_url"] = "https://unrelated.invalid/package_index.json"
        self.assertEqual(worker.source_receipt_metadata(receipt), {})
        receipt["index_url"] = "https://espressif.github.io/arduino-esp32/package_esp32_index.json"
        receipt["archive_sha256"] = "not-a-receipt"
        self.assertEqual(worker.source_receipt_metadata(receipt), {})

    def test_malformed_index_uri_does_not_abort_source_enumeration(self):
        self.receipt["index_url"] = "https://[malformed-index"
        self.write_receipt()
        self.assertEqual(worker.source_receipt_metadata(self.receipt), {})
        self.assertEqual(self.run_worker()["unavailable_count"], 2)
        self.installer.assert_not_called()

    def test_source_digest_work_is_bounded_per_source_file(self):
        with patch.object(worker, "_digest", wraps=worker._digest) as hashing:
            self.run_worker()
        # One original digest and one complete source-receipt verification,
        # rather than another full boards.txt read for every declaration.
        self.assertEqual(sum(Path(call.args[0]) == self.boards for call in hashing.call_args_list), 2)

    def test_browser_settings_owner_and_literal_association_key_are_preserved(self):
        settings_root = self.root / "settings-owner"
        index = settings_root / "index_json"
        database = settings_root / "src/dbs"
        index.mkdir(parents=True)
        database.mkdir(parents=True)
        stale = {"board_platformio_associations": {"stale": {"platform": "owner/stale"}}}
        (database / "arduino_browser_settings.json").write_text(json.dumps(stale), encoding="utf-8")
        with patch("main.core.constants.SCRIPT_DIR", settings_root):
            self.assertEqual(_REAL_PREFERENCES(), {})
            key = json.dumps([self.receipt["index_url"], self.receipt["package"], self.receipt["architecture"]],
                             ensure_ascii=False, separators=(",", ":"))
            association = {key: {"platform": "owner/exact"}}
            (index / "arduino_browser_settings.json").write_text(
                json.dumps({"board_platformio_associations": association}), encoding="utf-8")
            self.assertEqual(_REAL_PREFERENCES(), association)
            self.assertTrue(worker._explicit_platformio(self.receipt, _REAL_PREFERENCES()))

    def test_real_orchestration_publishes_and_reuses_exact_certificates_with_native_calls_mocked(self):
        from main.core import toolchain
        declaration = board_catalog._parse_downloaded_arduino_board_files(self.archive, force_read=True)[0]
        store = arduino_cli_support._source_store(self.core, arduino_cli_support.source_declaration_proof(declaration, self.receipt))
        installed = store / "data/packages/fixture-vendor/hardware/fixturecpu/1.2.3"
        installed.mkdir(parents=True)
        (installed / "boards.txt").write_bytes(self.boards.read_bytes())
        (installed / "platform.txt").write_bytes(self.platform.read_bytes())
        tool = store / "data/packages/fixture-vendor/tools/compiler/1.0/bin/compiler.exe"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(b"isolated tool bytes")
        cli = self.root / "arduino-cli.exe"
        cli.write_bytes(b"isolated CLI bytes")
        commands = []
        def run(command, **_kwargs):
            commands.append(command)
            if "list" in command:
                return {"platforms": [{"id": "fixture-vendor:fixturecpu", "installed_version": "1.2.3"}]}
            if "details" in command:
                return {"fqbn": command[command.index("--fqbn") + 1],
                        "tools_dependencies": [{"packager": "fixture-vendor", "name": "compiler", "version": "1.0"}]}
            return None
        with patch.object(arduino_cli_support, "prepare_source_boards", _REAL_PREPARE), \
                patch.object(arduino_cli_support, "load_prepared_targets", _REAL_LOAD), \
                patch.object(arduino_cli_support, "prepared_target_for_record", _REAL_LOOKUP), \
                patch.object(arduino_cli_support, "publish_prepared_targets", _REAL_PUBLISH), \
                patch.object(arduino_cli_support, "_run_json", side_effect=run), \
                patch.object(toolchain, "find_arduino_cli_executable", return_value=str(cli)), \
                patch("main.core.build_resources.get_optimal_compiler_jobs", return_value=2):
            first = self.run_worker()
            self.assertEqual(first["ready_count"], 2, first)
            persisted = _REAL_LOAD(self.core)
            self.assertEqual({row["arduino_id"] for row in persisted}, {"ambig", "absent"})
            self.assertTrue(all(arduino_cli_support.source_target_proof(row) for row in persisted))
            probes = [command for command in commands if "compile" in command]
            self.assertEqual({command[command.index("--fqbn") + 1] for command in probes},
                             {"fixture-vendor:fixturecpu:ambig", "fixture-vendor:fixturecpu:absent"})
            self.assertFalse(any("upload" in command for command in commands))
            commands.clear()
            second = self.run_worker()
            self.assertEqual(second["ready_count"], 2, second)
            self.assertEqual(commands, [], "Valid source certificates must not reinstall or replay probes")


class ReceiptChecks(unittest.TestCase):
    """Execute only receipt helpers, never import the setup/install module."""
    def setUp(self):
        audit = ROOT / "temp/audit/bootstrap-arduino-sources"
        audit.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "old-folder-label-1.8.5"
        self.source.mkdir()
        self.boards = self.source / "boards.txt"
        self.platform = self.source / "platform.txt"
        self.boards.write_text("uno.name=Arduino Uno\nuno.build.mcu=atmega328p\n", encoding="utf-8")
        self.platform.write_text("name=Arduino AVR Boards\nversion=1.8.6\n", encoding="utf-8")
        self.archive = self.root / "official-1.8.6.zip"
        with zipfile.ZipFile(self.archive, "w") as bundle:
            bundle.writestr("vendor-core/boards.txt", self.boards.read_bytes())
            bundle.writestr("vendor-core/platform.txt", self.platform.read_bytes())
        names = {"_write_bootstrap_board_source_receipt", "_verified_bootstrap_board_receipt",
                 "_adopt_existing_bootstrap_board_source", "_archive_source_pairs",
                 "_normalize_sha256", "_arduino_avr_release_from_index", "_existing_bootstrap_board_source"}
        source = ast.parse((ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig"))
        selected = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual({node.name for node in selected}, names)
        self.warnings = []
        self.namespace = {"Path": Path, "json": json, "hashlib": hashlib, "re": __import__("re"),
                          "tempfile": tempfile, "SCRIPT_DIR": self.root,
                          "_BOARD_SOURCE_RECEIPT": worker.RECEIPT, "_file_sha256": digest,
                          "ESP32_BOARD_INDEX_URL": worker._BUILTIN_INDEXES[("esp32", "esp32")],
                          "ARDUINO_BOARD_INDEX_URL": worker._BUILTIN_INDEXES[("arduino", "avr")],
                          "warn": self.warnings.append, "ok": lambda _message: None}
        exec(compile(ast.Module(body=selected, type_ignores=[]), "isolated-bootstrap-receipts", "exec"), self.namespace)
        self.release = {"version": "1.8.6", "version_base": "1.8.6", "archive": "official-1.8.6.zip",
                        "url": "https://official.invalid/avr.zip", "size": self.archive.stat().st_size,
                        "sha256": digest(self.archive)}
        self.namespace["_load_arduino_avr_board_release"] = lambda version: self.release if version == "1.8.6" else None
        self.downloads = []
        def download(url, destination, **options):
            self.downloads.append((url, options))
            self.assertEqual(options["expected_sha256"], digest(self.archive))
            self.assertEqual(options["expected_size"], self.archive.stat().st_size)
            Path(destination).write_bytes(self.archive.read_bytes())
        self.namespace["_download_file"] = download

    def adopt(self):
        return self.namespace["_adopt_existing_bootstrap_board_source"](
            self.boards, package="arduino", architecture="avr",
            index_url=worker._BUILTIN_INDEXES[("arduino", "avr")])

    def test_legacy_folder_adoption_uses_verified_official_bytes_and_actual_version(self):
        original = (digest(self.boards), digest(self.platform))
        self.assertTrue(self.adopt())
        receipt = json.loads((self.source / worker.RECEIPT).read_text(encoding="utf-8"))
        self.assertEqual(receipt["version"], "1.8.6")
        self.assertEqual(receipt["archive_sha256"], digest(self.archive))
        self.assertEqual((digest(self.boards), digest(self.platform)), original)
        self.assertTrue(worker.source_receipt_metadata(receipt))
        self.assertTrue(self.namespace["_verified_bootstrap_board_receipt"](
            self.boards, self.root, "arduino", "avr"))
        self.assertEqual(len(self.downloads), 1)

    def test_legacy_source_mismatch_retains_source_and_does_not_publish_receipt(self):
        self.boards.write_text(self.boards.read_text(encoding="utf-8") + "# user-owned difference\n", encoding="utf-8")
        original = (digest(self.boards), digest(self.platform))
        self.assertFalse(self.adopt())
        self.assertEqual((digest(self.boards), digest(self.platform)), original)
        self.assertFalse((self.source / worker.RECEIPT).exists())
        self.assertTrue(self.warnings)

    def test_missing_exact_official_version_never_downloads_or_labels_legacy_sources(self):
        self.namespace["_load_arduino_avr_board_release"] = lambda _version: None
        self.assertFalse(self.adopt())
        self.assertEqual(self.downloads, [])
        self.assertFalse((self.source / worker.RECEIPT).exists())

    def test_receipt_writer_binds_nested_source_pairs_and_equal_writes_are_noops(self):
        nested = self.source / "another-core"
        nested.mkdir()
        (nested / "boards.txt").write_bytes(self.boards.read_bytes())
        (nested / "platform.txt").write_bytes(self.platform.read_bytes())
        write = self.namespace["_write_bootstrap_board_source_receipt"]
        options = {"package": "arduino", "architecture": "avr", "version": "1.8.6",
                   "index_url": worker._BUILTIN_INDEXES[("arduino", "avr")], "archive_sha256": digest(self.archive)}
        receipt = write(self.source, **options)
        self.assertEqual(len(receipt["source_files"]), 4)
        path = self.source / worker.RECEIPT
        before = path.stat().st_mtime_ns
        self.assertEqual(write(self.source, **options), receipt)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_verified_receipt_is_found_after_unverified_folder_and_with_arbitrary_name(self):
        bad = self.root / "000-avr-custom"
        bad.mkdir()
        (bad / "boards.txt").write_text("custom.name=Custom board\n", encoding="utf-8")
        self.assertTrue(self.adopt())
        renamed = self.root / "zzz-verified-source"
        self.source.rename(renamed)
        with patch.dict(self.namespace, {"_adopt_existing_bootstrap_board_source":
                lambda *_a, **_k: self.fail("A valid receipt must be reused before any online adoption")}):
            self.assertTrue(self.namespace["_existing_bootstrap_board_source"](
                self.root, package="arduino", architecture="avr",
                index_url=worker._BUILTIN_INDEXES[("arduino", "avr")]))
        self.assertEqual((bad / "boards.txt").read_text(encoding="utf-8"), "custom.name=Custom board\n")

    def test_source_root_receipt_is_reused_without_online_adoption(self):
        options = {"package": "arduino", "architecture": "avr", "version": "1.8.6",
                   "index_url": worker._BUILTIN_INDEXES[("arduino", "avr")], "archive_sha256": digest(self.archive)}
        self.namespace["_write_bootstrap_board_source_receipt"](self.source, **options)
        with patch.dict(self.namespace, {"_adopt_existing_bootstrap_board_source":
                lambda *_a, **_k: self.fail("A receipt at the source root must be reused")}):
            self.assertTrue(self.namespace["_existing_bootstrap_board_source"](
                self.source, package="arduino", architecture="avr",
                index_url=worker._BUILTIN_INDEXES[("arduino", "avr")]))

    def test_legacy_adoption_continues_after_unverified_candidate(self):
        bad = self.root / "000-avr-custom"
        bad.mkdir()
        (bad / "boards.txt").write_text("custom.name=Custom board\n", encoding="utf-8")
        good = self.root / "zzz-avr-legacy"
        self.source.rename(good)
        self.boards = good / "boards.txt"
        visited = []
        def adopt(path, **_options):
            visited.append(Path(path))
            return Path(path) == self.boards
        with patch.dict(self.namespace, {"_adopt_existing_bootstrap_board_source": adopt}):
            self.assertTrue(self.namespace["_existing_bootstrap_board_source"](
                self.root, package="arduino", architecture="avr",
                index_url=worker._BUILTIN_INDEXES[("arduino", "avr")]))
        self.assertEqual(visited, [bad / "boards.txt", self.boards])

    def test_unverified_sources_are_retained_and_no_family_sources_allow_fresh_preparation(self):
        before = self.boards.read_bytes(), self.platform.read_bytes()
        with patch.dict(self.namespace, {"_adopt_existing_bootstrap_board_source": lambda *_a, **_k: False}):
            self.assertFalse(self.namespace["_existing_bootstrap_board_source"](
                self.root, package="arduino", architecture="avr",
                index_url=worker._BUILTIN_INDEXES[("arduino", "avr")]))
            self.assertIsNone(self.namespace["_existing_bootstrap_board_source"](
                self.root, package="esp32", architecture="esp32",
                index_url=worker._BUILTIN_INDEXES[("esp32", "esp32")]))
        self.assertEqual((self.boards.read_bytes(), self.platform.read_bytes()), before)
        self.assertFalse((self.source / worker.RECEIPT).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
