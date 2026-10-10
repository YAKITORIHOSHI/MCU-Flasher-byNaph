#!/usr/bin/env python3
"""Isolated owner authentication checks; no live vault, storage, or network."""
from __future__ import annotations

import hashlib
import io
import json
import socket
import sys
import types
import unittest
import urllib.error
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.core import owner_tickets as owner
from src.modules import offline_runtime
REAL_OPEN_FIREBASE_REQUEST = owner._open_firebase_request


class Response:
    """A complete HTTP response supplied by the fixture, never a real socket."""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit=None):
        data = json.dumps(self.payload).encode("utf-8")
        return data if limit is None else data[:limit]


class OwnerTicketServiceChecks(unittest.TestCase):
    def setUp(self):
        # Bypass the constructor entirely: it normally creates the private
        # owner directory and loads repository/local configuration.
        self.service = owner.OwnerTicketService.__new__(owner.OwnerTicketService)
        self.service._id_token = None
        self.service._refresh_token = None
        self.service._uid = None
        self.service._user_email = None
        self.service._is_authenticated = False
        self.service._active_root_collection = None
        self.config = {
            "firebase_api_key": "fixture-web-api-key",
            "firebase_database_url": "https://fixture.firebasedatabase.app/",
            "firebase_project_id": "fixture-project",
            "firebase_user_uid": "configured-fixture-uid",
            "firebase_root_collection": "owner",
            "owner_email": "developer@example.com",
            "local_access": {
                "algorithm": "pbkdf2-sha256", "iterations": 100_000,
                "salt": b"fixture-onlysalt".hex(),
                "hash": hashlib.pbkdf2_hmac("sha256", b"local-fixture-key", b"fixture-onlysalt", 100_000).hex(),
            },
            "use_firebase": True,
        }
        self.service.get_config = Mock(side_effect=lambda: dict(self.config))
        self.service._read_local_tickets = Mock(return_value=[])
        self.service._save_local_tickets = Mock(
            side_effect=AssertionError("Live persistence is forbidden in this fixture")
        )
        self.service.save_config = Mock(
            side_effect=AssertionError("Live configuration writes are forbidden in this fixture")
        )
        self.patch(owner, "load_cloud_configuration", side_effect=AssertionError("Live vault reads are forbidden"))
        self.patch(owner, "CredentialStore", side_effect=AssertionError("Live vault access is forbidden"))
        self.disabled = self.patch(offline_runtime, "network_access_disabled", return_value=False)
        self.http = self.patch(
            owner, "_open_firebase_request",
            side_effect=AssertionError("HTTP responses must be explicitly supplied by the fixture"),
        )
        self.socket_connect = self.patch(
            socket, "create_connection", side_effect=AssertionError("Real network access is forbidden")
        )
        self.socket_dns = self.patch(
            socket, "getaddrinfo", side_effect=AssertionError("Real DNS access is forbidden")
        )
        self.internet_probe = Mock(return_value=True)
        fake_ai = types.ModuleType("src.modules.dedicated_AI")
        fake_ai.check_internet_connection = self.internet_probe
        modules = patch.dict(sys.modules, {"src.modules.dedicated_AI": fake_ai})
        modules.start()
        self.addCleanup(modules.stop)

    def patch(self, obj, name, **kwargs):
        patcher = patch.object(obj, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def cloud_response(self, **updates):
        payload = {
            "idToken": "fixture-id-token",
            "refreshToken": "fixture-refresh-token",
            "localId": "authenticated-fixture-uid",
            "email": "developer@example.com",
        }
        payload.update(updates)
        self.http.side_effect = None
        self.http.return_value = Response(payload)

    def http_error(self, url, code, payload, reason):
        error = urllib.error.HTTPError(
            url, code, reason, {}, io.BytesIO(json.dumps(payload).encode("utf-8"))
        )
        self.addCleanup(error.close)
        return error

    def assert_session_cleared(self):
        self.assertFalse(self.service.is_authenticated)
        self.assertFalse(self.service.is_cloud_authenticated)
        self.assertIsNone(self.service._id_token)
        self.assertIsNone(self.service._refresh_token)
        self.assertIsNone(self.service.user_uid)

    def test_package_offline_mode_never_blocks_cloud_operations(self):
        self.disabled.return_value = True
        self.cloud_response()
        operations = (
            lambda: self.service.authenticate("developer@example.com", "firebase-fixture-password"),
            lambda: self.service.send_password_reset_email("developer@example.com"),
            lambda: self.service.test_firebase_connection(
                "fixture-web-api-key", "https://fixture.firebasedatabase.app/"
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                ok, message = operation()
                self.assertTrue(ok, message)
                self.assertNotIn("offline mode", message.lower())
        self.internet_probe.assert_not_called()
        self.assertEqual(self.http.call_count, 3)
        self.disabled.assert_not_called()
        self.socket_connect.assert_not_called()
        self.socket_dns.assert_not_called()

    def test_connection_checker_uses_provider_endpoint_only(self):
        self.http.side_effect = None
        self.http.return_value = Response({})
        self.assertTrue(self.service.check_connection()[0])
        request = self.http.call_args.args[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(request.full_url, "https://fixture.firebasedatabase.app/.json?shallow=true")
        self.assertIsNone(request.data)
        self.assertNotIn("key=", request.full_url)
        self.assertNotIn("auth=", request.full_url)
        self.disabled.assert_not_called()
        self.internet_probe.assert_not_called()

    def test_connection_probe_reads_no_database_data(self):
        response = Response({})
        response.read = Mock(side_effect=AssertionError("A connection check must not read database data"))
        self.http.side_effect = None
        self.http.return_value = response
        self.assertTrue(self.service.check_connection()[0])
        response.read.assert_not_called()

    def test_unconfigured_connection_checker_probes_firebase_auth_without_credentials(self):
        self.config["firebase_database_url"] = ""
        self.http.side_effect = self.http_error("https://identitytoolkit.googleapis.com/", 404, {}, "route missing")
        self.assertTrue(self.service.check_connection()[0])
        self.assertEqual(self.http.call_args.args[0].full_url, "https://identitytoolkit.googleapis.com/")
        self.assertEqual(self.http.call_args.args[0].get_method(), "HEAD")
        self.assertFalse(self.service.is_cloud_authenticated)

    def test_no_preflight_network_requests(self):
        self.http.assert_not_called()
        self.internet_probe.assert_not_called()

    def test_valid_cloud_authentication_stores_verified_session(self):
        self.cloud_response()
        ok, message = self.service.authenticate(" developer@example.com ", "firebase-fixture-password")
        self.assertTrue(ok, message)
        self.assertTrue(self.service.is_cloud_authenticated)
        self.assertEqual(self.service.user_uid, "authenticated-fixture-uid")
        self.assertEqual(self.service._id_token, "fixture-id-token")
        self.assertEqual(self.service._refresh_token, "fixture-refresh-token")
        request = self.http.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.full_url,
            "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=fixture-web-api-key",
        )
        self.assertEqual(json.loads(request.data)["email"], "developer@example.com")

    def test_firebase_endpoint_is_authoritative_when_generic_probe_fails(self):
        self.internet_probe.return_value = False
        self.cloud_response()
        ok, message = self.service.authenticate("developer@example.com", "firebase-fixture-password")
        self.assertTrue(ok, message)
        self.internet_probe.assert_not_called()
        self.http.return_value = Response({})
        ok, message = self.service.test_firebase_connection("fixture-web-api-key", "https://fixture.firebasedatabase.app/")
        self.assertTrue(ok, message)
        self.internet_probe.assert_not_called()

    def test_endpoint_test_rejects_non_firebase_https_hosts_before_http(self):
        for url in ("http://fixture.firebasedatabase.app/", "https://fixture.firebasedatabase.app.evil.invalid/",
                    "https://user:password@fixture.firebasedatabase.app/", "https://fixture.firebasedatabase.app/private",
                    "https://evil.invalid/firebasedatabase.app/"):
            with self.subTest(url=url):
                ok, _ = self.service.test_firebase_connection("fixture-web-api-key", url)
                self.assertFalse(ok)
        self.http.assert_not_called()

    def test_cloud_authentication_preserves_password_spaces(self):
        self.cloud_response()
        password = "  firebase-fixture-password  "
        ok, message = self.service.authenticate("developer@example.com", password)
        self.assertTrue(ok, message)
        self.assertEqual(json.loads(self.http.call_args.args[0].data)["password"], password)

    def test_blank_password_does_not_attempt_cloud_authentication(self):
        ok, _ = self.service.authenticate("developer@example.com", " \t ")
        self.assertFalse(ok)
        self.http.assert_not_called()
        self.internet_probe.assert_not_called()
        self.assert_session_cleared()

    def test_firebase_rejection_is_distinct_from_offline_or_transport_error(self):
        payload = {"error": {"message": "INVALID_LOGIN_CREDENTIALS"}}
        self.http.side_effect = self.http_error(
            "https://identitytoolkit.googleapis.com/fixture", 400, payload, "Bad Request",
        )
        ok, message = self.service.authenticate("developer@example.com", "firebase-fixture-password")
        self.assertFalse(ok)
        self.assertIn("email or password is incorrect", message)
        self.assertNotIn("offline mode", message.lower())
        self.assert_session_cleared()

    def test_transport_failure_does_not_claim_invalid_credentials(self):
        self.http.side_effect = TimeoutError("fixture request timed out")
        ok, message = self.service.authenticate("developer@example.com", "firebase-fixture-password")
        self.assertFalse(ok)
        self.assertIn("timed out", message)
        self.assertNotIn("invalid credentials", message.lower())
        self.assert_session_cleared()

    def test_malformed_authentication_response_does_not_create_session(self):
        payloads = (
            {},
            {"idToken": "fixture-id-token"},
            {"localId": "authenticated-fixture-uid"},
            {"idToken": "", "localId": "authenticated-fixture-uid"},
            {"idToken": "fixture-id-token", "localId": ""},
        )
        self.http.side_effect = None
        for payload in payloads:
            with self.subTest(payload=payload):
                self.http.return_value = Response(payload)
                ok, _ = self.service.authenticate("developer@example.com", "firebase-fixture-password")
                self.assertFalse(ok)
                self.assert_session_cleared()

    def test_uid_alias_in_valid_response_is_accepted(self):
        self.cloud_response(localId=None, uid="authenticated-fixture-uid")
        ok, message = self.service.authenticate("developer@example.com", "firebase-fixture-password")
        self.assertTrue(ok, message)
        self.assertTrue(self.service.is_cloud_authenticated)
        self.assertEqual(self.service.user_uid, "authenticated-fixture-uid")

    def test_failed_retry_clears_previous_cloud_session(self):
        self.service._is_authenticated = True
        self.service._id_token = "previous-fixture-token"
        self.service._refresh_token = "previous-fixture-refresh"
        self.service._uid = "previous-fixture-uid"
        self.service._user_email = "previous@example.com"
        self.disabled.return_value = True
        self.http.side_effect = TimeoutError("fixture connection failure")
        ok, _ = self.service.authenticate("developer@example.com", "firebase-fixture-password")
        self.assertFalse(ok)
        self.assert_session_cleared()
        self.http.assert_called_once()

    def test_local_key_unlocks_without_claiming_cloud_authentication(self):
        self.disabled.return_value = True
        ok, message = self.service.authenticate("developer@example.com", "local-fixture-key")
        self.assertTrue(ok, message)
        self.assertTrue(self.service.is_authenticated)
        self.assertFalse(self.service.is_cloud_authenticated)
        self.assertIsNone(self.service._id_token)
        self.internet_probe.assert_not_called()
        self.http.assert_not_called()

    def test_old_universal_owner_key_does_not_unlock_developer_tickets(self):
        self.config["use_firebase"] = False
        ok, _ = self.service.authenticate("developer@example.com", "owner")
        self.assertFalse(ok)
        self.assert_session_cleared()
        self.http.assert_not_called()

    def test_http_helper_ignores_package_mode_and_never_follows_redirects(self):
        request = owner.urllib.request.Request("https://fixture.firebasedatabase.app/.json?auth=fixture-token")
        with patch.object(owner.urllib.request, "build_opener") as create_opener:
            self.disabled.return_value = True
            REAL_OPEN_FIREBASE_REQUEST(request, timeout=4.0)
            handler = create_opener.call_args.args[0]
            self.assertIsNone(handler.redirect_request(request, None, 302, "redirect", {}, "https://other.invalid"))
            create_opener.return_value.open.assert_called_once_with(request, timeout=4.0)
            self.disabled.assert_not_called()

    def test_http_helper_rejects_insecure_and_foreign_hosts(self):
        with patch.object(owner.urllib.request, "build_opener") as create_opener:
            for url in ("http://fixture.firebasedatabase.app/", "https://other.invalid/",
                        "https://fixture.firebasedatabase.app:444/", "https://user:password@fixture.firebasedatabase.app/"):
                with self.subTest(url=url), self.assertRaises((ValueError, offline_runtime.OfflineDependencyError)):
                    REAL_OPEN_FIREBASE_REQUEST(owner.urllib.request.Request(url), timeout=4.0)
            create_opener.assert_not_called()

    def test_oversized_auth_and_ticket_responses_are_bounded(self):
        class LargeResponse(Response):
            def __init__(self, data):
                self.data, self.status, self.read_sizes = data, 200, []
            def read(self, size):
                self.read_sizes.append(size)
                return self.data[:size]
        oversized_auth = LargeResponse(b"x" * (owner._AUTH_RESPONSE_BYTES + 100))
        self.http.side_effect = None
        self.http.return_value = oversized_auth
        self.assertFalse(self.service.authenticate("developer@example.com", "fixture-password")[0])
        self.assert_session_cleared()
        self.assertEqual(oversized_auth.read_sizes, [owner._AUTH_RESPONSE_BYTES + 1])
        self.cloud_response()
        self.assertTrue(self.service.authenticate("developer@example.com", "fixture-password")[0])
        oversized_tickets = LargeResponse(b"x" * (owner._TICKET_RESPONSE_BYTES + 100))
        self.http.return_value = oversized_tickets
        self.assertEqual(self.service.get_tickets(), [])
        self.assertEqual(oversized_tickets.read_sizes, [owner._TICKET_RESPONSE_BYTES + 1])

    def test_auth_error_response_is_bounded_and_closed(self):
        error = self.http_error("https://fixture.invalid", 400, {"error": {"message": "x" * 20000}}, "error")
        self.http.side_effect = error
        self.assertFalse(self.service.authenticate("developer@example.com", "fixture-password")[0])
        self.assertTrue(error.fp.closed)

    def test_protected_endpoint_response_only_proves_reachability(self):
        for code in (401, 403, 404, 405, 503):
            with self.subTest(code=code):
                self.http.side_effect = self.http_error(
                    "https://fixture.firebasedatabase.app/.json?shallow=true", code,
                    {"error": "Permission denied"}, "Permission denied",
                )
                ok, message = self.service.test_firebase_connection(
                    "fixture-web-api-key", "https://fixture.firebasedatabase.app/"
                )
                self.assertTrue(ok, message)
                self.assertIn("connected", message.lower())
                self.assertIn("sign in", message.lower())
                self.assertFalse(self.service.is_cloud_authenticated)
                self.assertTrue(self.http.side_effect.fp.closed)

    def test_cloud_crud_uses_authenticated_endpoint_without_generic_probe(self):
        self.cloud_response()
        self.assertTrue(self.service.authenticate("developer@example.com", "firebase-fixture-password")[0])
        self.internet_probe.return_value = False
        self.http.reset_mock()
        self.http.return_value = Response({})
        self.service._save_local_tickets.side_effect = None
        self.assertEqual(self.service.get_tickets(), [])
        self.assertTrue(self.service.create_ticket("Fixture ticket"))
        self.service._read_local_tickets.return_value = [{"id": "fixture-ticket", "title": "Fixture"}]
        self.assertTrue(self.service.update_ticket("fixture-ticket", {"status": "Resolved"}))
        self.assertTrue(self.service.delete_ticket("fixture-ticket"))
        self.assertEqual(self.http.call_count, 4)
        self.assertEqual([call.args[0].get_method() for call in self.http.call_args_list], ["GET", "PUT", "PATCH", "DELETE"])
        for call in self.http.call_args_list:
            self.assertIn("/owner/authenticated-fixture-uid/tickets", call.args[0].full_url)
        self.internet_probe.assert_not_called()

    def test_local_key_never_requests_remote_ticket_paths(self):
        self.assertTrue(self.service.authenticate("developer@example.com", "local-fixture-key")[0])
        self.service._save_local_tickets.side_effect = None
        self.assertIsNone(self.service._get_firebase_tickets_url())
        self.service.get_tickets()
        self.service.create_ticket("Local fixture ticket")
        self.service._read_local_tickets.return_value = [{"id": "fixture-ticket", "title": "Fixture"}]
        self.service.update_ticket("fixture-ticket", {"status": "Closed"})
        self.service.delete_ticket("fixture-ticket")
        self.http.assert_not_called()
        self.internet_probe.assert_not_called()

    def test_authenticated_sync_is_independent_of_package_offline_mode(self):
        self.cloud_response()
        self.assertTrue(self.service.authenticate("developer@example.com", "firebase-fixture-password")[0])
        self.http.reset_mock()
        self.disabled.return_value = True
        self.service._save_local_tickets.side_effect = None
        self.assertIsNotNone(self.service._get_firebase_tickets_url())
        self.service.get_tickets()
        self.service.create_ticket("Offline fixture ticket")
        self.service._push_to_firebase({"id": "fixture-ticket"})
        self.assertEqual(self.http.call_count, 3)
        self.disabled.assert_not_called()

    def test_errors_never_reveal_request_urls_or_tokens(self):
        marker = "fixture-secret-that-must-not-display"
        self.http.side_effect = urllib.error.URLError("https://fixture.invalid/?key=" + marker)
        ok, message = self.service.authenticate("developer@example.com", "firebase-fixture-password")
        self.assertFalse(ok)
        self.assertNotIn(marker, message)
        self.assertNotIn("https://", message)
        ok, message = self.service.test_firebase_connection("fixture-key", "https://fixture.firebasedatabase.app/")
        self.assertFalse(ok)
        self.assertNotIn(marker, message)
        self.assertNotIn("https://", message)

    def test_cloud_cache_isolated_by_uid_and_provider_never_uses_local_legacy(self):
        scratch = ROOT / "temp" / "audit"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="owner-ticket-cache-", dir=scratch) as directory:
            self.service._storage_dir = Path(directory)
            self.service._tickets_file = Path(directory) / ".owner_tickets.json"
            self.service._tickets_file.write_text(json.dumps([{"id": "local-only"}]), encoding="utf-8")
            del self.service._read_local_tickets
            del self.service._save_local_tickets
            self.service._is_authenticated = True
            self.service._id_token = "fixture-token"
            self.service._uid = "account-a"
            first = self.service._ticket_cache_path()
            self.assertNotEqual(first, self.service._tickets_file)
            self.assertEqual(self.service._read_local_tickets(), [])
            self.assertTrue(self.service._save_local_tickets([{"id": "account-a-private"}]))
            self.service._uid = "account-b"
            self.assertNotEqual(first, self.service._ticket_cache_path())
            self.assertEqual(self.service._read_local_tickets(), [])
            self.disabled.return_value = True
            self.http.side_effect = TimeoutError("fixture connection unavailable")
            self.assertEqual(self.service.get_tickets(), [])
            self.service._uid = "account-a"
            self.assertEqual(self.service._read_local_tickets(), [{"id": "account-a-private"}])
            self.config["firebase_database_url"] = "https://another.firebasedatabase.app/"
            self.assertNotEqual(first, self.service._ticket_cache_path())
            self.assertEqual(self.service._read_local_tickets(), [])
            self.service.logout()
            self.assertEqual(self.service._read_local_tickets(), [{"id": "local-only"}])
        self.http.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
