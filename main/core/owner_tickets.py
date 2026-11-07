#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.core.owner_tickets — Private Owner Issue Ticket Service & Firebase Bridge.

Provides secure local offline storage and Firebase Realtime Database / Authentication
REST API integration for private owner defect tracking and issue management.
Adheres to owner-isolated database rules (/owner/{$uid}/tickets).
All methods are heavily guarded with try-catch blocks to operate seamlessly offline.
"""
from __future__ import annotations

import os
import sys
import json
import time
import uuid
import hashlib
import hmac
import secrets
import socket
import threading
import urllib.request
import urllib.error
import unicodedata
from urllib.parse import quote, urlencode, urlsplit
from pathlib import Path
from datetime import datetime
from typing import Optional, Any, List, Dict

from main.core.file_utils import (
    hide_hidden_attribute,
    ensure_file_writable,
)
from src.modules.offline_runtime import firebase_network_access
from main.core.firebase_provider import firebase_configuration, validate_firebase_destination
from main.core.credential_store import (
    CredentialStore,
    SecureStorageError,
)

_TICKETS_FILE_NAME = ".owner_tickets.json"
_CONFIG_FILE_NAME  = ".owner_config.json"
_DEFAULT_ROOT_COLLECTION = "owner"
_LOCAL_KEY_ITERATIONS = 600_000
_PROVIDER_KEYS = frozenset(firebase_configuration()) | {"firebase_root_collection", "use_firebase"}
_OWNER_METADATA_KEYS = {"owner_email", "firebase_user_uid"}
_AUTH_RESPONSE_BYTES = 64 * 1024
_TICKET_RESPONSE_BYTES = 4 * 1024 * 1024
_DEFAULT_TICKET_CATEGORIES = (
    "GUI / Interface", "Monaco Code Editor", "Serial Monitor",
    "PlatformIO / Toolchain", "Hardware & COM Port", "Low-End & HDD", "General Defect",
)
_MAX_TICKET_CATEGORIES = 64
_MAX_TICKET_CATEGORY_LENGTH = 80
# A kept Developer Access session lives only in the OS credential vault, never in a file.
_KEPT_SESSION_RECORD = "developer-session:"
# Firebase sends a keep-alive event every 30 seconds on an open event stream.
_STREAM_READ_TIMEOUT = 75.0
_STREAM_LINE_LIMIT = 64 * 1024
_POLL_INTERVAL = 30.0


def _clean_ticket_category(value: Any) -> str:
    if not isinstance(value, str) or any(unicodedata.category(char) == "Cc" for char in value):
        return ""
    value = " ".join(value.split())
    if not value or len(value) > _MAX_TICKET_CATEGORY_LENGTH:
        return ""
    return value


def _normalize_ticket_categories(values: Any) -> list[str]:
    categories = []
    seen = set()
    source = list(_DEFAULT_TICKET_CATEGORIES)
    if isinstance(values, list):
        source.extend(values)
    for value in source:
        category = _clean_ticket_category(value)
        key = category.casefold()
        if category and key not in seen:
            categories.append(category)
            seen.add(key)
            if len(categories) >= _MAX_TICKET_CATEGORIES:
                break
    return categories

class _NoFirebaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _open_firebase_request(request, *, timeout):
    host = validate_firebase_destination(request.full_url)
    with firebase_network_access(host):
        return urllib.request.build_opener(_NoFirebaseRedirect()).open(request, timeout=timeout)


def _read_firebase_json(response, limit):
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Firebase response exceeds the supported size limit.")
    return json.loads(raw.decode("utf-8"))


def _cloud_request_error(error: Exception) -> str:
    """Never display request URLs, credentials or tokens from exception strings."""
    if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
        return "The cloud connection timed out. Try again."
    return "Could not reach the cloud. Check your connection and try again."


def _firebase_database_url(value: str) -> str:
    clean_url = (value or "").strip().rstrip("/")
    validate_firebase_destination(clean_url)
    if clean_url != firebase_configuration()["firebase_database_url"]:
        raise ValueError("Use MCU Flasher's built-in Firebase database.")
    return clean_url


def _firebase_auth_error(error: urllib.error.HTTPError) -> str:
    try:
        code = _read_firebase_json(error, 8192).get("error", {}).get("message", "").split(" : ", 1)[0]
        messages = {
            "INVALID_LOGIN_CREDENTIALS": "The email or password is incorrect.",
            "EMAIL_NOT_FOUND": "The email or password is incorrect.",
            "INVALID_PASSWORD": "The email or password is incorrect.",
            "USER_DISABLED": "This account has been disabled.",
            "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many sign-in attempts. Try again later.",
            "OPERATION_NOT_ALLOWED": "Sign-in is not enabled for this connection.",
            "API_KEY_INVALID": "The cloud connection needs to be updated.",
            "INVALID_EMAIL": "Enter a valid email address.",
        }
        if code in messages:
            return messages[code]
    except (ValueError, AttributeError, TypeError):
        pass
    finally:
        error.close()
    return "Sign-in failed. Check your account and cloud connection."


def get_owner_portal_storage_dir() -> Path:
    """Return the private, isolated directory for owner portal settings and offline cache.

    Completely separated from the local notification database (src/dbs/dbs_notif.json).
    Uses %LOCALAPPDATA%/MCUFlasher/.owner_portal or ~/.mcu_flasher/.owner_portal.
    """
    try:
        local_app_data = os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else None
        if local_app_data:
            base = Path(local_app_data) / "MCUFlasher" / ".owner_portal"
        else:
            base = Path.home() / ".mcu_flasher" / ".owner_portal"
        base.mkdir(parents=True, exist_ok=True)
        hide_hidden_attribute(base)
        return base
    except Exception:
        fallback = Path.home() / ".mcu_flasher_owner"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


class OwnerTicketService:
    """Manages private developer issue tickets with local JSON persistence

    and the built-in Firebase Realtime Database & Auth REST endpoints.
    Completely decoupled from the local notification database (src/dbs/).
    Adheres to user-isolated database rules (/users/{$uid}/tickets).
    All operations are safe for offline operation without crashing.
    """

    def __init__(self, storage_dir: Optional[Path | str] = None, project_root: Optional[Path | str] = None, *, credential_store=None):
        self._credential_store = credential_store or CredentialStore()
        try:
            if storage_dir:
                self._storage_dir = Path(storage_dir)
            else:
                self._storage_dir = get_owner_portal_storage_dir()

            self._tickets_file = self._storage_dir / _TICKETS_FILE_NAME
            self._config_file = self._storage_dir / _CONFIG_FILE_NAME
        except Exception:
            self._storage_dir = get_owner_portal_storage_dir()
            self._tickets_file = self._storage_dir / _TICKETS_FILE_NAME
            self._config_file = self._storage_dir / _CONFIG_FILE_NAME

        self._id_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._uid: Optional[str] = None
        self._user_email: Optional[str] = None
        self._is_authenticated: bool = False
        self._active_root_collection: Optional[str] = None
        self._keeping_session = False

        try:
            self._init_storage()
        except Exception:
            pass

    def _init_storage(self) -> None:
        """Ensure storage directory and files exist with hidden Windows attributes."""
        try:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            if not self._tickets_file.exists():
                ensure_file_writable(self._tickets_file)
                self._tickets_file.write_text("[]", encoding="utf-8")
                hide_hidden_attribute(self._tickets_file)

            if not self._config_file.exists():
                default_cfg = {
                    "local_access": {},
                    "ticket_categories": list(_DEFAULT_TICKET_CATEGORIES),
                    "owner_email": "",
                    "firebase_user_uid": "",
                }
                ensure_file_writable(self._config_file)
                self._config_file.write_text(json.dumps(default_cfg, indent=2), encoding="utf-8")
                hide_hidden_attribute(self._config_file)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # Configuration
    # ─────────────────────────────────────────────────────────────────────────

    def _read_config(self) -> Dict[str, Any]:
        try:
            cfg = json.loads(self._config_file.read_text(encoding="utf-8"))
            return cfg if isinstance(cfg, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_config(self, cfg: Dict[str, Any]) -> bool:
        temporary = self._config_file.with_name(self._config_file.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            if sys.platform != "win32":
                temporary.chmod(0o600)
            ensure_file_writable(self._config_file)
            temporary.replace(self._config_file)
            hide_hidden_attribute(self._config_file)
            return True
        except OSError:
            return False
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _local_access_valid(record) -> bool:
        if not isinstance(record, dict):
            return False
        try:
            return (record.get("algorithm") == "pbkdf2-sha256"
                    and 100_000 <= int(record["iterations"]) <= 1_000_000
                    and len(bytes.fromhex(record["salt"])) == 16
                    and len(bytes.fromhex(record["hash"])) == 32)
        except (KeyError, TypeError, ValueError):
            return False

    def get_config(self) -> Dict[str, Any]:
        """Read local access preferences and protected account metadata.

        Legacy provider fields are ignored immediately. Account metadata moves to
        the OS vault only after a verified write; local tickets/keys stay intact.
        """
        cfg = self._read_config()
        metadata = {key: cfg[key] for key in _OWNER_METADATA_KEYS if cfg.get(key)}
        try:
            if metadata:
                saved = self._credential_store.get("owner_metadata") or {}
                saved.update(metadata)
                self._credential_store.set("owner_metadata", saved)
                if self._credential_store.get("owner_metadata") != saved:
                    raise ValueError("Secure save verification failed")
            cleaned = {key: value for key, value in cfg.items()
                       if key not in (_PROVIDER_KEYS - {"firebase_root_collection"}) | _OWNER_METADATA_KEYS | {"owner_password_hash"}}
            if cleaned.get("firebase_root_collection") not in ("owner", "users"):
                cleaned.pop("firebase_root_collection", None)
            if cleaned != cfg and self._write_config(cleaned):
                cfg = cleaned
        except Exception:
            pass
        try:
            private_metadata = self._credential_store.get("owner_metadata") or {}
        except Exception:
            private_metadata = {}
        cfg.update(firebase_configuration())
        cfg.update({key: private_metadata.get(key, "") for key in _OWNER_METADATA_KEYS})
        cfg["firebase_root_collection"] = (cfg.get("firebase_root_collection")
                                           if cfg.get("firebase_root_collection") in ("owner", "users")
                                           else _DEFAULT_ROOT_COLLECTION)
        cfg["use_firebase"] = True
        cfg["ticket_categories"] = _normalize_ticket_categories(cfg.get("ticket_categories"))
        cfg["local_access_configured"] = self._local_access_valid(cfg.get("local_access"))
        return cfg

    def get_ticket_categories(self) -> list[str]:
        """Return the built-in starting set plus saved user-defined categories."""
        return _normalize_ticket_categories(self._read_config().get("ticket_categories"))

    def add_ticket_category(self, value: str) -> tuple[bool, str, list[str]]:
        """Add a bounded category to the per-user developer portal settings."""
        if not isinstance(value, str) or any(unicodedata.category(char) == "Cc" for char in value):
            return False, "Enter a category using up to 80 characters.", self.get_ticket_categories()
        category = _clean_ticket_category(value)
        if not category:
            return False, "Enter a category using 1 to 80 characters.", self.get_ticket_categories()
        if category.casefold() == "all categories":
            return False, "Choose a different category name.", self.get_ticket_categories()
        categories = self.get_ticket_categories()
        if any(existing.casefold() == category.casefold() for existing in categories):
            return False, "That category already exists.", categories
        if len(categories) >= _MAX_TICKET_CATEGORIES:
            return False, "The category list is full.", categories
        categories.append(category)
        if not self.save_config({"ticket_categories": categories}):
            return False, "The category could not be saved. Try again.", self.get_ticket_categories()
        return True, f"Added {category}.", categories

    def update_vault(self, updates: Dict[str, Any]) -> bool:
        """Compatibility entry point: save to the current user's OS vault."""
        return self.save_config(updates)

    def save_config(self, new_cfg: Dict[str, Any]) -> bool:
        try:
            if _PROVIDER_KEYS.intersection(new_cfg):
                return False
            cfg = self._read_config()
            metadata = {key: cfg[key] for key in _OWNER_METADATA_KEYS if cfg.get(key)}
            metadata.update({key: value for key, value in new_cfg.items() if key in _OWNER_METADATA_KEYS})
            if metadata:
                saved = self._credential_store.get("owner_metadata") or {}
                saved.update(metadata)
                self._credential_store.set("owner_metadata", saved)
                if self._credential_store.get("owner_metadata") != saved:
                    return False
            cfg.update({key: value for key, value in new_cfg.items()
                        if key not in _PROVIDER_KEYS | _OWNER_METADATA_KEYS
                        and key not in {"owner_password_hash", "local_access_configured"}})
            cfg = {key: value for key, value in cfg.items()
                   if key not in (_PROVIDER_KEYS - {"firebase_root_collection"}) | _OWNER_METADATA_KEYS | {"owner_password_hash", "local_access_configured"}}
            if cfg.get("firebase_root_collection") not in ("owner", "users"):
                cfg.pop("firebase_root_collection", None)
            return self._write_config(cfg)
        except Exception:
            return False

    def configure_local_access(self, password: str) -> tuple[bool, str]:
        """Configure a separate local key; changing an existing key requires login."""
        cfg = self.get_config()
        if cfg.get("local_access_configured") and not self.is_authenticated:
            return False, "Sign in before changing the local access key."
        if not isinstance(password, str) or len(password) < 12 or not password.strip():
            return False, "Choose a local access key with at least 12 characters."
        salt = secrets.token_bytes(16)
        record = {"algorithm": "pbkdf2-sha256", "iterations": _LOCAL_KEY_ITERATIONS,
                  "salt": salt.hex(), "hash": hashlib.pbkdf2_hmac(
                      "sha256", password.encode("utf-8"), salt, _LOCAL_KEY_ITERATIONS).hex()}
        if not self.save_config({"local_access": record}):
            return False, "The local access key could not be saved."
        return True, "Local access key saved. It grants access to this computer's local tickets only."

    def is_firebase_configured(self) -> bool:
        try:
            cfg = self.get_config()
            return bool(cfg.get("firebase_api_key") and cfg.get("firebase_database_url"))
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Authentication (Local Access & Firebase Auth REST API)
    # ─────────────────────────────────────────────────────────────────────────

    def authenticate(self, email_or_user: str, password: str, *, keep_signed_in: bool = False) -> tuple[bool, str]:
        """Authenticate with the configured local key or Firebase Auth REST API.

        ``keep_signed_in`` stores a renewable session in the OS credential vault so the
        next launch signs in without a password; without it any kept session is forgotten.
        """
        try:
            self.logout()
            email_clean = (email_or_user or "").strip()
            pwd_clean = password or ""

            if not pwd_clean.strip():
                return False, "Password cannot be blank."
            if keep_signed_in and not self._credential_store.status[0]:
                return False, ("Keep me signed in needs your system's secure credential storage. "
                               + self._credential_store.status[1])

            cfg = self.get_config()

            record = cfg.get("local_access")
            if self._local_access_valid(record):
                entered = hashlib.pbkdf2_hmac("sha256", pwd_clean.encode("utf-8"),
                    bytes.fromhex(record["salt"]), int(record["iterations"]))
                if hmac.compare_digest(entered, bytes.fromhex(record["hash"])):
                    self._is_authenticated = True
                    self._user_email = email_clean or "Local developer"
                    # A local key never supplies a cloud UID or token.
                    return True, self._keep_session(keep_signed_in, {
                        "kind": "local", "email": self._user_email, "key": record["hash"]},
                        "Signed in to local developer tickets.")

            # 2. Firebase Cloud Authentication via REST API
            if cfg.get("use_firebase") and cfg.get("firebase_api_key"):
                api_key = cfg["firebase_api_key"]
                url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
                payload = json.dumps({
                    "email": email_clean or cfg.get("owner_email", ""),
                    "password": pwd_clean,
                    "returnSecureToken": True,
                }).encode("utf-8")

                try:
                    req = urllib.request.Request(
                        url,
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with _open_firebase_request(req, timeout=5.0) as resp:
                        data = _read_firebase_json(resp, _AUTH_RESPONSE_BYTES)
                        token = data.get("idToken") if isinstance(data, dict) else None
                        uid = (data.get("localId") or data.get("uid")) if isinstance(data, dict) else None
                        if not isinstance(token, str) or not token or not isinstance(uid, str) or not uid:
                            return False, "Firebase returned an incomplete sign-in response. Please try again."
                        self._id_token = token
                        self._refresh_token = data.get("refreshToken")
                        self._uid = uid
                        self._user_email = data.get("email", email_clean)
                        self._is_authenticated = True
                        return True, self._keep_session(keep_signed_in, self._firebase_session_record(),
                                                        "Firebase Cloud authentication successful.")
                except urllib.error.HTTPError as e:
                    return False, _firebase_auth_error(e)
                except Exception as e:
                    return False, _cloud_request_error(e)

            return False, "Invalid credentials. Access denied."
        except Exception as e:
            return False, _cloud_request_error(e)

    def send_password_reset_email(self, email: Optional[str] = None) -> tuple[bool, str]:
        """Send a password reset email via Firebase Identity Toolkit."""
        try:
            cfg = self.get_config()
            api_key = cfg.get("firebase_api_key", "")
            target_email = (email or cfg.get("owner_email", "")).strip()
            if not api_key:
                return False, "Firebase API Key is missing."
            if not target_email:
                return False, "Target email address cannot be empty."

            url = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={api_key}"
            payload = json.dumps({
                "requestType": "PASSWORD_RESET",
                "email": target_email,
            }).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _open_firebase_request(req, timeout=5.0) as resp:
                _read_firebase_json(resp, _AUTH_RESPONSE_BYTES)
                return True, f"Password reset email sent to {target_email}."
        except urllib.error.HTTPError as e:
            return False, _firebase_auth_error(e)
        except Exception as e:
            return False, _cloud_request_error(e)


    def logout(self) -> None:
        try:
            self._is_authenticated = False
            self._id_token = None
            self._refresh_token = None
            self._uid = None
            self._user_email = None
            self._active_root_collection = None
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # Kept sessions ("Keep me signed in") and sign-out
    # ─────────────────────────────────────────────────────────────────────────

    def _kept_session_name(self) -> str:
        provider = firebase_configuration()
        scope = "\n".join((provider["firebase_database_url"], provider["firebase_project_id"]))
        return _KEPT_SESSION_RECORD + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]

    def _firebase_session_record(self) -> Dict[str, Any]:
        return {"kind": "firebase", "uid": self._uid, "email": self._user_email or "",
                "refresh_token": self._refresh_token or ""}

    def _forget_kept_session(self) -> bool:
        try:
            if self._credential_store.status[0]:
                self._credential_store.delete(self._kept_session_name())
            return True
        except Exception:
            return False

    def _keep_session(self, keep: bool, record: Dict[str, Any], message: str) -> str:
        """Store or forget the kept session after a successful sign-in; never fail the sign-in."""
        if not keep:
            self._forget_kept_session()
            self._keeping_session = False
            return message
        if record.get("kind") == "firebase" and not record.get("refresh_token"):
            self._keeping_session = False
            return message + " This sign-in cannot be kept; sign in again next time."
        try:
            self._credential_store.set(self._kept_session_name(), record)
            self._keeping_session = True
            return message + " You will stay signed in on this device."
        except Exception:
            self._keeping_session = False
            return message + " This device could not keep you signed in, so sign in again next time."

    @property
    def keeps_session(self) -> bool:
        return bool(getattr(self, "_keeping_session", False))

    def restore_session(self) -> tuple[bool, str]:
        """Sign in from a session kept in the OS vault. Returns (signed in, message for the user)."""
        self.logout()
        try:
            if not self._credential_store.status[0]:
                return False, ""
            record = self._credential_store.get(self._kept_session_name())
        except SecureStorageError as error:
            return False, str(error)
        except Exception:
            return False, ""
        if not isinstance(record, dict):
            return False, ""
        kind = record.get("kind")
        if kind == "local":
            try:
                current = self.get_config().get("local_access")
            except Exception:
                current = None
            key = record.get("key")
            if (self._local_access_valid(current) and isinstance(key, str)
                    and hmac.compare_digest(key.encode("ascii", "replace"), current["hash"].encode("ascii", "replace"))):
                self._is_authenticated = True
                self._user_email = str(record.get("email") or "Local developer")[:256]
                self._keeping_session = True
                return True, "Signed in from your kept session."
            self._forget_kept_session()
            return False, "The local access key changed, so your kept session ended. Sign in again."
        if kind != "firebase":
            self._forget_kept_session()
            return False, ""
        uid, refresh = record.get("uid"), record.get("refresh_token")
        if not (isinstance(uid, str) and uid and isinstance(refresh, str) and refresh):
            self._forget_kept_session()
            return False, "Your kept session was incomplete. Sign in again."
        self._uid, self._refresh_token = uid, refresh
        self._user_email = str(record.get("email") or "")[:256] or None
        # Kept from the start, so the renewal below writes its rotated refresh token back.
        self._keeping_session = True
        ok, message = self.refresh_id_token()
        if not ok:
            expired = self._refresh_token is None
            self.logout()
            self._keeping_session = False
            if expired:
                self._forget_kept_session()
                return False, "Your kept session has expired. Sign in again."
            return False, message
        self._keeping_session = True
        return True, "Signed in from your kept session."

    def refresh_id_token(self) -> tuple[bool, str]:
        """Exchange the refresh token for a new ID token (they last one hour).

        On a refused token the session ends; on a connection failure it stays as it was.
        """
        try:
            api_key = firebase_configuration()["firebase_api_key"]
            if not self._refresh_token or not self._uid:
                return False, "Sign in again."
            url = f"https://securetoken.googleapis.com/v1/token?key={quote(api_key, safe='')}"
            payload = urlencode({"grant_type": "refresh_token", "refresh_token": self._refresh_token}).encode("ascii")
            req = urllib.request.Request(url, data=payload, method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                with _open_firebase_request(req, timeout=5.0) as resp:
                    data = _read_firebase_json(resp, _AUTH_RESPONSE_BYTES)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code in (400, 401, 403):
                    self._id_token = None
                    self._refresh_token = None
                    self._is_authenticated = False
                    return False, "Your session has expired. Sign in again."
                return False, "The cloud service is unavailable. Try again."
            token = data.get("id_token") if isinstance(data, dict) else None
            refresh = data.get("refresh_token") if isinstance(data, dict) else None
            uid = data.get("user_id") if isinstance(data, dict) else None
            if not (isinstance(token, str) and token and isinstance(refresh, str) and refresh and uid == self._uid):
                return False, "Firebase returned an incomplete session. Sign in again."
            self._id_token, self._refresh_token = token, refresh
            self._is_authenticated = True
            if getattr(self, "_keeping_session", False):
                try:
                    self._credential_store.set(self._kept_session_name(), self._firebase_session_record())
                except Exception:
                    pass  # The current session continues; the next launch may ask to sign in.
            return True, ""
        except Exception as error:
            return False, _cloud_request_error(error)

    def sign_out(self) -> tuple[bool, str]:
        """End this session and forget any kept session, so the next launch asks to sign in."""
        self.logout()
        self._keeping_session = False
        if self._forget_kept_session():
            return True, "Signed out."
        return False, ("Signed out here, but your system's credential storage could not forget the kept "
                       "session. Unlock it and sign out again.")

    @property
    def is_authenticated(self) -> bool:
        return self._is_authenticated

    @property
    def is_cloud_authenticated(self) -> bool:
        return bool(self._is_authenticated and self._id_token and self._uid)

    @property
    def current_user(self) -> str:
        return self._user_email or "Owner"

    @property
    def current_owner(self) -> str:
        return self.current_user

    @property
    def user_uid(self) -> Optional[str]:
        return self._uid

    @property
    def owner_uid(self) -> Optional[str]:
        return self._uid

    # ─────────────────────────────────────────────────────────────────────────
    # Tickets CRUD Operations (Adhering to Firebase security rules: owner/$uid/tickets)
    # ─────────────────────────────────────────────────────────────────────────

    def _get_firebase_tickets_url(self, ticket_id: Optional[str] = None, root_collection: Optional[str] = None) -> Optional[str]:
        """Construct the REST URL adhering to Firebase security rules: /owner/{$uid}/tickets"""
        try:
            if not self.is_cloud_authenticated:
                return None
            cfg = self.get_config()
            db_url = _firebase_database_url(cfg.get("firebase_database_url", ""))
            auth_param = "?" + urlencode({"auth": self._id_token})
            uid = self._uid
            root = (root_collection or cfg.get("firebase_root_collection") or _DEFAULT_ROOT_COLLECTION).strip().strip("/")
            if any(character in root + uid + (ticket_id or "") for character in ".#$[]/\\"):
                return None
            base_path = f"{db_url}/{quote(root, safe='')}/{quote(uid, safe='')}/tickets"

            if ticket_id:
                return f"{base_path}/{quote(ticket_id, safe='')}.json{auth_param}"
            return f"{base_path}.json{auth_param}"
        except Exception:
            return None

    def ticket_feed_url(self) -> Optional[str]:
        """The signed-in account's tickets location, for watching changes; None without a cloud session."""
        try:
            if not self.is_cloud_authenticated:
                return None
            root = self._active_root_collection or self.get_config().get("firebase_root_collection")
            return self._get_firebase_tickets_url(root_collection=root)
        except Exception:
            return None

    def get_tickets(self) -> List[Dict[str, Any]]:
        """Retrieve list of issue tickets from Firebase (if online) or local storage."""
        try:
            cfg = self.get_config()

            # Try the configured service directly; package Offline Mode is unrelated.
            if self.is_cloud_authenticated and cfg.get("use_firebase") and cfg.get("firebase_database_url"):
                candidates: List[str] = []
                configured_root = cfg.get("firebase_root_collection", _DEFAULT_ROOT_COLLECTION)
                for r in [self._active_root_collection, configured_root, "owner", "users"]:
                    if r and r not in candidates:
                        candidates.append(r)

                for root in candidates:
                    try:
                        url = self._get_firebase_tickets_url(root_collection=root)
                        if not url:
                            continue
                        req = urllib.request.Request(url, headers={"User-Agent": "MCUFlasher-OwnerApp/1.0"})
                        with _open_firebase_request(req, timeout=4.0) as resp:
                            raw = _read_firebase_json(resp, _TICKET_RESPONSE_BYTES)
                            if isinstance(raw, dict):
                                tickets = []
                                for k, v in raw.items():
                                    if isinstance(v, dict):
                                        v["id"] = v.get("id") or k
                                        tickets.append(v)
                                self._active_root_collection = root
                                # Sync to local cache
                                self._save_local_tickets(tickets)
                                return self._sort_tickets(tickets)
                            elif raw is None:
                                self._active_root_collection = root
                                return self._sort_tickets(self._read_local_tickets())
                    except urllib.error.HTTPError as e:
                        e.close()
                        if e.code in (401, 403, 404):
                            continue
                        break
                    except Exception:
                        break

            return self._sort_tickets(self._read_local_tickets())
        except Exception:
            try:
                return self._sort_tickets(self._read_local_tickets())
            except Exception:
                return []

    def create_ticket(
        self,
        title: str,
        category: str = "General",
        severity: str = "Medium",
        description: str = "",
        status: str = "Open",
    ) -> Dict[str, Any]:
        """Create a new defect issue ticket."""
        try:
            ticket_id = f"tkt_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            ticket: Dict[str, Any] = {
                "id": ticket_id,
                "title": (title or "Untitled Defect").strip(),
                "category": category or "General",
                "severity": severity or "Medium",
                "status": status or "Open",
                "description": description.strip(),
                "author": self.current_user,
                "created_at": now_iso,
                "updated_at": now_iso,
            }

            # 1. Update local storage immediately
            try:
                tickets = self._read_local_tickets()
                tickets.insert(0, ticket)
                if self._save_local_tickets(tickets) is False:
                    return {}
            except Exception:
                pass

            # 2. Sync to Firebase if configured and online
            try:
                cfg = self.get_config()
                if self.is_cloud_authenticated and cfg.get("use_firebase") and cfg.get("firebase_database_url"):
                    self._push_to_firebase(ticket)
            except Exception:
                pass

            return ticket
        except Exception:
            return {}

    def update_ticket(self, ticket_id: str, updates: Dict[str, Any]) -> bool:
        """Update existing issue ticket fields (status, severity, notes)."""
        try:
            tickets = self._read_local_tickets()
            found = False
            target_ticket = None

            for t in tickets:
                if t.get("id") == ticket_id:
                    t.update(updates)
                    t["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    target_ticket = t
                    found = True
                    break

            if found:
                if self._save_local_tickets(tickets) is False:
                    return False
                try:
                    cfg = self.get_config()
                    if target_ticket and self.is_cloud_authenticated and cfg.get("use_firebase") and cfg.get("firebase_database_url"):
                        self._patch_to_firebase(ticket_id, target_ticket)
                except Exception:
                    pass
                return True
            return False
        except Exception:
            return False

    def delete_ticket(self, ticket_id: str) -> bool:
        """Delete an issue ticket by ID."""
        try:
            tickets = self._read_local_tickets()
            initial_len = len(tickets)
            tickets = [t for t in tickets if t.get("id") != ticket_id]

            if len(tickets) != initial_len:
                if self._save_local_tickets(tickets) is False:
                    return False
                try:
                    cfg = self.get_config()
                    if self.is_cloud_authenticated and cfg.get("use_firebase") and cfg.get("firebase_database_url"):
                        self._delete_from_firebase(ticket_id)
                except Exception:
                    pass
                return True
            return False
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Local & Remote Synchronization Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _ticket_cache_path(self) -> Path:
        """Keep cloud fallback data isolated by verified identity and provider."""
        if self.is_cloud_authenticated:
            cfg = self.get_config()
            database = _firebase_database_url(cfg.get("firebase_database_url", "")).lower()
            identity = "\n".join((database, cfg.get("firebase_project_id", ""), self._uid))
            partition = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            return self._storage_dir / (".owner_cloud_tickets_" + partition + ".json")
        return self._tickets_file

    def _read_local_tickets(self) -> List[Dict[str, Any]]:
        try:
            cache_file = self._ticket_cache_path()
            if cache_file.exists():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _save_local_tickets(self, tickets: List[Dict[str, Any]]) -> bool:
        try:
            cache_file = self._ticket_cache_path()
            temporary = cache_file.with_name(cache_file.name + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(json.dumps(tickets, indent=2, ensure_ascii=False), encoding="utf-8")
            if sys.platform != "win32":
                temporary.chmod(0o600)
            ensure_file_writable(cache_file)
            temporary.replace(cache_file)
            hide_hidden_attribute(cache_file)
            return True
        except Exception:
            return False
        finally:
            if "temporary" in locals():
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _sort_tickets(self, tickets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort tickets with Open & Critical tickets at the top."""
        try:
            status_weight = {"Open": 0, "In Progress": 1, "Resolved": 2, "Closed": 3}
            severity_weight = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}

            def _key(t: Dict[str, Any]):
                st = status_weight.get(t.get("status", "Open"), 1)
                sev = severity_weight.get(t.get("severity", "Medium"), 2)
                created = t.get("created_at", "")
                return (st, sev, -time.mktime(datetime.strptime(created, "%Y-%m-%d %H:%M:%S").timetuple()) if created else 0)

            return sorted(tickets, key=_key)
        except Exception:
            return tickets

    def _push_to_firebase(self, ticket: Dict[str, Any]) -> None:
        try:
            cfg = self.get_config()
            candidates: List[str] = []
            configured_root = cfg.get("firebase_root_collection", _DEFAULT_ROOT_COLLECTION)
            for r in [self._active_root_collection, configured_root, "owner", "users"]:
                if r and r not in candidates:
                    candidates.append(r)

            for root in candidates:
                url = self._get_firebase_tickets_url(ticket["id"], root_collection=root)
                if not url:
                    continue
                req = urllib.request.Request(
                    url,
                    data=json.dumps(ticket).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )
                try:
                    with _open_firebase_request(req, timeout=4.0) as resp:
                        if resp.status in (200, 204):
                            self._active_root_collection = root
                            return
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403, 404):
                        continue
                    break
                except Exception:
                    break
        except Exception:
            pass

    def _patch_to_firebase(self, ticket_id: str, updates: Dict[str, Any]) -> None:
        try:
            cfg = self.get_config()
            candidates: List[str] = []
            configured_root = cfg.get("firebase_root_collection", _DEFAULT_ROOT_COLLECTION)
            for r in [self._active_root_collection, configured_root, "owner", "users"]:
                if r and r not in candidates:
                    candidates.append(r)

            for root in candidates:
                url = self._get_firebase_tickets_url(ticket_id, root_collection=root)
                if not url:
                    continue
                req = urllib.request.Request(
                    url,
                    data=json.dumps(updates).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="PATCH",
                )
                try:
                    with _open_firebase_request(req, timeout=4.0) as resp:
                        if resp.status in (200, 204):
                            self._active_root_collection = root
                            return
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403, 404):
                        continue
                    break
                except Exception:
                    break
        except Exception:
            pass

    def _delete_from_firebase(self, ticket_id: str) -> None:
        try:
            cfg = self.get_config()
            candidates: List[str] = []
            configured_root = cfg.get("firebase_root_collection", _DEFAULT_ROOT_COLLECTION)
            for r in [self._active_root_collection, configured_root, "owner", "users"]:
                if r and r not in candidates:
                    candidates.append(r)

            for root in candidates:
                url = self._get_firebase_tickets_url(ticket_id, root_collection=root)
                if not url:
                    continue
                req = urllib.request.Request(url, method="DELETE")
                try:
                    with _open_firebase_request(req, timeout=4.0) as resp:
                        if resp.status in (200, 204):
                            self._active_root_collection = root
                            return
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403, 404):
                        continue
                    break
                except Exception:
                    break
        except Exception:
            pass

    def test_firebase_connection(self) -> tuple[bool, str]:
        """Probe the fixed endpoint without credentials or database content."""
        try:
            url = firebase_configuration()["firebase_database_url"] + "/.json?shallow=true"
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "MCUFlasher-Test/1.0"})
            try:
                with _open_firebase_request(req, timeout=4.0):
                    pass
            except urllib.error.HTTPError as error:
                error.close()
                if error.code not in (401, 403, 405):
                    return False, "The cloud service is unavailable. Try again."
            return True, "Connected. Sign in to continue."
        except Exception as error:
            return False, _cloud_request_error(error)

    def check_connection(self) -> tuple[bool, str]:
        """Probe Firebase independently of package Offline Mode and local settings."""
        return self.test_firebase_connection()


class TicketChangeWatcher:
    """Tells the portal when this account's tickets change in another client.

    It opens the Realtime Database event stream for the signed-in account's tickets
    (``Accept: text/event-stream``), where Firebase pushes ``put``/``patch`` events
    for every write, a keep-alive every 30 seconds, ``auth_revoked`` when the hour-long
    ID token ends and ``cancel`` when access is refused. If the server will not stream
    to this exact host (a redirect is refused, so a token is never forwarded), it
    compares a digest of the tickets every 30 seconds instead. Lines are read in
    bounded pieces and nothing in the stream is interpreted beyond its event name;
    the portal re-reads tickets through the ordinary request path.

    Runs on its own daemon thread. ``on_change`` and ``on_end`` run on that thread and
    must only hand off to the GUI thread. ``lock`` is the portal's service lock.
    """

    def __init__(self, service: "OwnerTicketService", lock, on_change, on_end=None):
        self._service, self._lock = service, lock
        self._on_change, self._on_end = on_change, on_end
        self._stop = threading.Event()
        self._response = None
        self._response_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.mode = "starting"

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="developer-ticket-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._response_lock:
            response = self._response
        if response is not None:
            self._interrupt(response)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @staticmethod
    def _interrupt(response) -> None:
        # A blocked read only returns when its socket closes under it.
        try:
            raw = getattr(getattr(response, "fp", None), "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            response.close()
        except Exception:
            pass

    def _locked(self, function):
        while not self._stop.is_set():
            if self._lock.acquire(timeout=0.5):
                try:
                    return function()
                finally:
                    self._lock.release()
        return None

    def _changed(self) -> None:
        if not self._stop.is_set():
            try:
                self._on_change()
            except Exception:
                pass

    def _end(self, reason: str) -> None:
        self.mode = "ended"
        if not self._stop.is_set() and self._on_end is not None:
            try:
                self._on_end(reason)
            except Exception:
                pass

    def _run(self) -> None:
        backoff, polling, digest = 1.0, False, None
        while not self._stop.is_set():
            url = self._locked(self._service.ticket_feed_url)
            if self._stop.is_set():
                return
            if not url:
                self._end("signed-out")
                return
            try:
                if polling:
                    self.mode = "polling"
                    current = self._digest(url)
                    if digest is not None and current != digest:
                        self._changed()
                    digest, backoff = current, 1.0
                    self._stop.wait(_POLL_INTERVAL)
                    continue
                outcome = self._stream(url)
                if outcome == "streamed":
                    backoff = 1.0
            except urllib.error.HTTPError as error:
                code = error.code
                error.close()
                outcome = ("auth" if code in (401, 403)
                           else "fallback" if 300 <= code < 400 or code in (400, 404, 405, 406, 501)
                           else "retry")
            except Exception:
                outcome = "retry"
            if self._stop.is_set():
                return
            if outcome == "auth":
                renewed = self._locked(self._service.refresh_id_token)
                if not renewed or not renewed[0]:
                    self._end("session")
                    return
                continue
            if outcome == "cancel":
                self._end("denied")
                return
            if outcome == "fallback":
                polling = True
                continue
            if outcome == "streamed":
                continue  # The server closed a healthy stream; reconnect at once.
            self.mode = "reconnecting"
            self._stop.wait(backoff)
            backoff = min(60.0, backoff * 2)

    def _digest(self, url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "MCUFlasher-OwnerApp/1.0"})
        with _open_firebase_request(request, timeout=10.0) as response:
            raw = response.read(_TICKET_RESPONSE_BYTES + 1)
        return hashlib.sha256(raw).hexdigest()

    def _stream(self, url: str) -> str:
        request = urllib.request.Request(url, headers={"Accept": "text/event-stream",
                                                       "User-Agent": "MCUFlasher-OwnerApp/1.0"})
        response = _open_firebase_request(request, timeout=_STREAM_READ_TIMEOUT)
        with self._response_lock:
            self._response = response
        try:
            if self._stop.is_set():
                return "stopped"
            if "text/event-stream" not in (response.headers.get("Content-Type") or ""):
                return "fallback"
            self.mode = "live"
            event, continuing, received = "", False, False
            while not self._stop.is_set():
                chunk = response.readline(_STREAM_LINE_LIMIT)
                if not chunk:
                    return "streamed" if received else "retry"
                complete = chunk.endswith(b"\n")
                if continuing:  # The rest of an over-long data line: never kept.
                    continuing = not complete
                    continue
                continuing = not complete
                line = chunk.rstrip(b"\r\n")
                if line.startswith(b"event:"):
                    event = line[6:].strip().decode("ascii", "replace")
                elif not line and event:
                    received = True
                    if event in ("put", "patch"):
                        self._changed()
                    elif event == "auth_revoked":
                        return "auth"
                    elif event == "cancel":
                        return "cancel"
                    event = ""
            return "stopped"
        finally:
            with self._response_lock:
                self._response = None
            try:
                response.close()
            except Exception:
                pass
