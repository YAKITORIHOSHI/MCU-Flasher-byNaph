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
import urllib.request
import urllib.error
from urllib.parse import quote, urlencode, urlsplit
from pathlib import Path
from datetime import datetime
from typing import Optional, Any, List, Dict

from main.core.file_utils import (
    hide_hidden_attribute,
    ensure_file_writable,
)
from src.modules.offline_runtime import firebase_network_access
from main.core.credential_store import (
    CredentialStore, load_cloud_configuration, save_cloud_configuration,
)

_TICKETS_FILE_NAME = ".owner_tickets.json"
_CONFIG_FILE_NAME  = ".owner_config.json"
_DEFAULT_ROOT_COLLECTION = "owner"
_LOCAL_KEY_ITERATIONS = 600_000
_PROVIDER_KEYS = {"firebase_api_key", "firebase_database_url", "firebase_project_id"}
_OWNER_METADATA_KEYS = {"owner_email", "firebase_user_uid"}
_AUTH_RESPONSE_BYTES = 64 * 1024
_TICKET_RESPONSE_BYTES = 4 * 1024 * 1024

class _NoFirebaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _open_firebase_request(request, *, timeout):
    endpoint = urlsplit(request.full_url)
    if (endpoint.scheme != "https" or endpoint.username or endpoint.password
            or endpoint.port not in (None, 443)):
        raise ValueError("Use a secure Firebase connection.")
    with firebase_network_access(endpoint.hostname):
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
    if "://" not in clean_url:
        clean_url = "https://" + clean_url
    endpoint = urlsplit(clean_url)
    host = endpoint.hostname or ""
    if (endpoint.scheme != "https" or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or endpoint.path not in ("", "/")
            or endpoint.port not in (None, 443)
            or not host.endswith((".firebaseio.com", ".firebasedatabase.app"))):
        raise ValueError("Use an HTTPS Firebase Realtime Database URL.")
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

    and pluggable Firebase Realtime Database & Auth REST endpoints.
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
                    "firebase_api_key": "",
                    "firebase_database_url": "",
                    "firebase_project_id": "",
                    "firebase_root_collection": _DEFAULT_ROOT_COLLECTION,
                    "use_firebase": True,
                    "local_access": {},
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
        """Read OS-protected provider settings; never consult the application tree."""
        cfg = self._read_config()
        # Upgrade older per-user plaintext settings only after verified secure
        # persistence. Failures leave the original settings intact for recovery.
        provider = {key: cfg[key] for key in _PROVIDER_KEYS if cfg.get(key)}
        metadata = {key: cfg[key] for key in _OWNER_METADATA_KEYS if cfg.get(key)}
        if provider or metadata:
            try:
                if provider:
                    save_cloud_configuration(provider, self._credential_store)
                if metadata:
                    saved = self._credential_store.get("owner_metadata") or {}
                    saved.update(metadata)
                    self._credential_store.set("owner_metadata", saved)
                    if self._credential_store.get("owner_metadata") != saved:
                        raise ValueError("Secure save verification failed")
                cleaned = {key: value for key, value in cfg.items()
                           if key not in _PROVIDER_KEYS | _OWNER_METADATA_KEYS | {"owner_password_hash"}}
                if self._write_config(cleaned):
                    cfg = cleaned
            except Exception:
                pass
        elif "owner_password_hash" in cfg:
            cleaned = {key: value for key, value in cfg.items() if key != "owner_password_hash"}
            if self._write_config(cleaned):
                cfg = cleaned
        try:
            private_metadata = self._credential_store.get("owner_metadata") or {}
        except Exception:
            private_metadata = {}
        secure = load_cloud_configuration(self._credential_store)
        # An unavailable keyring must not revive a legacy plaintext API key.
        cfg.update(secure)
        cfg.update({key: os.environ.get(key.upper()) or private_metadata.get(key, "")
                    for key in _OWNER_METADATA_KEYS})
        cfg["firebase_root_collection"] = cfg.get("firebase_root_collection") or _DEFAULT_ROOT_COLLECTION
        cfg.setdefault("use_firebase", True)
        cfg["local_access_configured"] = self._local_access_valid(cfg.get("local_access"))
        return cfg

    def update_vault(self, updates: Dict[str, Any]) -> bool:
        """Compatibility entry point: save to the current user's OS vault."""
        return self.save_config(updates)

    def save_config(self, new_cfg: Dict[str, Any]) -> bool:
        try:
            cfg = self._read_config()
            provider = {key: cfg[key] for key in _PROVIDER_KEYS if cfg.get(key)}
            provider.update({key: value for key, value in new_cfg.items() if key in _PROVIDER_KEYS})
            if provider:
                save_cloud_configuration(provider, self._credential_store)
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
                   if key not in _PROVIDER_KEYS | _OWNER_METADATA_KEYS | {"owner_password_hash", "local_access_configured"}}
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

    def authenticate(self, email_or_user: str, password: str) -> tuple[bool, str]:
        """Authenticate with the configured local key or Firebase Auth REST API."""
        try:
            self.logout()
            email_clean = (email_or_user or "").strip()
            pwd_clean = password or ""

            if not pwd_clean.strip():
                return False, "Password cannot be blank."

            cfg = self.get_config()

            record = cfg.get("local_access")
            if self._local_access_valid(record):
                entered = hashlib.pbkdf2_hmac("sha256", pwd_clean.encode("utf-8"),
                    bytes.fromhex(record["salt"]), int(record["iterations"]))
                if hmac.compare_digest(entered, bytes.fromhex(record["hash"])):
                    self._is_authenticated = True
                    self._user_email = email_clean or "Local developer"
                    # A local key never supplies a cloud UID or token.
                    return True, "Signed in to local developer tickets."

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
                        return True, "Firebase Cloud authentication successful."
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

    def test_firebase_connection(self, api_key: str, db_url: str) -> tuple[bool, str]:
        """Check the endpoint without sending credentials or reading database data."""
        try:
            try:
                clean_url = _firebase_database_url(db_url)
            except ValueError:
                return False, "The cloud connection needs a valid database address."

            url = f"{clean_url}/.json?shallow=true"
            try:
                req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "MCUFlasher-Test/1.0"})
                with _open_firebase_request(req, timeout=4.0) as resp:
                    return True, "Connected. Sign in to continue."
            except urllib.error.HTTPError as e:
                e.close()
                # Some Firebase endpoints reject HEAD with 405. Their HTTP
                # response still proves connectivity; sign-in verifies access.
                return True, "Connected. Sign in to continue."
            except Exception as e:
                return False, _cloud_request_error(e)
        except Exception as e:
            return False, _cloud_request_error(e)

    def check_connection(self) -> tuple[bool, str]:
        """Probe this feature's Firebase service independently of package settings."""
        try:
            cfg = self.get_config()
            if cfg.get("firebase_database_url"):
                return self.test_firebase_connection("", cfg["firebase_database_url"])
            request = urllib.request.Request("https://identitytoolkit.googleapis.com/", method="HEAD")
            try:
                with _open_firebase_request(request, timeout=4.0):
                    pass
            except urllib.error.HTTPError as error:
                # The Auth API requires a specific route. Any HTTP response proves
                # reachability, without claiming authentication or ticket access.
                error.close()
            return True, "Connected. Set up the cloud connection to sign in."
        except Exception as error:
            return False, _cloud_request_error(error)
