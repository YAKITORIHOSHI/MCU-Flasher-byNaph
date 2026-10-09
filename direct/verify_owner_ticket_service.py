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
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.core import owner_tickets as owner
from src.modules import offline_runtime


class Response:
    """A complete HTTP response supplied by the fixture, never a real socket."""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


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
            "owner_password_hash": hashlib.sha256(b"local-fixture-key").hexdigest(),
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
        self.patch(owner, "load_encrypted_vault", side_effect=AssertionError("Live vault reads are forbidden"))
        self.disabled = self.patch(owner, "network_access_disabled", return_value=False)
        self.patch(offline_runtime, "network_access_disabled", new=self.disabled)
        self.http = self.patch(
            owner.urllib.request, "urlopen",
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

    def test_offline_cloud_operations_do_not_probe_or_request_http(self):
        self.disabled.return_value = True
        self.assertFalse(owner.is_internet_available())
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
                self.assertFalse(ok)
                self.assertIn("offline mode", message.lower())
                self.assertIn("restart", message.lower())
                self.assertNotIn("invalid credentials", message.lower())
        self.internet_probe.assert_not_called()
        self.http.assert_not_called()
        self.socket_connect.assert_not_called()
        self.socket_dns.assert_not_called()

    def test_online_preflight_has_no_offline_error(self):
        self.assertEqual(owner.cloud_network_error(), "")
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
        self.assertIn("INVALID_LOGIN_CREDENTIALS", message)
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
        ok, _ = self.service.authenticate("developer@example.com", "firebase-fixture-password")
        self.assertFalse(ok)
        self.assert_session_cleared()
        self.http.assert_not_called()

    def test_local_key_unlocks_without_claiming_cloud_authentication(self):
        self.disabled.return_value = True
        ok, message = self.service.authenticate("developer@example.com", "local-fixture-key")
        self.assertTrue(ok, message)
        self.assertTrue(self.service.is_authenticated)
        self.assertFalse(self.service.is_cloud_authenticated)
        self.assertIsNone(self.service._id_token)
        self.internet_probe.assert_not_called()
        self.http.assert_not_called()

    def test_protected_endpoint_response_only_proves_reachability(self):
        for code in (401, 403):
            with self.subTest(code=code):
                self.http.side_effect = self.http_error(
                    "https://fixture.firebasedatabase.app/.json?shallow=true", code,
                    {"error": "Permission denied"}, "Permission denied",
                )
                ok, message = self.service.test_firebase_connection(
                    "fixture-web-api-key", "https://fixture.firebasedatabase.app/"
                )
                self.assertTrue(ok, message)
                self.assertIn("reach", message.lower())
                self.assertIn("not verified", message.lower())
                self.assertFalse(self.service.is_cloud_authenticated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
