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
                         "package": "vendor", "architecture": "cpu", "version": "1.2.3"}
        self.key = selection.association_key(self.metadata)
        self.stack.enter_context(patch.object(selection, "settings_file", return_value=self.settings))
        selection.invalidate_preferences()
        self.addCleanup(selection.invalidate_preferences)

    def write(self, values=None, **other):
        self.settings.write_text(json.dumps(dict(other, **{
            selection.FIELD: values if values is not None else {self.key: ["one"]}})), encoding="utf-8")

    def test_missing_empty_broken_and_oversized_settings_disable_every_board(self):
        self.assertFalse(selection.board_selected(self.metadata, "one"))
        for raw in ("{}", "[]", "{invalid", " " * (2 * 1024 * 1024 + 1)):
            with self.subTest(raw=raw[:30]):
                self.settings.write_text(raw, encoding="utf-8")
                self.assertEqual(selection.load_preferences(force_read=True), {})
                self.assertFalse(selection.board_selected(self.metadata, "one"))

    def test_choice_is_scoped_to_exact_index_package_architecture_and_board(self):
        self.write()
        self.assertTrue(selection.board_selected(self.metadata, "one"))
        self.assertFalse(selection.board_selected(self.metadata, "two"))
        for changes in ({"package": "other"}, {"architecture": "other"},
                        {"index_url": "https://other.invalid/package_index.json"}):
            with self.subTest(changes=changes):
                self.assertFalse(selection.board_selected(dict(self.metadata, **changes), "one"))
        self.assertTrue(selection.board_selected(dict(self.metadata, version="9.9.9"), "one"))

    def test_bad_metadata_and_identifiers_never_grant_selection(self):
        self.write({self.key: ["one", "../outside", "vendor:cpu:one", True, None, "bad id"]})
        self.assertEqual(selection.selected_boards(self.metadata), frozenset({"one"}))
        for changes in ({"package": "../outside"}, {"architecture": "bad id"},
                        {"index_url": "https://user:secret@vendor.invalid/index.json"},
                        {"index_url": "https://vendor.invalid/index.json#fragment"},
                        {"index_url": "https://[broken"}, {"index_url": "file:///index.json"}):
            with self.subTest(changes=changes):
                self.assertEqual(selection.association_key(dict(self.metadata, **changes)), "")
                self.assertFalse(selection.board_selected(dict(self.metadata, **changes), "one"))
        self.assertEqual(selection.association_key(None), "")

    def test_malformed_values_and_noncanonical_keys_fail_closed(self):
        for values in ([], {self.key: "one"}, {self.key: {"one": True}},
                       {json.dumps(json.loads(self.key)): ["one"]}, {"bad key": ["one"]}):
            with self.subTest(values=values):
                self.write(values)
                self.assertEqual(selection.load_preferences(force_read=True), {})

    def test_receipt_selection_identity_never_enables_without_local_choice(self):
        row = {"arduino_id": "one", "arduino_fqbn": "vendor:cpu:one",
               "arduino_cli_selection": selection.selection_identity(self.metadata),
               "backend": "arduino-cli", "status": "ready", "arduino_cli_selected": True}
        self.assertFalse(selection.selection_for_row(row))
        self.write()
        self.assertTrue(selection.selection_for_row(row))
        for changes in ({"arduino_fqbn": "vendor:cpu:two"}, {"arduino_id": "two"},
                        {"arduino_cli_selection": {}}, {"arduino_cli_selection": None}):
            with self.subTest(changes=changes):
                self.assertFalse(selection.selection_for_row(dict(row, **changes)))
        self.assertFalse(selection.selection_for_row(dict(row, arduino_fqbn=None, arduino_cli=[])))
        self.assertFalse(selection.selection_for_row(dict(row, arduino_fqbn=None, arduino_cli="bad")))

    def test_original_source_receipt_must_bind_the_same_selected_fqbn(self):
        self.write()
        row = {"arduino_id": "one", "arduino_source_proof": {
            "core": "vendor:cpu", "index_url": self.metadata["index_url"], "fqbn": "vendor:cpu:one"}}
        self.assertTrue(selection.selection_for_row(row))
        for key, value in (("core", "vendor:other"), ("fqbn", "vendor:cpu:two"),
                           ("index_url", "https://other.invalid/index.json")):
            changed = copy.deepcopy(row)
            changed["arduino_source_proof"][key] = value
            self.assertFalse(selection.selection_for_row(changed))

    def test_action_force_read_revokes_choice_despite_unchanged_file_stat(self):
        self.write({self.key: ["one"]})
        stat = self.settings.stat()
        original_stat = Path.stat
        def same_stamp(path, *args, **kwargs):
            return stat if path == self.settings else original_stat(path, *args, **kwargs)
        with patch.object(Path, "stat", same_stamp):
            self.assertTrue(selection.board_selected(self.metadata, "one"))
            self.write({self.key: ["two"]})
            preferences = selection.load_preferences(force_read=True)
            self.assertFalse(selection.board_selected(self.metadata, "one", preferences))
            self.assertTrue(selection.board_selected(self.metadata, "two", preferences))
            self.assertFalse(selection.board_selected(self.metadata, "one"))

    def test_catalog_fingerprint_ignores_unrelated_preferences_but_tracks_choices(self):
        self.write(theme="dark")
        first = selection.preferences_fingerprint()
        self.write(theme="light")
        selection.invalidate_preferences()
        self.assertEqual(selection.preferences_fingerprint(), first)
        self.write({self.key: ["two"]}, theme="light")
        selection.invalidate_preferences()
        self.assertNotEqual(selection.preferences_fingerprint(), first)

    def test_reader_does_not_modify_settings_or_create_package_resources(self):
        self.write()
        before = self.settings.read_bytes(), self.settings.stat().st_mtime_ns
        self.assertTrue(selection.board_selected(self.metadata, "one"))
        self.assertEqual((self.settings.read_bytes(), self.settings.stat().st_mtime_ns), before)
        self.assertEqual(list(self.root.iterdir()), [self.settings])


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
