#!/usr/bin/env python3
"""Isolated notification persistence checks; never access the active database."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.dbs import dbs_create, dbs_delete, dbs_update


class NotificationWriteChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp/audit"
        scratch.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(dir=scratch, prefix="notification-writes-")
        self.addCleanup(fixture.cleanup)
        self.database = Path(fixture.name) / "notifications.json"
        self.original = [{"id": "existing", "category": "system", "message": "Retained history"}]
        self.database.write_text(json.dumps(self.original), encoding="utf-8")
        self.before = self.database.read_bytes()

    def test_failed_create_reports_no_persisted_record_and_preserves_history(self):
        with patch.object(dbs_create, "_safe_replace_file", return_value=False) as replace, \
             patch("builtins.print") as diagnostic:
            record = dbs_create.add_notification(message="Unsaved notification", db_path=self.database)
        self.assertIsNone(record)
        self.assertEqual(self.database.read_bytes(), self.before)
        replace.assert_called_once()
        self.assertIn("file replacement failed", diagnostic.call_args.args[0])

    def test_failed_update_does_not_claim_success_or_change_history(self):
        with patch.object(dbs_update, "_safe_replace_file", return_value=False) as replace, \
             patch("builtins.print") as diagnostic:
            updated = dbs_update.update_notification("existing", {"message": "Unsaved change"}, db_path=self.database)
        self.assertFalse(updated)
        self.assertEqual(self.database.read_bytes(), self.before)
        replace.assert_called_once()
        self.assertIn("file replacement failed", diagnostic.call_args.args[0])

    def test_successful_create_and_update_are_present_on_disk(self):
        record = dbs_create.add_notification(message="Stored notification", db_path=self.database)
        self.assertIsInstance(record, dict)
        created = json.loads(self.database.read_text(encoding="utf-8"))
        self.assertEqual(created, self.original + [record])
        self.assertTrue(dbs_update.update_notification(record["id"], {"message": "Stored update"}, db_path=self.database))
        updated = json.loads(self.database.read_text(encoding="utf-8"))
        self.assertEqual(updated[0], self.original[0])
        self.assertEqual(updated[-1]["message"], "Stored update")

    def test_same_millisecond_notifications_have_independent_ids_and_edits(self):
        same_time = datetime(2026, 10, 5, 12, 0, 0, 123000)
        with patch.object(dbs_create, "datetime") as clock, \
             patch.object(dbs_create.time, "time", return_value=1000000):
            clock.now.return_value = same_time
            first = dbs_create.add_notification(message="First notice", db_path=self.database)
            second = dbs_create.add_notification(message="Second notice", db_path=self.database)
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(first["id"].startswith("notif_1000000_123_"))
        self.assertTrue(second["id"].startswith("notif_1000000_123_"))
        self.assertEqual(first["timestamp"], second["timestamp"])
        self.assertTrue(dbs_update.update_notification(first["id"], {"message": "Changed first notice"}, db_path=self.database))
        records = {record["id"]: record for record in json.loads(self.database.read_text(encoding="utf-8"))}
        self.assertEqual(records[first["id"]]["message"], "Changed first notice")
        self.assertEqual(records[second["id"]]["message"], "Second notice")
        self.assertTrue(dbs_delete.delete_notification(first["id"], db_path=self.database))
        records = {record["id"]: record for record in json.loads(self.database.read_text(encoding="utf-8"))}
        self.assertNotIn(first["id"], records)
        self.assertEqual(records[second["id"]]["message"], "Second notice")

    def test_temp_write_failure_reports_failed_create_and_update(self):
        with patch("builtins.open", side_effect=PermissionError("fixture write denied")), \
             patch("builtins.print") as diagnostic:
            record = dbs_create.add_notification(message="Blocked notification", db_path=self.database)
        self.assertIsNone(record)
        self.assertIn("fixture write denied", diagnostic.call_args.args[0])
        self.assertEqual(self.database.read_bytes(), self.before)
        with patch.object(dbs_update, "_safe_replace_file", side_effect=OSError("fixture replace denied")), \
             patch("builtins.print") as diagnostic:
            result = dbs_update.update_notification("existing", {"message": "Blocked update"}, db_path=self.database)
        self.assertFalse(result)
        self.assertIn("fixture replace denied", diagnostic.call_args.args[0])
        self.assertEqual(self.database.read_bytes(), self.before)

    def test_failed_atomic_replace_never_falls_back_to_truncating_database(self):
        staging = self.database.with_name(self.database.name + ".tmp")
        staging.write_text("replacement", encoding="utf-8")
        with patch.object(dbs_create.os, "replace", side_effect=PermissionError("fixture locked")) as replace, \
             patch.object(dbs_create.time, "sleep"), patch("shutil.copy2") as copy:
            self.assertFalse(dbs_create._safe_replace_file(str(staging), str(self.database)))
        self.assertEqual(replace.call_count, 5)
        copy.assert_not_called()
        self.assertEqual(self.database.read_bytes(), self.before)

    @unittest.skipUnless(sys.platform == "win32", "Native Windows attributes")
    def test_repeated_hidden_system_database_updates_remain_writable_and_atomic(self):
        from main.core import file_utils
        self.assertTrue(file_utils._set_windows_file_attributes(self.database, 0x01 | 0x02 | 0x04))
        for number in range(3):
            record = dbs_create.add_notification(message=f"Stored notification {number}", db_path=self.database)
            self.assertIsInstance(record, dict)
            self.assertTrue(dbs_update.update_notification(record["id"], {"message": f"Updated {number}"}, db_path=self.database))
            self.assertEqual(self.database.stat().st_file_attributes & 0x07, 0x02 | 0x04)
            self.assertEqual(json.loads(self.database.read_text(encoding="utf-8"))[-1]["message"], f"Updated {number}")

        # A locked target keeps its original bytes and attributes; retrying
        # the same staging filename after the lock is released still works.
        before = self.database.read_bytes()
        with patch.object(dbs_create.os, "replace", side_effect=PermissionError("fixture locked")), \
             patch.object(dbs_create.time, "sleep"), patch("builtins.print"):
            self.assertIsNone(dbs_create.add_notification(message="Locked record", db_path=self.database))
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(self.database.stat().st_file_attributes & 0x07, 0x02 | 0x04)
        self.assertIsInstance(dbs_create.add_notification(message="After unlock", db_path=self.database), dict)
        self.assertEqual(self.database.stat().st_file_attributes & 0x07, 0x02 | 0x04)

        staging = self.database.with_name(self.database.name + ".tmp")
        staging.write_text("replacement", encoding="utf-8")
        self.assertTrue(file_utils._set_windows_file_attributes(staging, 0x01))
        before = self.database.read_bytes()
        with patch.object(dbs_create.os, "replace", side_effect=PermissionError("fixture locked")), \
             patch.object(dbs_create.time, "sleep"):
            self.assertFalse(dbs_create._safe_replace_file(str(staging), str(self.database)))
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse(staging.stat().st_file_attributes & 0x01)
        staging.write_text("staging file stays writable", encoding="utf-8")

    @unittest.skipUnless(sys.platform == "win32", "Native Windows attributes")
    def test_new_cache_database_is_hidden_without_readonly(self):
        target = self.database.parent / ".mcu_flasher_build_cache" / "dbs_notif.json"
        record = dbs_create.add_notification(message="Cache record", db_path=target)
        self.assertIsInstance(record, dict)
        self.assertEqual(target.stat().st_file_attributes & 0x03, 0x02)


if __name__ == "__main__":
    unittest.main(verbosity=2)
