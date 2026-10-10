#!/usr/bin/env python3
"""Isolated cloud REST/security checks; no real account, keyring, DB or hardware."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.core import cloud_sketch_service as cloud
from main.core.credential_store import CredentialStore, SecureStorageError

CONFIG = {"firebase_api_key": "fixture-key", "firebase_database_url": "https://fixture.firebasedatabase.app",
          "firebase_project_id": "fixture-project"}


class MemoryStore:
    status = True, "Fixture secure store"

    def __init__(self):
        self.values = {}

    def get(self, key):
        return copy.deepcopy(self.values.get(key))

    def set(self, key, value):
        self.values[key] = copy.deepcopy(value)

    def delete(self, key):
        self.values.pop(key, None)


class MemoryHTTP:
    def __init__(self):
        self.data = {}
        self.calls = []
        self.uid = "fixture-user"
        self.force_conflict = False

    def response(self):
        return {"localId": self.uid, "idToken": "fixture-id-token", "refreshToken": "fixture-refresh-token",
                "expiresIn": "3600", "email": "user@example.com"}

    def fail(self, request, code=400, message="INVALID_LOGIN_CREDENTIALS"):
        raise urllib.error.HTTPError(request.full_url, code, "Fixture", {},
                                     io.BytesIO(json.dumps({"error": {"message": message}}).encode()))

    def __call__(self, request, *, timeout, max_bytes):
        self.calls.append(request)
        assert 0 < timeout <= 15 and max_bytes <= cloud.MAX_RESPONSE_BYTES
        url = urllib.parse.urlsplit(request.full_url)
        if url.hostname == "identitytoolkit.googleapis.com":
            return ({} if url.path.endswith(":delete") else self.response()), {}
        if url.hostname == "securetoken.googleapis.com":
            return {"user_id": self.uid, "id_token": "fixture-refreshed-id-token",
                    "refresh_token": "fixture-new-refresh", "expires_in": "3600"}, {}
        assert url.hostname == "fixture.firebasedatabase.app"
        assert urllib.parse.parse_qs(url.query)["auth"][0].startswith("fixture-")
        names = url.path.removesuffix(".json").strip("/").split("/")
        assert names[:2] == ["users", self.uid]
        parent = self.data
        for name in names[:-1]:
            parent = parent.setdefault(name, {})
        key = names[-1]
        old = parent.get(key)
        etag = ('"' + hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest() + '"') if old else "null_etag"
        method = request.get_method()
        if method == "GET":
            result = ({name: True for name in old} if isinstance(old, dict) else old) if "shallow=true" in url.query else old
            return copy.deepcopy(result), {"ETag": etag}
        if method == "PUT":
            if self.force_conflict or request.get_header("If-match") != etag:
                self.fail(request, 412, "Fixture conflict")
            parent[key] = json.loads(request.data)
            return copy.deepcopy(parent[key]), {}
        if method == "DELETE":
            parent.pop(key, None)
            return None, {}
        raise AssertionError(method)


class CloudChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp" / "audit"
        audit.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix="cloud-service-", dir=audit))
        self.addCleanup(shutil.rmtree, self.folder)
        self.store = MemoryStore()
        self.http = MemoryHTTP()
        self.service = self.make_service()
        self.local = self.folder / "local"
        self.local.mkdir()
        (self.local / "Sketch.ino").write_bytes(b"void setup() {}\nvoid loop() {}\n")
        (self.local / "Header.h").write_bytes(b"#pragma once\n")
        guard = patch.object(cloud, "network_access_disabled", return_value=False)
        self.offline = guard.start()
        self.addCleanup(guard.stop)
        real_network = patch.object(cloud.urllib.request, "build_opener", side_effect=AssertionError("Real HTTP forbidden"))
        real_network.start()
        self.addCleanup(real_network.stop)

    def make_service(self):
        return cloud.CloudSketchService(config_provider=lambda: dict(CONFIG), credential_store=self.store,
                                        transport=self.http, data_dir=self.folder / "user-cloud")

    def login(self, **options):
        return self.service.sign_in(" user@example.com ", " literal password ", **options)

    def upload(self):
        self.login()
        original = self.local
        result = self.service.upload_project(original)
        self.assertFalse((original / cloud.LINK_NAME).exists())
        self.local = self.service.pull_project(result["id"])
        return result

    def test_temporary_login_preserves_literal_password_and_saves_nothing(self):
        result = self.login()
        self.assertEqual(result["uid"], "fixture-user")
        payload = json.loads(self.http.calls[0].data)
        self.assertEqual(payload["password"], " literal password ")
        self.assertEqual(payload["email"], "user@example.com")
        self.assertFalse(self.store.values)

    def test_save_login_and_remember_session_are_distinct(self):
        self.login(save_login=True)
        self.assertEqual(self.service.saved_login()["password"], " literal password ")
        self.assertFalse(any(key.startswith("session:") for key in self.store.values))
        self.login(remember_me=True)
        self.assertFalse(any(key.startswith("login:") for key in self.store.values))
        session = next(iter(self.store.values.values()))
        self.assertIn("refresh_token", session)
        self.assertNotIn("password", session)
        child = self.make_service()
        self.assertTrue(child.restore_session())
        self.assertEqual(child.account_info["uid"], "fixture-user")
        self.assertIn("grant_type=refresh_token", self.http.calls[-1].data.decode())
        child.sign_out(forget_saved=True)
        self.assertFalse(self.store.values)

    def test_offline_never_sends_credentials(self):
        self.offline.return_value = True
        with self.assertRaisesRegex(cloud.CloudError, "Offline Mode"):
            self.login()
        self.assertFalse(self.http.calls)
        self.assertFalse(self.service.is_authenticated)

    def test_saved_credentials_fail_closed(self):
        self.store.status = False, "Fixture keyring is locked"
        with self.assertRaisesRegex(cloud.CloudError, "locked"):
            self.login(save_login=True)
        self.assertFalse(self.http.calls)
        self.store.status = True, "Fixture"
        self.store.set = Mock(side_effect=SecureStorageError("Fixture secure write failed"))
        with self.assertRaisesRegex(cloud.CloudError, "secure write failed"):
            self.login(remember_me=True)
        self.assertFalse(self.service.is_authenticated)

    def test_error_never_exposes_credential_url_or_arbitrary_remote_message(self):
        original = self.http.__call__
        def malicious(request, **kwargs):
            self.http.fail(request, 500, "password=actual-secret auth=actual-secret")
        self.service._transport = malicious
        with self.assertRaises(cloud.CloudError) as caught:
            self.login()
        self.assertNotIn("actual-secret", str(caught.exception))
        self.assertNotIn("fixture-key", str(caught.exception))
        self.assertNotIn("https://", str(caught.exception))

    def test_token_refresh_and_uid_mismatch(self):
        self.login(remember_me=True)
        self.service._expires_at = 0
        self.assertEqual(self.service.list_sketches(), [])
        self.assertIn("fixture-refreshed-id-token", self.http.calls[-1].full_url)
        self.service._expires_at = 0
        self.http.uid = "other-account"
        with self.assertRaisesRegex(cloud.CloudError, "different account"):
            self.service.list_sketches()
        self.assertFalse(self.service.is_authenticated)

    def test_upload_excludes_all_cache_and_hidden_material(self):
        cache = self.local / ".mcu_flasher_build_cache"
        cache.mkdir()
        (cache / "secret.txt").write_text("fixture secret", encoding="utf-8")
        (self.local / ".private.txt").write_text("fixture hidden", encoding="utf-8")
        (self.local / "asset.png").write_bytes(b"image")
        meta = self.upload()
        self.assertEqual(meta["file_count"], 2)
        record = self.http.data["users"]["fixture-user"]["cloud_sketches"][meta["id"]]
        self.assertEqual({entry["name"] for entry in record["versions"]["r1"]["files"].values()}, {"Sketch.ino", "Header.h"})
        link = cloud.read_project_link(self.local)
        self.assertEqual(link["revision"], 1)
        self.assertEqual(link["name"], meta["name"])
        self.assertNotIn("token", json.dumps(link))
        self.assertEqual(self.service.list_sketches()[0]["id"], meta["id"])

    def test_push_conditional_guard_and_history_revert(self):
        meta = self.upload()
        (self.local / "Sketch.ino").write_bytes(b"// revision two\n")
        meta = self.service.push_project(self.local, meta["id"], 1)
        self.assertEqual(meta["revision"], 2)
        self.assertEqual([row["revision"] for row in self.service.list_revisions(meta["id"])], [2, 1])
        folder = self.service.pull_project(meta["id"], self.folder / "revert-copy", revision=1)
        self.assertEqual((folder / "Sketch.ino").read_bytes(), b"void setup() {}\nvoid loop() {}\n")
        self.assertEqual(cloud.read_project_link(folder)["revision"], 2)
        self.assertEqual(cloud.read_project_link(folder)["source_revision"], 1)
        self.assertEqual(self.service.push_project(folder, meta["id"], 2)["revision"], 3)
        self.assertNotEqual(folder, self.local)
        self.assertIn(str(self.folder / "user-cloud"), str(self.service.working_directory(meta["id"])))

    def test_stale_base_or_racing_etag_never_overwrites(self):
        meta = self.upload()
        folder = self.service.pull_project(meta["id"], self.folder / "stale-copy")
        self.service.push_project(self.local, meta["id"], 1)
        before = copy.deepcopy(self.http.data)
        with self.assertRaises(cloud.CloudConflict):
            self.service.push_project(folder, meta["id"], 1)
        self.assertEqual(self.http.data, before)
        self.http.force_conflict = True
        with self.assertRaises(cloud.CloudConflict):
            self.service.push_project(self.local, meta["id"], 2)
        self.assertEqual(self.http.data, before)
        self.assertEqual(cloud.read_project_link(self.local)["revision"], 2)

    def test_pull_preserves_unknown_files_and_protected_journals(self):
        meta = self.upload()
        folder = self.service.pull_project(meta["id"], self.folder / "protected-copy")
        cache = folder / ".mcu_flasher_build_cache" / ".mcu_ai_edits"
        cache.mkdir(parents=True)
        (cache / "edit1.txt").write_bytes(b"protected")
        (folder / "untracked.txt").write_bytes(b"keep")
        (self.local / "Header.h").unlink()
        self.service.push_project(self.local, meta["id"], 1)
        self.service.pull_project(meta["id"], folder)
        self.assertFalse((folder / "Header.h").exists())
        self.assertEqual((folder / "untracked.txt").read_bytes(), b"keep")
        self.assertEqual((cache / "edit1.txt").read_bytes(), b"protected")
        backups = list((self.folder / "user-cloud" / "recovery").rglob("Header.h"))
        self.assertTrue(backups)

    def test_pull_refuses_untracked_overwrite_and_bad_digest_or_traversal(self):
        meta = self.upload()
        destination = self.folder / "occupied"
        destination.mkdir()
        (destination / "Sketch.ino").write_bytes(b"untouched")
        with self.assertRaisesRegex(cloud.CloudError, "unlinked"):
            self.service.pull_project(meta["id"], destination)
        self.assertEqual((destination / "Sketch.ino").read_bytes(), b"untouched")
        files = self.http.data["users"]["fixture-user"]["cloud_sketches"][meta["id"]]["versions"]["r1"]["files"]
        entry = next(iter(files.values()))
        original = entry.copy()
        entry["sha256"] = "0" * 64
        with self.assertRaisesRegex(cloud.CloudError, "verification failed"):
            self.service.pull_project(meta["id"])
        entry.update(original)
        entry["name"] = "../escape.ino"
        with self.assertRaises(cloud.CloudError):
            self.service.pull_project(meta["id"])
        self.assertFalse((self.folder / "escape.ino").exists())

    def test_source_guard_covers_new_and_removed_files_and_blocks_pending_ai(self):
        meta = self.upload()
        folder = self.service.pull_project(meta["id"], self.folder / "guarded-copy")
        (self.local / "Header.h").unlink()
        (self.local / "New.h").write_bytes(b"#pragma once\n")
        self.service.push_project(self.local, meta["id"], 1)
        seen = []
        @contextlib.contextmanager
        def guard(paths):
            seen.extend(path.name for path in paths)
            raise ValueError("Fixture pending AI review")
            yield
        with self.assertRaisesRegex(cloud.CloudError, "pending AI"):
            self.service.pull_project(meta["id"], folder, source_guard=guard)
        self.assertEqual(set(seen), {"Sketch.ino", "Header.h", "New.h"})
        self.assertTrue((folder / "Header.h").exists())
        self.assertFalse((folder / "New.h").exists())

    def test_partial_pull_storage_failure_restores_sources(self):
        meta = self.upload()
        folder = self.service.pull_project(meta["id"], self.folder / "rollback-copy")
        (self.local / "Sketch.ino").write_bytes(b"new source")
        (self.local / "Header.h").write_bytes(b"new header")
        self.service.push_project(self.local, meta["id"], 1)
        old = {name: (folder / name).read_bytes() for name in ("Sketch.ino", "Header.h")}
        replace = cloud.os.replace
        calls = 0
        def failing_replace(src, dst):
            nonlocal calls
            if Path(dst).parent == folder and Path(dst).suffix == ".h":
                calls += 1
                if calls == 1:
                    raise OSError("Fixture storage failure")
            return replace(src, dst)
        with patch.object(cloud.os, "replace", side_effect=failing_replace):
            with self.assertRaisesRegex(cloud.CloudError, "Previous linked files were restored"):
                self.service.pull_project(meta["id"], folder)
        self.assertEqual({name: (folder / name).read_bytes() for name in old}, old)
        self.assertEqual(cloud.read_project_link(folder)["revision"], 1)

    def test_account_deletion_reauthenticates_before_database_then_identity(self):
        meta = self.upload()
        self.http.calls.clear()
        self.service.delete_account(" current literal password ")
        self.assertEqual(json.loads(self.http.calls[0].data)["password"], " current literal password ")
        self.assertEqual(self.http.calls[1].get_method(), "DELETE")
        self.assertTrue(self.http.calls[2].full_url.split("?")[0].endswith(":delete"))
        self.assertFalse(self.service.is_authenticated)
        self.assertTrue((self.local / "Sketch.ino").exists())

    def test_cross_platform_filename_and_configuration_validation(self):
        for filename in ("../bad.ino", "CON.h", "A?.ino", "a.h.", "sub\\a.h", ".private.txt"):
            with self.subTest(filename=filename), self.assertRaises(cloud.CloudError):
                cloud._source_name(filename)
        self.assertEqual(cloud._source_name("測定.Header.hpp"), "測定.Header.hpp")
        for url in ("http://fixture.firebasedatabase.app", "https://example.com", "https://user:pw@fixture.firebasedatabase.app"):
            self.service._config_provider = lambda url=url: {**CONFIG, "firebase_database_url": url}
            self.assertFalse(self.service.configured)

    def test_missing_linux_keyring_has_no_file_fallback(self):
        store = CredentialStore(platform="linux")
        with patch("main.core.credential_store.shutil.which", return_value=None):
            self.assertFalse(store.status[0])
            with self.assertRaises(SecureStorageError):
                store.set("fixture", {"password": "fixture-secret"})
            with self.assertRaises(SecureStorageError):
                store.get("fixture")

    def test_external_edit_during_pull_staging_is_preserved(self):
        meta = self.upload()
        before_link = (self.local / cloud.LINK_NAME).read_bytes()
        @contextlib.contextmanager
        def external_edit(_paths):
            (self.local / "Sketch.ino").write_bytes(b"external edit during pull")
            yield
        with self.assertRaisesRegex(cloud.CloudError, "changed during pull"):
            self.service.pull_project(meta["id"], self.local, source_guard=external_edit)
        self.assertEqual((self.local / "Sketch.ino").read_bytes(), b"external edit during pull")
        self.assertEqual((self.local / cloud.LINK_NAME).read_bytes(), before_link)

    def test_oldest_history_is_retired_after_twenty_revisions(self):
        meta = self.upload()
        for base in range(1, 22):
            (self.local / "Sketch.ino").write_bytes(f"// revision {base + 1}\n".encode())
            self.service.push_project(self.local, meta["id"], base)
        history = self.service.list_revisions(meta["id"])
        self.assertEqual(len(history), 20)
        self.assertEqual(history[0]["revision"], 22)
        self.assertEqual(history[-1]["revision"], 3)
        with self.assertRaisesRegex(cloud.CloudError, "no longer available"):
            self.service.pull_project(meta["id"], revision=1)

    def test_revision_boolean_is_not_a_valid_integer(self):
        meta = self.upload()
        with self.assertRaises(cloud.CloudConflict):
            self.service.push_project(self.local, meta["id"], True)
        with self.assertRaises(cloud.CloudError):
            self.service.pull_project(meta["id"], revision=True)
        link_path = self.local / cloud.LINK_NAME
        link = json.loads(link_path.read_text(encoding="utf-8"))
        link["revision"] = True
        link_path.write_text(json.dumps(link), encoding="utf-8")
        self.assertIsNone(cloud.read_project_link(self.local))

    def test_other_window_provider_change_never_sends_old_account_token(self):
        self.login(remember_me=True, save_login=True)
        old_scope = self.service._scope()
        self.service._config_provider = lambda: {**CONFIG, "firebase_api_key": "another-provider-key"}
        self.http.calls.clear()
        with self.assertRaisesRegex(cloud.CloudError, "provider changed"):
            self.service.list_sketches()
        self.assertFalse(self.http.calls)
        self.service.sign_out(forget_saved=True)
        self.assertNotIn("session:" + old_scope, self.store.values)
        self.assertNotIn("login:" + old_scope, self.store.values)

    def test_unconfigured_service_saved_login_is_empty(self):
        self.service._config_provider = lambda: {}
        self.assertFalse(self.service.configured)
        self.assertEqual(self.service.saved_login(), {})

    def test_failed_rollback_requires_editor_reload_and_keeps_recovery(self):
        meta = self.upload()
        folder = self.service.pull_project(meta["id"], self.folder / "incomplete-copy")
        (self.local / "Sketch.ino").write_bytes(b"new source")
        (self.local / "Header.h").write_bytes(b"new header")
        self.service.push_project(self.local, meta["id"], 1)
        replace = cloud.os.replace
        source_calls = 0
        def failed_rollback(src, dst):
            nonlocal source_calls
            if Path(dst).parent == folder and Path(dst).suffix in (".ino", ".h"):
                source_calls += 1
                if source_calls >= 2:
                    raise OSError("Fixture rollback failure")
            return replace(src, dst)
        with patch.object(cloud.os, "replace", side_effect=failed_rollback):
            with self.assertRaises(cloud.CloudRecoveryError) as caught:
                self.service.pull_project(meta["id"], folder)
        self.assertTrue(caught.exception.local_sources_changed)
        self.assertEqual(caught.exception.project_root, folder)
        self.assertTrue((caught.exception.recovery_dir / "Sketch.ino").exists())

    def test_notes_only_or_empty_primary_cannot_be_uploaded(self):
        self.login()
        (self.local / "Sketch.ino").unlink()
        (self.local / "NOTE.txt").write_bytes(b"notes only")
        with self.assertRaisesRegex(cloud.CloudError, "nonempty root"):
            self.service.upload_project(self.local)
        (self.local / "Sketch.ino").write_bytes(b"\n \t")
        with self.assertRaisesRegex(cloud.CloudError, "nonempty root"):
            self.service.upload_project(self.local)


if __name__ == "__main__":
    unittest.main(verbosity=2)
