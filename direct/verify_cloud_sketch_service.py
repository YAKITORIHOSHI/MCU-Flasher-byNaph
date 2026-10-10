#!/usr/bin/env python3
"""Isolated cloud REST/security checks; no real account, keyring, DB or hardware."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
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
from src.modules import offline_runtime as offline

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
        if request.get_method() == "HEAD":
            return None, {}
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
        self.addCleanup(shutil.rmtree, cloud._io_path(self.folder))
        self.store = MemoryStore()
        self.http = MemoryHTTP()
        self.service = self.make_service()
        self.local = self.folder / "local"
        self.local.mkdir()
        (self.local / "Sketch.ino").write_bytes(b"void setup() {}\nvoid loop() {}\n")
        (self.local / "Header.h").write_bytes(b"#pragma once\n")
        real_network = patch.object(cloud.urllib.request, "build_opener", side_effect=AssertionError("Real HTTP forbidden"))
        real_network.start()
        self.addCleanup(real_network.stop)

    def make_service(self):
        return cloud.CloudSketchService(config_provider=lambda: dict(CONFIG), credential_store=self.store,
                                        transport=self.http, data_dir=self.folder / "user-cloud",
                                        source_root_provider=lambda: self.folder / "_MCUFlasherByNaph_src")

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

    def test_cloud_auth_and_probe_work_independently_of_offline_mode(self):
        original = self.service._transport
        def offline_aware_transport(request, **kwargs):
            host = urllib.parse.urlsplit(request.full_url).hostname
            offline._audit("socket.getaddrinfo", (host, 443, 0, 0, 0))
            offline._audit("socket.connect", (None, ("8.8.8.8", 443)))
            return original(request, **kwargs)
        self.service._transport = offline_aware_transport
        for enabled in (False, True):
            with self.subTest(offline_mode=enabled), patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "1" if enabled else ""}):
                self.assertTrue(self.service.check_connection())
                self.login()
                self.assertTrue(self.service.is_authenticated)
                self.assertFalse(offline.network_access_disabled())
                offline._audit("socket.getaddrinfo", ("example.invalid", 443, 0, 0, 0))
                offline._audit("socket.connect", (None, ("8.8.8.8", 443)))
        self.assertGreaterEqual(len(self.http.calls), 4)

    def test_connection_probe_uses_firebase_head_without_credentials_or_database_content(self):
        self.assertTrue(self.service.check_connection())
        request = self.http.calls[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(urllib.parse.urlsplit(request.full_url).hostname, "fixture.firebasedatabase.app")
        self.assertIsNone(request.data)
        self.assertEqual(urllib.parse.urlsplit(request.full_url).query, "shallow=true")

    def test_provider_configuration_is_read_only_from_secure_vault(self):
        names = {"FIREBASE_API_KEY": "environment-key",
                 "FIREBASE_DATABASE_URL": "https://environment.firebasedatabase.app",
                 "FIREBASE_PROJECT_ID": "environment-project"}
        with patch.dict(os.environ, names, clear=False):
            self.assertFalse(any(cloud.load_cloud_configuration(self.store).values()))
            self.store.set("provider_configuration", CONFIG)
            self.assertEqual(cloud.load_cloud_configuration(self.store), CONFIG)

    def test_unconfigured_connection_probe_checks_firebase_auth_host(self):
        self.service._config_provider = lambda: {}
        self.assertTrue(self.service.check_connection())
        request = self.http.calls[-1]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(urllib.parse.urlsplit(request.full_url).hostname, "identitytoolkit.googleapis.com")
        self.assertIsNone(request.data)

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

    def test_locked_vault_cannot_reuse_a_prior_saved_session(self):
        scope = self.service._scope()
        self.store.values["session:" + scope] = {
            "uid": "old-user", "email": "old@example.invalid", "refresh_token": "old-refresh",
        }
        self.store.delete = Mock(side_effect=SecureStorageError("Fixture keyring is locked"))
        with self.assertRaisesRegex(cloud.CloudError, "locked"):
            self.login()
        with self.assertRaisesRegex(cloud.CloudError, "locked"):
            self.service.create_account("new@example.invalid", "fixture password")
        self.assertFalse(self.http.calls)
        self.assertFalse(self.service.is_authenticated)

    def test_missing_vault_blocks_ephemeral_login_if_this_provider_has_saved_credentials(self):
        self.login(save_login=True)
        marker = next((self.folder / "user-cloud").glob(".credential-store-*"))
        self.assertEqual(marker.read_bytes(), b"")
        marker_content = b"".join(path.read_bytes() for path in (self.folder / "user-cloud").glob(".credential-store-*"))
        self.assertNotIn(b"literal password", marker_content)
        self.assertNotIn(b"fixture-refresh-token", marker_content)
        self.store.status = False, "Fixture keyring is unavailable"
        child = self.make_service()
        self.http.calls.clear()
        with self.assertRaisesRegex(cloud.CloudError, "saved credentials"):
            child.sign_in("other@example.invalid", "other password")
        self.assertFalse(self.http.calls)

    def test_credential_marker_tracks_remaining_saved_vault_entries(self):
        scope = self.service._scope()
        marker = self.folder / "user-cloud" / (".credential-store-" + scope)
        self.login(remember_me=True)
        self.assertTrue(marker.is_file())
        self.service.sign_out()
        self.assertFalse(marker.exists())

        self.login(save_login=True, remember_me=True)
        self.service.sign_out()
        self.assertTrue(marker.is_file())
        self.service.sign_out(forget_saved=True)
        self.assertFalse(marker.exists())

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
        self.assertEqual(self.service.working_directory(meta["id"]),
                         self.folder / "_MCUFlasherByNaph_src" / "Cloud" / meta["name"])

    def test_readable_cloud_folder_reopens_saved_edits_without_a_pull(self):
        self.login()
        meta = self.service.upload_project(self.local, "My Arduino Sketch")
        working = self.service.pull_project(meta["id"])
        self.assertEqual(working, self.folder / "_MCUFlasherByNaph_src" / "Cloud" / "My Arduino Sketch")
        (working / "Sketch.ino").write_bytes(b"// saved unsynced edits\n")
        self.http.calls.clear()
        self.assertEqual(self.service.working_directory(meta["id"], "Renamed on cloud"), working)
        self.assertFalse(self.http.calls)
        self.assertEqual((working / "Sketch.ino").read_bytes(), b"// saved unsynced edits\n")
        self.assertFalse((self.folder / "user-cloud" / "projects").exists())

    def test_generated_source_location_uses_the_configured_download_root(self):
        from main.core import board_catalog
        configured = self.folder / "Configured source location"
        configured.mkdir()
        for name in ("Boards", "Libs"):
            (configured / name).mkdir()
            (configured / name / "keep.txt").write_bytes(b"owned existing content")
        self.login()
        meta = self.service.upload_project(self.local, "Sketch one")
        self.service._source_root_provider = None
        with patch.object(board_catalog, "_get_download_dir", return_value=str(configured)) as download_root:
            working = self.service.pull_project(meta["id"])
        download_root.assert_called_once_with()
        self.assertEqual(working, configured / "Cloud" / "Sketch one")
        self.assertEqual({entry.name for entry in configured.iterdir()}, {"Boards", "Libs", "Cloud"})
        for name in ("Boards", "Libs"):
            self.assertEqual((configured / name / "keep.txt").read_bytes(), b"owned existing content")

    def test_safe_names_case_collisions_and_different_accounts_never_share_sources(self):
        self.login()
        first = self.service.upload_project(self.local, "../CON:<bad>\\Sketch")
        one = self.service.pull_project(first["id"])
        self.assertEqual(one.parent, self.folder / "_MCUFlasherByNaph_src" / "Cloud")
        self.assertNotIn(first["id"], one.name)
        self.assertNotIn("..", one.name)
        two_meta = self.service.upload_project(self.local, first["name"].upper())
        two = self.service.pull_project(two_meta["id"])
        self.assertNotEqual(one.name.casefold(), two.name.casefold())
        self.http.uid = "other-account"
        self.login()
        third = self.service.upload_project(self.local, first["name"])
        three = self.service.pull_project(third["id"])
        self.assertNotEqual(one, three)
        self.assertNotEqual(two, three)
        self.assertEqual(cloud.read_project_link(one)["uid"], "fixture-user")
        self.assertEqual(cloud.read_project_link(three)["uid"], "other-account")
        (three / "Sketch.ino").write_bytes(b"// another account\n")
        self.assertEqual((one / "Sketch.ino").read_bytes(), b"void setup() {}\nvoid loop() {}\n")

    def test_existing_legacy_saved_sources_are_copied_and_journals_left_untouched(self):
        self.login()
        meta = self.service.upload_project(self.local, "Legacy sketch")
        legacy = (self.folder / "user-cloud" / "projects" / self.service._scope()[:16]
                  / self.service._account_folder() / meta["id"])
        self.service.pull_project(meta["id"], legacy)
        (legacy / "Sketch.ino").write_bytes(b"// saved legacy edit\n")
        (legacy / "Header.h").unlink()
        (legacy / "New.cpp").write_bytes(b"// newly added source\n")
        journal = legacy / ".mcu_flasher_build_cache" / ".mcu_ai_edits"
        journal.mkdir(parents=True)
        (journal / "edit1.txt").write_bytes(b"protected journal")
        (legacy / "asset.png").write_bytes(b"user asset")
        self.http.calls.clear()
        working = self.service.working_directory(meta["id"], meta["name"])
        self.assertEqual(working, self.folder / "_MCUFlasherByNaph_src" / "Cloud" / "Legacy sketch")
        self.assertEqual((working / "Sketch.ino").read_bytes(), b"// saved legacy edit\n")
        self.assertEqual((working / "New.cpp").read_bytes(), b"// newly added source\n")
        self.assertFalse((working / "Header.h").exists())
        self.assertFalse((working / ".mcu_flasher_build_cache").exists())
        self.assertEqual((journal / "edit1.txt").read_bytes(), b"protected journal")
        self.assertEqual((legacy / "asset.png").read_bytes(), b"user asset")
        self.assertEqual((legacy / "Sketch.ino").read_bytes(), b"// saved legacy edit\n")
        self.assertFalse(self.http.calls)

    def test_relocated_generated_root_preserves_existing_saved_sources(self):
        meta = self.upload()
        first = self.local
        (first / "Sketch.ino").write_bytes(b"// unpushed relocation edit\n")
        self.service._source_root_provider = lambda: self.folder / "Relocated sources"
        moved = self.service.working_directory(meta["id"], meta["name"])
        self.assertEqual(moved, self.folder / "Relocated sources" / "Cloud" / meta["name"])
        self.assertEqual((moved / "Sketch.ino").read_bytes(), b"// unpushed relocation edit\n")
        self.assertEqual((first / "Sketch.ino").read_bytes(), b"// unpushed relocation edit\n")
        reopened = self.make_service()
        reopened._source_root_provider = lambda: self.folder / "Relocated sources"
        reopened.sign_in("user@example.com", "literal password")
        self.assertEqual(reopened.working_directory(meta["id"], meta["name"]), moved)

    def test_source_added_during_legacy_copy_is_retained_and_retry_uses_clean_destination(self):
        self.login()
        meta = self.service.upload_project(self.local, "Legacy retry")
        legacy = (self.folder / "user-cloud" / "projects" / self.service._scope()[:16]
                  / self.service._account_folder() / meta["id"])
        self.service.pull_project(meta["id"], legacy)
        sync = cloud.os.fsync
        changed = False
        def add_source_on_copy(descriptor):
            nonlocal changed
            sync(descriptor)
            if not changed:
                changed = True
                (legacy / "Added.cpp").write_bytes(b"// saved while copying\n")
        with patch.object(cloud.os, "fsync", side_effect=add_source_on_copy):
            with self.assertRaisesRegex(cloud.CloudError, "original folder and saved edits were preserved"):
                self.service.working_directory(meta["id"], meta["name"])
        cloud_root = self.folder / "_MCUFlasherByNaph_src" / "Cloud"
        self.assertFalse(any(cloud_root.iterdir()))
        self.assertEqual((legacy / "Added.cpp").read_bytes(), b"// saved while copying\n")
        working = self.service.working_directory(meta["id"], meta["name"])
        self.assertEqual(working.name, "Legacy retry")
        self.assertEqual((working / "Added.cpp").read_bytes(), b"// saved while copying\n")

    def test_external_edit_to_new_copy_is_never_removed_during_failed_copy_cleanup(self):
        self.login()
        meta = self.service.upload_project(self.local, "Copy race")
        legacy = (self.folder / "user-cloud" / "projects" / self.service._scope()[:16]
                  / self.service._account_folder() / meta["id"])
        self.service.pull_project(meta["id"], legacy)
        destination = self.folder / "_MCUFlasherByNaph_src" / "Cloud" / "Copy race"
        sync = cloud.os.fsync
        def change_destination(descriptor):
            sync(descriptor)
            path = destination / "Sketch.ino"
            if path.exists():
                path.write_bytes(b"// external destination edit\n")
        with patch.object(cloud.os, "fsync", side_effect=change_destination):
            with self.assertRaisesRegex(cloud.CloudError, "original folder and saved edits were preserved"):
                self.service.working_directory(meta["id"], meta["name"])
        self.assertEqual((destination / "Sketch.ino").read_bytes(), b"// external destination edit\n")
        self.assertEqual((legacy / "Sketch.ino").read_bytes(), b"void setup() {}\nvoid loop() {}\n")
        self.assertFalse((destination / cloud.LINK_NAME).exists())
        retry = self.service.working_directory(meta["id"], meta["name"])
        self.assertEqual(retry.name, "Copy race (2)")
        self.assertEqual((destination / "Sketch.ino").read_bytes(), b"// external destination edit\n")

    def test_unknown_folder_and_empty_reservation_never_overwrite_user_material(self):
        self.login()
        meta = self.service.upload_project(self.local, "Occupied sketch")
        occupied = self.folder / "_MCUFlasherByNaph_src" / "Cloud" / meta["name"]
        occupied.mkdir(parents=True)
        (occupied / "Sketch.ino").write_bytes(b"unrelated user project")
        working = self.service.working_directory(meta["id"], meta["name"])
        self.assertEqual(working.name, "Occupied sketch (2)")
        self.assertEqual(self.service.working_directory(meta["id"], meta["name"]), working)
        self.service.pull_project(meta["id"])
        self.assertEqual((occupied / "Sketch.ino").read_bytes(), b"unrelated user project")

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
            if Path(dst).parent == cloud._io_path(folder) and Path(dst).suffix == ".h":
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
            if Path(dst).parent == cloud._io_path(folder) and Path(dst).suffix in (".ino", ".h"):
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

    def test_deep_windows_paths_preserve_source_and_recovery_operations(self):
        deep = self.folder / ("a" * 70) / ("b" * 70) / ("c" * 70)
        cloud._io_path(deep).mkdir(parents=True)
        filename = "Long-" + "n" * 100 + ".ino"
        cloud._io_path(deep / filename).write_bytes(b"void setup() {}\nvoid loop() {}\n")
        self.login()
        meta = self.service.upload_project(deep)
        self.service._data_dir = deep / "cloud-working"
        self.service._source_root_provider = lambda: deep / "_MCUFlasherByNaph_src"
        working = self.service.pull_project(meta["id"])
        self.assertEqual(cloud._io_path(working / filename).read_bytes(), b"void setup() {}\nvoid loop() {}\n")
        cloud._io_path(working / filename).write_bytes(b"// changed\n")
        self.service.push_project(working, meta["id"], 1)
        self.service.pull_project(meta["id"], working, revision=1)
        self.assertEqual(cloud._io_path(working / filename).read_bytes(), b"void setup() {}\nvoid loop() {}\n")
        self.assertEqual(cloud.read_project_link(working)["revision"], 2)
        with patch.object(cloud.sys, "platform", "linux"):
            self.assertEqual(cloud._io_path(deep), deep)


if __name__ == "__main__":
    unittest.main(verbosity=2)
