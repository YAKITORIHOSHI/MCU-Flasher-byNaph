"""Isolated secure configuration migration and app cloud-operation gates."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.core.credential_store import SecureStorageError
from main.core.owner_tickets import OwnerTicketService
from main.core import owner_tickets
from main.web_bridge import MCUWebBackendAPI


class MemoryVault:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return copy.deepcopy(self.values.get(name))

    def set(self, name, value):
        self.values[name] = copy.deepcopy(value)


class ConfigurationChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp" / "audit" / "cloud-configuration"
        scratch.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.folder.cleanup)
        self.vault = MemoryVault()
        self.service = OwnerTicketService(self.folder.name, credential_store=self.vault)

    def legacy_config(self):
        value = {"firebase_api_key": "fixture-key", "firebase_database_url":
                 "https://fixture.firebasedatabase.app", "firebase_project_id": "fixture",
                 "owner_email": "fixture@example.com", "firebase_user_uid": "fixture-uid",
                 "firebase_root_collection": "users", "use_firebase": True}
        self.assertTrue(self.service._write_config(value))
        return value

    def test_legacy_settings_migrate_verified_values_then_remove_plaintext(self):
        old = self.legacy_config()
        cfg = self.service.get_config()
        self.assertEqual(cfg["firebase_api_key"], old["firebase_api_key"])
        disk = self.service._read_config()
        self.assertNotIn("firebase_api_key", disk)
        self.assertNotIn("owner_email", disk)
        self.assertEqual(disk["firebase_root_collection"], "users")
        self.assertEqual(self.vault.values["owner_metadata"]["firebase_user_uid"], "fixture-uid")

    def test_obsolete_local_hash_retired_after_verified_migration_or_alone(self):
        legacy = self.legacy_config()
        legacy["owner_password_hash"] = "obsolete-fixture-hash"
        self.assertTrue(self.service._write_config(legacy))
        self.service.get_config()
        self.assertNotIn("owner_password_hash", self.service._read_config())
        self.assertTrue(self.service._write_config({"owner_password_hash": "obsolete-fixture-hash", "use_firebase": False}))
        self.service.get_config()
        self.assertEqual(self.service._read_config(), {"use_firebase": False})

    def test_failed_migration_keeps_all_legacy_settings_for_recovery(self):
        legacy = self.legacy_config()
        legacy["owner_password_hash"] = "obsolete-fixture-hash"
        self.assertTrue(self.service._write_config(legacy))
        with patch.object(self.vault, "set", side_effect=SecureStorageError("locked")):
            self.service.get_config()
        self.assertEqual(self.service._read_config(), legacy)

    def test_failed_secure_migration_preserves_original_settings(self):
        old = self.legacy_config()
        with patch.object(self.vault, "set", side_effect=SecureStorageError("locked")):
            cfg = self.service.get_config()
            self.assertFalse(self.service.save_config({"use_firebase": False}))
        self.assertEqual(self.service._read_config(), old)
        self.assertEqual(cfg["firebase_api_key"], "")

    def test_local_settings_change_preserves_and_secures_legacy_provider(self):
        self.legacy_config()
        self.assertTrue(self.service.save_config({"use_firebase": False}))
        self.assertEqual(self.vault.values["provider_configuration"]["firebase_api_key"], "fixture-key")
        self.assertNotIn("firebase_api_key", self.service._read_config())

    def test_default_owner_password_never_unlocks_local_data(self):
        with patch("main.core.owner_tickets.cloud_network_error", return_value="offline"):
            ok, _ = self.service.authenticate("", "owner")
        self.assertFalse(ok)
        self.assertFalse(self.service.is_authenticated)
        self.assertFalse(self.service.get_config()["local_access_configured"])

    def test_local_key_uses_salted_hash_and_requires_signin_to_change(self):
        literal = " local fixture key "
        self.assertTrue(self.service.configure_local_access(literal)[0])
        cfg = self.service.get_config()
        self.assertTrue(cfg["local_access_configured"])
        self.assertNotIn(literal, self.service._config_file.read_text(encoding="utf-8"))
        self.assertFalse(self.service.configure_local_access("different fixture key")[0])
        self.assertTrue(self.service.authenticate("fixture", literal)[0])
        self.assertFalse(self.service.is_cloud_authenticated)
        self.assertTrue(self.service.configure_local_access("different fixture key")[0])
        self.assertNotEqual(cfg["local_access"]["salt"], self.service.get_config()["local_access"]["salt"])

    def test_cloud_source_operation_blocks_file_saves_and_hardware_actions(self):
        api = MCUWebBackendAPI.__new__(MCUWebBackendAPI)
        api.emit = Mock()
        api.is_busy = False
        api._cloud_project_operation = True
        self.assertTrue(api.is_busy)
        self.assertFalse(api.save_file(str(Path(self.folder.name) / "root.ino"), "content")["success"])
        self.assertFalse(api.save_all_files()["success"])
        api.active_operation = None
        api._current_op_phase = None
        self.assertFalse(api.open_project(self.folder.name)["success"])
        self.assertFalse(api.open_project_window(self.folder.name)["success"])
        self.assertFalse((Path(self.folder.name) / "root.ino").exists())
        api._cloud_project_operation = False
        self.assertFalse(api.is_busy)
        api.is_busy = True
        self.assertTrue(api.is_busy)

    def test_link_worker_start_failure_can_retry_without_losing_project(self):
        api = MCUWebBackendAPI.__new__(MCUWebBackendAPI)
        api._lock = threading.Lock()
        api.sketch_dir_path = Path(self.folder.name)
        api._cloud_project_link = None
        api.emit = Mock()
        with patch("main.web_bridge.threading.Thread") as worker:
            worker.return_value.start.side_effect = RuntimeError("fixture start failure")
            api.refresh_cloud_project_link()
        self.assertFalse(api._cloud_link_worker_running)
        self.assertIsNone(api._cloud_link_pending)
        self.assertEqual(api.sketch_dir_path, Path(self.folder.name))
        with patch("main.web_bridge.threading.Thread") as worker:
            api.refresh_cloud_project_link()
            worker.return_value.start.assert_called_once()

    def test_native_owner_storage_ignores_foreign_windows_environment(self):
        fake_home = Path(self.folder.name) / "fixture-home"
        with patch.object(owner_tickets.sys, "platform", "linux"), \
                patch.dict(owner_tickets.os.environ, {"LOCALAPPDATA": r"C:\Windows\foreign-user"}), \
                patch.object(owner_tickets.Path, "home", return_value=fake_home), \
                patch.object(owner_tickets, "hide_hidden_attribute"):
            storage = owner_tickets.get_owner_portal_storage_dir()
        self.assertEqual(storage, fake_home / ".mcu_flasher" / ".owner_portal")
        self.assertTrue(storage.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
