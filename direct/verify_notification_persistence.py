#!/usr/bin/env python3
"""Verify notification deletion persistence using explicit, isolated databases."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dbs import dbs_delete


class NotificationPersistenceChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/notification-persistence"
        audit.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "notifications.json"
        self.records = [
            {"id": "one", "category": "build", "message": "Compiled ✓"},
            {"id": "two", "category": "device", "message": "Disconnected"},
            {"id": "three", "category": "build", "message": "Missing header"},
        ]
        self.write_records()
        guard = patch("src.dbs.dbs_create.get_default_db_path",
                      side_effect=AssertionError("Verification must not use the live notification database"))
        guard.start()
        self.addCleanup(guard.stop)

    def write_records(self):
        self.database.write_text(json.dumps(self.records, ensure_ascii=False), encoding="utf-8")

    def operations(self):
        return (
            (lambda: dbs_delete.clear_all_notifications(self.database), False),
            (lambda: dbs_delete.delete_notification("one", self.database), False),
            (lambda: dbs_delete.delete_notifications_by_category("build", self.database), 0),
        )

    def test_failed_replacement_preserves_database_and_reports_failure(self):
        for operation, expected in self.operations():
            with self.subTest(operation=operation):
                self.write_records()
                original = self.database.read_bytes()
                with patch.object(dbs_delete, "_safe_replace_file", return_value=False) as replace:
                    self.assertEqual(operation(), expected)
                replace.assert_called_once_with(str(self.database) + ".tmp", str(self.database))
                self.assertEqual(self.database.read_bytes(), original)

    def test_replacement_exception_preserves_database_and_reports_failure(self):
        for operation, expected in self.operations():
            with self.subTest(operation=operation):
                self.write_records()
                original = self.database.read_bytes()
                with patch.object(dbs_delete, "_safe_replace_file", side_effect=OSError("Fixture replacement denied")), \
                        patch("builtins.print"):
                    self.assertEqual(operation(), expected)
                self.assertEqual(self.database.read_bytes(), original)

    def test_clear_success_persists_empty_database(self):
        self.assertTrue(dbs_delete.clear_all_notifications(self.database))
        self.assertEqual(json.loads(self.database.read_text(encoding="utf-8")), [])
        self.assertFalse(Path(str(self.database) + ".tmp").exists())

    def test_single_delete_success_preserves_other_records(self):
        self.assertTrue(dbs_delete.delete_notification("two", self.database))
        self.assertEqual(json.loads(self.database.read_text(encoding="utf-8")),
                         [self.records[0], self.records[2]])

    def test_category_delete_success_reports_persisted_count(self):
        self.assertEqual(dbs_delete.delete_notifications_by_category("build", self.database), 2)
        self.assertEqual(json.loads(self.database.read_text(encoding="utf-8")), [self.records[1]])

    def test_missing_records_do_not_attempt_replacement(self):
        original = self.database.read_bytes()
        with patch.object(dbs_delete, "_safe_replace_file") as replace:
            self.assertFalse(dbs_delete.delete_notification("missing", self.database))
            self.assertEqual(dbs_delete.delete_notifications_by_category("missing", self.database), 0)
        replace.assert_not_called()
        self.assertEqual(self.database.read_bytes(), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
