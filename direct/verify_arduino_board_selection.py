#!/usr/bin/env python3
"""Trusted per-board compiler choices, using only isolated settings fixtures."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from src.modules import arduino_board_selection as selection


class SelectionChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/arduino-board-selection"
        audit.mkdir(parents=True, exist_ok=True)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit)))
        self.settings = self.root / "arduino_browser_settings.json"
        self.metadata = {"index_url": "https://vendor.invalid/package_index.json",
                         "package": "rp2040", "architecture": "rp2040", "version": "1.2.3"}
        self.key = selection.association_key(self.metadata)
        self.stack.enter_context(patch.object(selection, "settings_file", return_value=self.settings))
        selection.invalidate_preferences()
        self.addCleanup(selection.invalidate_preferences)

    def write(self, values=None, **other):
        self.settings.write_text(json.dumps(dict(other, **{
            selection.FIELD: values if values is not None else {self.key: ["rpipico2"]}})), encoding="utf-8")

    def test_missing_empty_broken_and_oversized_settings_disable_every_board(self):
        self.assertFalse(selection.board_selected(self.metadata, "rpipico2"))
        for raw in ("{}", "[]", "{invalid", " " * (2 * 1024 * 1024 + 1)):
            with self.subTest(raw=raw[:30]):
                self.settings.write_text(raw, encoding="utf-8")
                self.assertEqual(selection.load_preferences(force_read=True), {})
                self.assertFalse(selection.board_selected(self.metadata, "rpipico2"))

    def test_choice_is_scoped_to_exact_index_package_architecture_and_board(self):
        self.write()
        self.assertTrue(selection.board_selected(self.metadata, "rpipico2"))
        self.assertFalse(selection.board_selected(self.metadata, "rpipico2w"))
        for changes in ({"package": "other"}, {"architecture": "other"},
                        {"index_url": "https://other.invalid/package_index.json"}):
            with self.subTest(changes=changes):
                self.assertFalse(selection.board_selected(dict(self.metadata, **changes), "rpipico2"))
        self.assertTrue(selection.board_selected(dict(self.metadata, version="9.9.9"), "rpipico2"))

    def test_bad_metadata_and_identifiers_never_grant_selection(self):
        self.write({self.key: ["rpipico2", "../outside", "rp2040:rp2040:one", True, None, "bad id"]})
        self.assertEqual(selection.selected_boards(self.metadata), frozenset({"rpipico2"}))
        for changes in ({"package": "../outside"}, {"architecture": "bad id"},
                        {"index_url": "https://user:secret@vendor.invalid/index.json"},
                        {"index_url": "https://vendor.invalid/index.json#fragment"},
                        {"index_url": "https://[broken"}, {"index_url": "file:///index.json"}):
            with self.subTest(changes=changes):
                self.assertEqual(selection.association_key(dict(self.metadata, **changes)), "")
                self.assertFalse(selection.board_selected(dict(self.metadata, **changes), "rpipico2"))
        self.assertEqual(selection.association_key(None), "")

    def test_malformed_values_and_noncanonical_keys_fail_closed(self):
        for values in ([], {self.key: "rpipico2"}, {self.key: {"rpipico2": True}},
                       {json.dumps(json.loads(self.key)): ["rpipico2"]}, {"bad key": ["rpipico2"]}):
            with self.subTest(values=values):
                self.write(values)
                self.assertEqual(selection.load_preferences(force_read=True), {})

    def test_receipt_selection_identity_never_enables_without_local_choice(self):
        row = {"arduino_id": "rpipico2", "arduino_fqbn": "rp2040:rp2040:rpipico2",
               "arduino_cli_selection": selection.selection_identity(self.metadata),
               "backend": "arduino-cli", "status": "ready", "arduino_cli_selected": True}
        self.assertFalse(selection.selection_for_row(row))
        self.write()
        self.assertTrue(selection.selection_for_row(row))
        for changes in ({"arduino_fqbn": "rp2040:rp2040:rpipico2w"}, {"arduino_id": "rpipico2w"},
                        {"arduino_cli_selection": {}}, {"arduino_cli_selection": None}):
            with self.subTest(changes=changes):
                self.assertFalse(selection.selection_for_row(dict(row, **changes)))
        self.assertFalse(selection.selection_for_row(dict(row, arduino_fqbn=None, arduino_cli=[])))
        self.assertFalse(selection.selection_for_row(dict(row, arduino_fqbn=None, arduino_cli="bad")))

    def test_original_source_receipt_must_bind_the_same_selected_fqbn(self):
        self.write()
        row = {"arduino_id": "rpipico2", "arduino_backend_role": "primary", "arduino_source_proof": {
            "core": "rp2040:rp2040", "index_url": self.metadata["index_url"], "fqbn": "rp2040:rp2040:rpipico2"}}
        self.assertTrue(selection.selection_for_row(row))
        for key, value in (("core", "vendor:other"), ("fqbn", "rp2040:rp2040:two"),
                           ("index_url", "https://other.invalid/index.json")):
            changed = copy.deepcopy(row)
            changed["arduino_source_proof"][key] = value
            self.assertFalse(selection.selection_for_row(changed))
        for changes in ({"arduino_backend_role": "fallback"},
                        {"arduino_fqbn": "other:cpu:rpipico2"},
                        {"arduino_cli": {"fqbn": "other:cpu:rpipico2"}}):
            changed = dict(row, **changes)
            self.assertFalse(selection.row_allowed(changed))
            self.assertFalse(selection.selection_for_row(changed))

    def test_conflicting_legacy_cli_fqbn_cannot_borrow_an_allowed_identity(self):
        self.write()
        row = {"arduino_id": "rpipico2", "arduino_fqbn": "rp2040:rp2040:rpipico2",
               "arduino_cli_selection": selection.selection_identity(self.metadata),
               "arduino_cli": {"fqbn": "other:cpu:rpipico2"}}
        self.assertFalse(selection.row_allowed(row))
        self.assertFalse(selection.selection_for_row(row))

    def test_action_force_read_revokes_choice_despite_unchanged_file_stat(self):
        self.write({self.key: ["rpipico2"]})
        stat = self.settings.stat()
        original_stat = Path.stat
        def same_stamp(path, *args, **kwargs):
            return stat if path == self.settings else original_stat(path, *args, **kwargs)
        with patch.object(Path, "stat", same_stamp):
            self.assertTrue(selection.board_selected(self.metadata, "rpipico2"))
            self.write({self.key: ["rpipico2w"]})
            preferences = selection.load_preferences(force_read=True)
            self.assertFalse(selection.board_selected(self.metadata, "rpipico2", preferences))
            self.assertFalse(selection.board_selected(self.metadata, "rpipico2w", preferences))
            self.assertFalse(selection.board_selected(self.metadata, "rpipico2"))

    def test_catalog_fingerprint_ignores_unrelated_preferences_but_tracks_choices(self):
        self.write(theme="dark")
        first = selection.preferences_fingerprint()
        self.write(theme="light")
        selection.invalidate_preferences()
        self.assertEqual(selection.preferences_fingerprint(), first)
        self.write({self.key: ["rpipico2w"]}, theme="light")
        selection.invalidate_preferences()
        self.assertNotEqual(selection.preferences_fingerprint(), first)

    def test_reader_does_not_modify_settings_or_create_package_resources(self):
        self.write()
        before = self.settings.read_bytes(), self.settings.stat().st_mtime_ns
        self.assertTrue(selection.board_selected(self.metadata, "rpipico2"))
        self.assertEqual((self.settings.read_bytes(), self.settings.stat().st_mtime_ns), before)
        self.assertEqual(list(self.root.iterdir()), [self.settings])

    def test_only_exact_uno_q_and_pico_2_identities_are_eligible(self):
        uno = {"package": "arduino", "architecture": "zephyr", "index_url": ""}
        pico = {"package": "rp2040", "architecture": "rp2040", "index_url": ""}
        self.assertEqual(selection.allowed_board_ids(uno), frozenset({"unoq"}))
        self.assertEqual(selection.allowed_board_ids(pico), frozenset({"rpipico2"}))
        self.assertTrue(selection.board_allowed(uno, "unoq"))
        self.assertTrue(selection.board_allowed(pico, "rpipico2"))
        for metadata, identifiers in ((uno, ("uno", "ventunoq", "rpipico2", "unoq_variant")),
                                      (pico, ("rpipico", "rpipico2w", "rp2350", "generic", "unoq")),
                                      ({"package": "esp32", "architecture": "esp32"}, ("esp32", "esp32s3")),
                                      ({"package": "other", "architecture": "rp2040"}, ("rpipico2",)),
                                      ({"package": "arduino", "architecture": "avr"}, ("unoq", "uno"))):
            for identifier in identifiers:
                with self.subTest(metadata=metadata, identifier=identifier):
                    self.assertFalse(selection.board_allowed(dict(metadata, name="Arduino UNO Q",
                                                               mcu="rp2350"), identifier))

    def test_stale_other_board_choices_are_read_only_and_cannot_enable_cli(self):
        uno = {"package": "arduino", "architecture": "zephyr", "index_url": ""}
        esp = {"package": "esp32", "architecture": "esp32", "index_url": ""}
        self.write({self.key: ["rpipico2", "rpipico2w", "generic"],
                    selection.association_key(uno): ["unoq", "ventunoq"],
                    selection.association_key(esp): ["esp32s3"]})
        before = self.settings.read_bytes()
        preferences = selection.load_preferences(force_read=True)
        self.assertEqual(selection.selected_boards(self.metadata, preferences), frozenset({"rpipico2"}))
        self.assertEqual(selection.selected_boards(uno, preferences), frozenset({"unoq"}))
        self.assertEqual(selection.selected_boards(esp, preferences), frozenset())
        self.assertEqual(before, self.settings.read_bytes())
        for metadata, identifier in ((uno, "ventunoq"), (self.metadata, "rpipico2w"), (esp, "esp32s3")):
            row = {"backend": "arduino-cli", "arduino_id": identifier,
                   "arduino_fqbn": f"{metadata['package']}:{metadata['architecture']}:{identifier}",
                   "arduino_cli_selection": selection.selection_identity(metadata)}
            self.assertFalse(selection.row_allowed(row))
            self.assertFalse(selection.selection_for_row(row, preferences))

    def test_stale_cli_target_rejects_before_package_repair_guidance(self):
        from main.core.target_profile import target_problem
        row = {"backend": "arduino-cli", "arduino_id": "rpipico2w",
               "arduino_fqbn": "rp2040:rp2040:rpipico2w",
               "arduino_cli_selection": selection.selection_identity(self.metadata)}
        message = target_problem(row)
        self.assertIn("only for Arduino UNO Q", message)
        self.assertIn("PlatformIO", message)
        self.assertNotIn("Repair", message)


class SettingsOwnerChecks(unittest.TestCase):
    def test_index_directory_owner_does_not_borrow_stale_database_preferences(self):
        audit = ROOT / "temp/audit/arduino-board-selection"
        audit.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=audit) as directory:
            root = Path(directory)
            index = root / "index_json"
            database = root / "src/dbs"
            database.mkdir(parents=True)
            stale = database / "arduino_browser_settings.json"
            stale.write_text("{}", encoding="utf-8")
            with patch("main.core.constants.SCRIPT_DIR", root):
                self.assertEqual(selection.settings_file(), stale)
                index.mkdir()
                self.assertEqual(selection.settings_file(), index / "arduino_browser_settings.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
