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
from pathlib import Path
from datetime import datetime
from typing import Optional, Any, List, Dict

from main.core.constants import SCRIPT_DIR
from main.core.file_utils import (
    hide_hidden_attribute,
    ensure_file_writable,
)

_TICKETS_FILE_NAME = ".owner_tickets.json"
_CONFIG_FILE_NAME  = ".owner_config.json"
_VAULT_FILE_NAME   = "dbs_cloud_sync.dat"
_DEFAULT_OWNER_KEY = "owner"  # Default developer master unlock key for skeleton mode

# Cryptographic derivation constants for tracked repo vault (src/dbs/dbs_cloud_sync.dat)
_SECRET_SEED = b"MCU_FLASHER_BY_NAPH_SECURE_VAULT_KEY_2026_X79"
_SALT        = b"mcu_c46e3_rtdb_salt_v1"

# Default Firebase Realtime Database configuration (Base endpoints)
_DEFAULT_RTDB_URL        = "https://mcu-flasher-c46e3-default-rtdb.asia-southeast1.firebasedatabase.app/"
_DEFAULT_PROJECT_ID      = "mcu-flasher-c46e3"
_DEFAULT_API_KEY         = ""
_DEFAULT_ROOT_COLLECTION = "owner"


def _derive_vault_keys() -> tuple[bytes, bytes]:
    """Derive 256-bit encryption key and 256-bit MAC key using PBKDF2."""
    master = hashlib.pbkdf2_hmac("sha256", _SECRET_SEED, _SALT, 100000, dklen=64)
    return master[:32], master[32:]


def encrypt_vault_payload(data: Dict[str, Any]) -> bytes:
    """Encrypt confidential dictionary payload into authentic binary vault with HMAC-SHA256 authentication."""
    enc_key, mac_key = _derive_vault_keys()
    plaintext = json.dumps(data).encode("utf-8")
    nonce = secrets.token_bytes(16)

    keystream = bytearray()
    counter = 0
    while len(keystream) < len(plaintext):
        counter_bytes = counter.to_bytes(4, "big")
        keystream.extend(hmac.new(enc_key, nonce + counter_bytes, hashlib.sha256).digest())
        counter += 1

    ciphertext = bytes(p ^ k for p, k in zip(plaintext, keystream[:len(plaintext)]))
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return b"MCUV" + nonce + tag + ciphertext


def decrypt_vault_payload(raw: bytes) -> Dict[str, Any]:
    """Decrypt binary vault payload and verify cryptographic integrity."""
    if not raw.startswith(b"MCUV") or len(raw) < 4 + 16 + 32:
        raise ValueError("Invalid vault header")
    nonce = raw[4:20]
    expected_tag = raw[20:52]
    ciphertext = raw[52:]

    enc_key, mac_key = _derive_vault_keys()
    actual_tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_tag, actual_tag):
        raise ValueError("Integrity verification failed")

    keystream = bytearray()
    counter = 0
    while len(keystream) < len(ciphertext):
        counter_bytes = counter.to_bytes(4, "big")
        keystream.extend(hmac.new(enc_key, nonce + counter_bytes, hashlib.sha256).digest())
        counter += 1

    plaintext = bytes(c ^ k for c, k in zip(ciphertext, keystream[:len(ciphertext)]))
    return json.loads(plaintext.decode("utf-8"))


def get_vault_file_path() -> Path:
    """Resolve path to tracked encrypted cloud vault in src/dbs/ or project root."""
    try:
        candidates = [
            Path(__file__).resolve().parent.parent.parent / "src" / "dbs" / _VAULT_FILE_NAME,
            SCRIPT_DIR / "src" / "dbs" / _VAULT_FILE_NAME,
            SCRIPT_DIR / _VAULT_FILE_NAME,
        ]
        for p in candidates:
            if p.exists() and p.is_file():
                return p
        return candidates[0]
    except Exception:
        return Path("src/dbs") / _VAULT_FILE_NAME


def load_encrypted_vault() -> Dict[str, Any]:
    """Load and decrypt confidential Firebase configuration from tracked repository vault."""
    try:
        vp = get_vault_file_path()
        if vp.exists() and vp.is_file():
            raw = vp.read_bytes()
            return decrypt_vault_payload(raw)
    except Exception:
        pass
    return {}


def is_internet_available(timeout: float = 0.5) -> bool:
    """Quickly check whether an active internet connection is available.

    Must never throw exceptions and must never block for long.
    Returns False immediately if offline or on any connection failure.
    """
    try:
        import socket
        for host in ("8.8.8.8", "1.1.1.1"):
            try:
                with socket.create_connection((host, 53), timeout=timeout):
                    return True
            except (socket.timeout, OSError, Exception):
                continue
    except Exception:
        pass
    return False


def get_owner_portal_storage_dir() -> Path:
    """Return the private, isolated directory for owner portal settings and offline cache.

    Completely separated from the local notification database (src/dbs/dbs_notif.json).
    Uses %LOCALAPPDATA%/MCUFlasher/.owner_portal or ~/.mcu_flasher/.owner_portal.
    """
    try:
        local_app_data = os.environ.get("LOCALAPPDATA")
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

    def __init__(self, storage_dir: Optional[Path | str] = None, project_root: Optional[Path | str] = None):
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
                    "owner_password_hash": hashlib.sha256(_DEFAULT_OWNER_KEY.encode()).hexdigest(),
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

    def get_config(self) -> Dict[str, Any]:
        try:
            # 1. Load from tracked git encrypted cloud vault (src/dbs/dbs_cloud_sync.dat)
            vault = load_encrypted_vault()

            # 2. Local config overrides (if any)
            cfg = {}
            if self._config_file.exists():
                try:
                    cfg = json.loads(self._config_file.read_text(encoding="utf-8"))
                except Exception:
                    cfg = {}

            # Priority: explicit local override > os.environ > encrypted vault > defaults
            api_key  = cfg.get("firebase_api_key") or os.environ.get("FIREBASE_API_KEY") or vault.get("firebase_api_key") or _DEFAULT_API_KEY
            db_url   = cfg.get("firebase_database_url") or os.environ.get("FIREBASE_DATABASE_URL") or vault.get("firebase_database_url") or _DEFAULT_RTDB_URL
            proj_id  = cfg.get("firebase_project_id") or os.environ.get("FIREBASE_PROJECT_ID") or vault.get("firebase_project_id") or _DEFAULT_PROJECT_ID
            uid      = cfg.get("firebase_user_uid") or os.environ.get("FIREBASE_USER_UID") or vault.get("firebase_user_uid") or ""
            email    = cfg.get("owner_email") or os.environ.get("OWNER_EMAIL") or vault.get("owner_email") or ""
            root_col = cfg.get("firebase_root_collection") or os.environ.get("FIREBASE_ROOT_COLLECTION") or vault.get("firebase_root_collection") or _DEFAULT_ROOT_COLLECTION

            cfg["firebase_api_key"] = api_key
            cfg["firebase_database_url"] = db_url
            cfg["firebase_project_id"] = proj_id
            cfg["firebase_user_uid"] = uid
            cfg["owner_email"] = email
            cfg["firebase_root_collection"] = root_col
            if "use_firebase" not in cfg:
                cfg["use_firebase"] = True
            return cfg
        except Exception:
            pass
        return {
            "firebase_api_key": os.environ.get("FIREBASE_API_KEY", _DEFAULT_API_KEY),
            "firebase_database_url": os.environ.get("FIREBASE_DATABASE_URL", _DEFAULT_RTDB_URL),
            "firebase_project_id": os.environ.get("FIREBASE_PROJECT_ID", _DEFAULT_PROJECT_ID),
            "firebase_user_uid": os.environ.get("FIREBASE_USER_UID", ""),
            "firebase_root_collection": os.environ.get("FIREBASE_ROOT_COLLECTION", _DEFAULT_ROOT_COLLECTION),
            "use_firebase": True,
            "owner_email": os.environ.get("OWNER_EMAIL", ""),
        }

    def update_vault(self, updates: Dict[str, Any]) -> bool:
        """Update and re-encrypt the tracked git vault."""
        try:
            vault = load_encrypted_vault()
            vault.update(updates)
            vp = get_vault_file_path()
            ensure_file_writable(vp)
            vp.write_bytes(encrypt_vault_payload(vault))
            return True
        except Exception:
            return False

    def save_config(self, new_cfg: Dict[str, Any]) -> bool:
        try:
            cfg = {}
            if self._config_file.exists():
                try:
                    cfg = json.loads(self._config_file.read_text(encoding="utf-8"))
                except Exception:
                    cfg = {}
            cfg.update(new_cfg)
            ensure_file_writable(self._config_file)
            self._config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            hide_hidden_attribute(self._config_file)
            return True
        except Exception:
            return False

    def is_firebase_configured(self) -> bool:
        try:
            cfg = self.get_config()
            return bool(cfg.get("firebase_api_key") and cfg.get("firebase_database_url"))
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Authentication (Master Key & Firebase Auth REST API)
    # ─────────────────────────────────────────────────────────────────────────

    def authenticate(self, email_or_user: str, password: str) -> tuple[bool, str]:
        """Authenticate with master developer key or Firebase Auth REST API."""
        try:
            email_clean = (email_or_user or "").strip()
            pwd_clean = (password or "").strip()

            if not pwd_clean:
                return False, "Password cannot be blank."

            cfg = self.get_config()

            # 1. Master developer unlock key check ("owner" or stored local hash)
            entered_hash = hashlib.sha256(pwd_clean.encode()).hexdigest()
            stored_hash = cfg.get("owner_password_hash") or hashlib.sha256(_DEFAULT_OWNER_KEY.encode()).hexdigest()

            if pwd_clean == _DEFAULT_OWNER_KEY or entered_hash == stored_hash:
                self._is_authenticated = True
                self._user_email = email_clean or cfg.get("owner_email", "developer@mcuflasher.local")
                self._uid = cfg.get("firebase_user_uid", "")
                return True, "Authenticated (Developer Master Key)."

            # 2. Firebase Cloud Authentication via REST API
            if cfg.get("use_firebase") and cfg.get("firebase_api_key"):
                if not is_internet_available(timeout=0.6):
                    return False, "No internet connection available."

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
                    with urllib.request.urlopen(req, timeout=5.0) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        self._id_token = data.get("idToken")
                        self._refresh_token = data.get("refreshToken")
                        self._uid = data.get("localId") or data.get("uid") or cfg.get("firebase_user_uid")
                        self._user_email = data.get("email", email_clean)
                        self._is_authenticated = True
                        return True, "Firebase Cloud authentication successful."
                except urllib.error.HTTPError as e:
                    try:
                        err_json = json.loads(e.read().decode("utf-8"))
                        err_msg = err_json.get("error", {}).get("message", "Authentication failed.")
                        return False, f"Firebase error: {err_msg}"
                    except Exception:
                        return False, f"Firebase HTTP error: {e.code}"
                except Exception as e:
                    return False, f"Connection error: {e}"

            return False, "Invalid credentials. Access denied."
        except Exception as e:
            return False, f"Authentication error: {e}"

    def send_password_reset_email(self, email: Optional[str] = None) -> tuple[bool, str]:
        """Send a password reset email via Firebase Identity Toolkit."""
        try:
            cfg = self.get_config()
            api_key = cfg.get("firebase_api_key", _DEFAULT_API_KEY)
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
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return True, f"Password reset email sent to {target_email}."
        except urllib.error.HTTPError as e:
            try:
                err_json = json.loads(e.read().decode("utf-8"))
                return False, err_json.get("error", {}).get("message", f"HTTP {e.code}")
            except Exception:
                return False, f"HTTP error {e.code}"
        except Exception as e:
            return False, f"Failed to send reset email: {e}"


    def logout(self) -> None:
        try:
            self._is_authenticated = False
            self._id_token = None
            self._refresh_token = None
            self._uid = None
            self._user_email = None
        except Exception:
            pass

    @property
    def is_authenticated(self) -> bool:
        return self._is_authenticated

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
            cfg = self.get_config()
            db_url = (cfg.get("firebase_database_url") or "").rstrip("/")
            if not db_url:
                return None
            auth_param = f"?auth={self._id_token}" if self._id_token else ""
            uid = self._uid or cfg.get("firebase_user_uid")
            root = (root_collection or cfg.get("firebase_root_collection") or _DEFAULT_ROOT_COLLECTION).strip().strip("/")
            if uid:
                base_path = f"{db_url}/{root}/{uid}/tickets"
            else:
                base_path = f"{db_url}/{root}/tickets"

            if ticket_id:
                return f"{base_path}/{ticket_id}.json{auth_param}"
            return f"{base_path}.json{auth_param}"
        except Exception:
            return None

    def get_tickets(self) -> List[Dict[str, Any]]:
        """Retrieve list of issue tickets from Firebase (if online) or local storage."""
        try:
            cfg = self.get_config()

            # Try Firebase Realtime Database REST API only if configured, authenticated, AND online
            if self._is_authenticated and cfg.get("use_firebase") and cfg.get("firebase_database_url"):
                if is_internet_available(timeout=0.5):
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
                            with urllib.request.urlopen(req, timeout=4.0) as resp:
                                raw = json.loads(resp.read().decode("utf-8"))
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
                self._save_local_tickets(tickets)
            except Exception:
                pass

            # 2. Sync to Firebase if configured and online
            try:
                cfg = self.get_config()
                if cfg.get("use_firebase") and cfg.get("firebase_database_url"):
                    if is_internet_available(timeout=0.5):
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
                self._save_local_tickets(tickets)
                try:
                    cfg = self.get_config()
                    if target_ticket and cfg.get("use_firebase") and cfg.get("firebase_database_url"):
                        if is_internet_available(timeout=0.5):
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
                self._save_local_tickets(tickets)
                try:
                    cfg = self.get_config()
                    if cfg.get("use_firebase") and cfg.get("firebase_database_url"):
                        if is_internet_available(timeout=0.5):
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

    def _read_local_tickets(self) -> List[Dict[str, Any]]:
        try:
            if self._tickets_file.exists():
                data = json.loads(self._tickets_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _save_local_tickets(self, tickets: List[Dict[str, Any]]) -> None:
        try:
            ensure_file_writable(self._tickets_file)
            self._tickets_file.write_text(json.dumps(tickets, indent=2, ensure_ascii=False), encoding="utf-8")
            hide_hidden_attribute(self._tickets_file)
        except Exception:
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
                    with urllib.request.urlopen(req, timeout=4.0) as resp:
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
                    with urllib.request.urlopen(req, timeout=4.0) as resp:
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
                    with urllib.request.urlopen(req, timeout=4.0) as resp:
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
        """Verify Firebase Realtime Database endpoint accessibility."""
        try:
            if not is_internet_available(timeout=0.6):
                return False, "No internet connection detected."

            clean_url = db_url.strip().rstrip("/")
            if not clean_url.startswith("http"):
                clean_url = "https://" + clean_url
            if not clean_url.endswith(".firebaseio.com") and "firebasedatabase.app" not in clean_url:
                return False, "URL should be a valid Firebase Realtime Database URL."

            url = f"{clean_url}/.json?shallow=true"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MCUFlasher-Test/1.0"})
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    if resp.status in (200, 401, 403):
                        return True, "Successfully reached Firebase Realtime Database!"
                    return False, f"Unexpected response status: {resp.status}"
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    return True, "Firebase endpoint reachable (Security rules active & protected)."
                return False, f"HTTP error {e.code}: {e.reason}"
            except Exception as e:
                return False, f"Connection failed: {e}"
        except Exception as e:
            return False, f"Test failed: {e}"
