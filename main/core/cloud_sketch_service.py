"""Bounded, user-isolated Firebase cloud sketches and account authentication.

All methods that touch storage or the network belong on a background worker.
Push is explicit and conditional: stale revisions never overwrite another push.
Pull has a recovery copy and only changes linked, root-level source/notes files.
"""
from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from main.core.credential_store import (
    CredentialStore, SecureStorageError, load_cloud_configuration, user_cloud_directory,
)
from src.modules.offline_runtime import OfflineDependencyError, network_access_disabled


MAX_FILES = 128
MAX_FILE_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_HISTORY_BYTES = 16 * 1024 * 1024
MAX_REVISIONS = 20
MAX_SKETCHES = 100
SOURCE_SUFFIXES = frozenset({".ino", ".cpp", ".c", ".h", ".hpp", ".txt"})
LINK_NAME = ".mcu_cloud_link.json"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_REVISION_KEY = re.compile(r"^r[1-9][0-9]{0,9}$")
_OFFLINE = ("Cloud access is blocked by Offline Mode in this session. Turn Offline Mode "
            "off in Settings and restart MCU Flasher.")


class CloudError(RuntimeError):
    """Safe user-facing error; raw HTTP exceptions and credential URLs stay private."""


class CloudConflict(CloudError):
    """The cloud version changed after this local copy was downloaded."""


class CloudRecoveryError(CloudError):
    """A failed pull left changed disk sources; old editor models must reload."""

    local_sources_changed = True

    def __init__(self, message: str, *, project_root: Path, recovery_dir: Path):
        super().__init__(message)
        self.project_root = project_root
        self.recovery_dir = recovery_dir


def _safe_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CloudError("Cloud returned an invalid account or sketch identifier.")
    return value


def _source_name(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 240:
        raise CloudError("A sketch filename is invalid or too long.")
    if (value != Path(value).name or value.startswith(".") or any(c in value for c in '/\\:\x00<>"|?*')
            or any(ord(c) < 32 for c in value) or value.endswith((".", " "))
            or Path(value).suffix.lower() not in SOURCE_SUFFIXES):
        raise CloudError("Cloud sketches support only safe root source and text-note filenames.")
    if value.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                                *(f"LPT{i}" for i in range(1, 10))}:
        raise CloudError("A sketch filename is reserved on Windows.")
    return value


def _io_path(value: Path | str) -> Path:
    """Extend Windows file-operation paths only; keep UI/Linux path identities."""
    path = Path(value)
    if sys.platform != "win32":
        return path
    native = os.path.abspath(os.fspath(path))
    if native.startswith("\\\\?\\"):
        return Path(native)
    return Path("\\\\?\\UNC\\" + native[2:] if native.startswith("\\\\") else "\\\\?\\" + native)


def read_project_link(root: Path | str) -> dict[str, Any] | None:
    path = _io_path(Path(root) / LINK_NAME)
    try:
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != 1:
            return None
        _safe_id(value.get("uid"))
        _safe_id(value.get("sketch_id"))
        if type(value.get("revision")) is not int or value["revision"] < 1:
            return None
        names = value.get("files")
        if not isinstance(names, list) or len(names) > MAX_FILES:
            return None
        for name in names:
            _source_name(name)
        if len(set(name.casefold() for name in names)) != len(names):
            return None
        if "name" in value and (not isinstance(value["name"], str) or not value["name"].strip()
                                or len(value["name"]) > 100 or any(ord(c) < 32 for c in value["name"])):
            return None
        return value
    except (OSError, ValueError, TypeError, CloudError):
        return None


def _atomic_json(path: Path, value: Any) -> None:
    path = _io_path(path)
    if path.is_symlink():
        raise CloudError("A linked cloud file cannot be a symbolic link.")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _root_snapshot(root: Path | str) -> dict[str, dict[str, str]]:
    folder = _io_path(root)
    if folder.is_symlink() or not folder.is_dir():
        raise CloudError("Select a real sketch project folder.")
    result = {}
    total = 0
    seen = set()
    try:
        # One shallow pass. Build inputs, journals, libraries and assets are excluded.
        for item in folder.iterdir():
            if item.name.startswith(".") or item.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            name = _source_name(item.name)
            if item.is_symlink() or not item.is_file():
                raise CloudError("Root sketch sources cannot be symbolic links or directories.")
            if name.casefold() in seen:
                raise CloudError("Sketch filenames must also be distinct on Windows.")
            seen.add(name.casefold())
            if len(result) >= MAX_FILES or item.stat().st_size > MAX_FILE_BYTES:
                raise CloudError("Cloud sketches allow 128 files and 1 MiB per file.")
            with item.open("rb") as stream:
                before = os.fstat(stream.fileno())
                raw = stream.read(MAX_FILE_BYTES + 1)
                after = os.fstat(stream.fileno())
            if len(raw) > MAX_FILE_BYTES or before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size:
                raise CloudError("A sketch file changed during upload. Save it and try again.")
            total += len(raw)
            if total > MAX_SOURCE_BYTES:
                raise CloudError("Cloud sketches allow up to 4 MiB of source and notes.")
            text = raw.decode("utf-8")
            key = base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")
            result[key] = {"name": name, "content": text, "sha256": hashlib.sha256(raw).hexdigest()}
    except CloudError:
        raise
    except UnicodeError:
        raise CloudError("Cloud sketch source files must use UTF-8 text encoding.") from None
    except OSError:
        raise CloudError("A sketch source file could not be read. Check folder access and try again.") from None
    if not result:
        raise CloudError("This project has no root sketch sources or text notes to upload.")
    if not any(Path(entry["name"]).suffix.lower() in {".ino", ".cpp", ".c"}
               and entry["content"].strip() for entry in result.values()):
        raise CloudError("A cloud sketch needs at least one nonempty root .ino, .cpp or .c source file.")
    return result


def _validate_snapshot(value: Any) -> dict[str, bytes]:
    if not isinstance(value, dict) or not value or len(value) > MAX_FILES:
        raise CloudError("The cloud snapshot has an invalid file list.")
    result: dict[str, bytes] = {}
    seen = set()
    total = 0
    for entry in value.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("content"), str):
            raise CloudError("The cloud snapshot contains an invalid source file.")
        name = _source_name(entry.get("name"))
        if name.casefold() in seen:
            raise CloudError("The cloud snapshot contains duplicate filenames.")
        seen.add(name.casefold())
        raw = entry["content"].encode("utf-8")
        total += len(raw)
        if len(raw) > MAX_FILE_BYTES or total > MAX_SOURCE_BYTES:
            raise CloudError("The cloud snapshot exceeds the source size limit.")
        if entry.get("sha256") != hashlib.sha256(raw).hexdigest():
            raise CloudError("Cloud source verification failed. No local files were changed.")
        result[name] = raw
    if not any(Path(name).suffix.lower() in {".ino", ".cpp", ".c"} and raw.decode("utf-8").strip()
               for name, raw in result.items()):
        raise CloudError("The cloud snapshot has no primary sketch source. No local files were changed.")
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class CloudSketchService:
    def __init__(self, *, config_provider: Callable[[], dict] | None = None,
                 credential_store: CredentialStore | None = None, transport=None,
                 data_dir: Path | str | None = None):
        self._store = credential_store or CredentialStore()
        self._config_provider = config_provider or (lambda: load_cloud_configuration(self._store))
        self._transport = transport
        self._data_dir = Path(data_dir) if data_dir is not None else user_cloud_directory()
        self._lock = threading.RLock()
        self._id_token = ""
        self._refresh_token = ""
        self._uid = ""
        self._email = ""
        self._expires_at = 0.0
        self._remember = False
        self._session_scope = ""

    def _config(self) -> dict:
        cfg = self._config_provider()
        if not isinstance(cfg, dict) or not cfg.get("firebase_api_key"):
            raise CloudError("Cloud is not configured. Add the Firebase provider configuration for this OS user.")
        database = urllib.parse.urlsplit(str(cfg.get("firebase_database_url", "")))
        hostname = database.hostname or ""
        try:
            port = database.port
        except ValueError:
            raise CloudError("Cloud needs a valid HTTPS Firebase Realtime Database URL.") from None
        if (database.scheme != "https" or database.username or database.password or port not in (None, 443)
                or database.query or database.fragment or database.path not in ("", "/")
                or not (hostname.endswith(".firebasedatabase.app") or hostname.endswith(".firebaseio.com"))):
            raise CloudError("Cloud needs an HTTPS Firebase Realtime Database URL.")
        cfg = {**cfg, "firebase_database_url": str(cfg["firebase_database_url"]).rstrip("/")}
        if self._uid and self._session_scope and self._configuration_scope(cfg) != self._session_scope:
            raise CloudError("The cloud provider changed in another window. Sign out and sign in to the configured provider.")
        return cfg

    @staticmethod
    def _configuration_scope(cfg: dict) -> str:
        # Include provider project/API identity as well as database URL so account
        # sessions cannot cross providers after shared settings change.
        identity = json.dumps([cfg["firebase_database_url"], cfg["firebase_api_key"],
                               cfg.get("firebase_project_id", "")], separators=(",", ":"))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _scope(self) -> str:
        cfg = self._config()
        return self._configuration_scope(cfg)

    @property
    def configured(self) -> bool:
        try:
            self._config()
            return True
        except (CloudError, SecureStorageError, ValueError):
            return False

    @property
    def is_authenticated(self) -> bool:
        return bool(self._uid and self._id_token)

    @property
    def account_info(self) -> dict:
        return {"uid": self._uid, "email": self._email, "remember_me": self._remember}

    @property
    def secure_storage_status(self) -> tuple[bool, str]:
        return self._store.status

    def saved_login(self) -> dict:
        with self._lock:
            try:
                value = self._store.get("login:" + self._scope()) or {}
                if isinstance(value.get("email"), str) and isinstance(value.get("password"), str):
                    return {"email": value["email"], "password": value["password"]}
                return {}
            except (SecureStorageError, CloudError):
                return {}

    def _request(self, method: str, url: str, payload=None, *, headers=None, form=False) -> tuple[Any, dict]:
        if network_access_disabled():
            raise CloudError(_OFFLINE)
        request_headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if payload is not None:
            data = (urllib.parse.urlencode(payload).encode("utf-8") if form else
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if len(data) > MAX_RESPONSE_BYTES:
                raise CloudError("The cloud request exceeds the size limit.")
            request_headers["Content-Type"] = ("application/x-www-form-urlencoded" if form else "application/json")
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            if self._transport:
                return self._transport(request, timeout=12.0, max_bytes=MAX_RESPONSE_BYTES)
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=12.0) as response:
                chunks = []
                size = 0
                started = time.monotonic()
                while True:
                    if time.monotonic() - started > 30:
                        raise CloudError("The cloud response took too long. Try again later.")
                    chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise CloudError("The cloud response exceeds the size limit.")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                return json.loads(raw.decode("utf-8")), dict(response.headers.items())
        except CloudError:
            raise
        except OfflineDependencyError:
            raise CloudError(_OFFLINE) from None
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                body = json.loads(error.read(8192).decode("utf-8"))
                detail = body.get("error", {})
                code = detail.get("message", "") if isinstance(detail, dict) else ""
                code = code.split(" : ", 1)[0]
            except (ValueError, UnicodeError, AttributeError):
                code = ""
            finally:
                error.close()
            if status == 412:
                raise CloudConflict("This cloud sketch changed. Pull the latest version before pushing again.") from None
            messages = {
                "EMAIL_EXISTS": "An account already uses this email address.",
                "INVALID_EMAIL": "Enter a valid email address.",
                "WEAK_PASSWORD": "Use a stronger password with at least six characters.",
                "INVALID_LOGIN_CREDENTIALS": "The email or password is incorrect.",
                "EMAIL_NOT_FOUND": "The email or password is incorrect.",
                "INVALID_PASSWORD": "The email or password is incorrect.",
                "USER_DISABLED": "This account has been disabled.",
                "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many sign-in attempts. Try again later.",
                "OPERATION_NOT_ALLOWED": "Email/password accounts are not enabled for this Firebase project.",
                "CREDENTIAL_TOO_OLD_LOGIN_AGAIN": "Sign in again before deleting your account.",
                "INVALID_REFRESH_TOKEN": "Your saved session expired. Sign in again.",
                "TOKEN_EXPIRED": "Your saved session expired. Sign in again.",
                "USER_NOT_FOUND": "Your saved account is unavailable. Sign in again.",
            }
            if code in messages:
                raise CloudError(messages[code]) from None
            if status in (401, 403):
                raise CloudError("Firebase denied access. Check your account and the deployed database security rules.") from None
            if status == 400:
                raise CloudError("Firebase rejected this request. Check the provider configuration and account details.") from None
            raise CloudError("The cloud service is unavailable. Try again later.") from None
        except (OSError, ValueError, TypeError):
            raise CloudError("Could not reach the cloud service. Check internet access and try again.") from None

    def _auth_request(self, operation: str, payload: dict) -> Any:
        key = urllib.parse.quote(str(self._config()["firebase_api_key"]), safe="")
        value, _ = self._request("POST", f"https://identitytoolkit.googleapis.com/v1/accounts:{operation}?key={key}", payload)
        return value

    def _session(self, value: Any, *, expected_uid: str | None = None) -> None:
        if not isinstance(value, dict):
            raise CloudError("Firebase returned an incomplete sign-in response.")
        token = value.get("idToken", value.get("id_token"))
        refresh = value.get("refreshToken", value.get("refresh_token"))
        uid = value.get("localId", value.get("user_id"))
        if not isinstance(token, str) or not token or not isinstance(refresh, str) or not refresh:
            raise CloudError("Firebase returned an incomplete sign-in response.")
        uid = _safe_id(uid)
        if expected_uid and uid != expected_uid:
            raise CloudError("Firebase returned a different account. Sign in again.")
        try:
            lifetime = max(1, min(int(value.get("expiresIn", value.get("expires_in", 3600))), 3600))
        except (ValueError, TypeError):
            raise CloudError("Firebase returned an invalid session lifetime.") from None
        self._id_token, self._refresh_token, self._uid = token, refresh, uid
        self._email = value.get("email", self._email) or self._email
        self._expires_at = time.monotonic() + lifetime

    def _clear_session(self) -> None:
        self._id_token = self._refresh_token = self._uid = self._email = ""
        self._expires_at = 0.0
        self._remember = False
        self._session_scope = ""

    def _persist_session(self) -> None:
        if self._remember:
            self._store.set("session:" + self._scope(), {
                "uid": self._uid, "email": self._email, "refresh_token": self._refresh_token,
            })

    def sign_in(self, email: str, password: str, *, save_login=False, remember_me=False) -> dict:
        with self._lock:
            self._clear_session()
            clean_email = (email or "").strip()
            if not clean_email or not isinstance(password, str) or not password:
                raise CloudError("Enter your email and password.")
            try:
                if (save_login or remember_me) and not self._store.status[0]:
                    raise SecureStorageError(self._store.status[1])
                scope = self._scope()
                # Failed sign-in cannot silently revive an earlier remembered account.
                try:
                    self._store.delete("session:" + scope)
                except SecureStorageError:
                    if remember_me:
                        raise
                value = self._auth_request("signInWithPassword", {
                    "email": clean_email, "password": password, "returnSecureToken": True,
                })
                self._email = clean_email
                self._session(value)
                self._session_scope = scope
                self._remember = bool(remember_me)
                if save_login:
                    self._store.set("login:" + scope, {"email": self._email, "password": password})
                else:
                    try:
                        self._store.delete("login:" + scope)
                    except SecureStorageError:
                        pass
                self._persist_session()
                return self.account_info
            except SecureStorageError as error:
                self._clear_session()
                raise CloudError(str(error)) from None
            except Exception:
                self._clear_session()
                raise

    def create_account(self, email: str, password: str, *, save_login=False, remember_me=False) -> dict:
        with self._lock:
            self._clear_session()
            clean_email = (email or "").strip()
            if not clean_email or not isinstance(password, str) or len(password) < 6:
                raise CloudError("Enter an email and a password with at least six characters.")
            if (save_login or remember_me) and not self._store.status[0]:
                raise CloudError(self._store.status[1])
            scope = self._scope()
            value = self._auth_request("signUp", {"email": clean_email, "password": password, "returnSecureToken": True})
            self._email = clean_email
            self._session(value)
            self._session_scope = scope
            self._remember = bool(remember_me)
            try:
                self._store.delete("session:" + scope)
                if save_login:
                    self._store.set("login:" + scope, {"email": self._email, "password": password})
                else:
                    self._store.delete("login:" + scope)
                self._persist_session()
            except SecureStorageError as error:
                if save_login or remember_me:
                    self._clear_session()
                    raise CloudError("Account created, but secure saving failed. Sign in again without saving, "
                                     "or unlock your OS keyring.") from None
            return self.account_info

    def _refresh(self) -> None:
        if not self._refresh_token or not self._uid:
            raise CloudError("Sign in to your cloud account first.")
        key = urllib.parse.quote(str(self._config()["firebase_api_key"]), safe="")
        try:
            value, _ = self._request("POST", f"https://securetoken.googleapis.com/v1/token?key={key}",
                                     {"grant_type": "refresh_token", "refresh_token": self._refresh_token}, form=True)
            self._session(value, expected_uid=self._uid)
            self._persist_session()
        except SecureStorageError as error:
            self._clear_session()
            raise CloudError(str(error)) from None
        except CloudError:
            # Do not silently re-send a request or revive a failed session.
            self._clear_session()
            raise

    def restore_session(self) -> bool:
        with self._lock:
            self._clear_session()
            try:
                scope = self._scope()
                value = self._store.get("session:" + scope)
                if not value:
                    return False
                self._uid = _safe_id(value.get("uid"))
                self._session_scope = scope
                self._email = value.get("email", "")
                self._refresh_token = value.get("refresh_token", "")
                if not isinstance(self._refresh_token, str) or not self._refresh_token:
                    raise CloudError("Your saved session is incomplete. Sign in again.")
                self._remember = True
                self._refresh()
                return True
            except SecureStorageError as error:
                self._clear_session()
                raise CloudError(str(error)) from None
            except Exception:
                self._clear_session()
                raise

    def sign_out(self, *, forget_saved=False) -> None:
        with self._lock:
            scope = self._session_scope
            if not scope:
                try:
                    scope = self._scope()
                except CloudError:
                    scope = ""
            self._clear_session()
            try:
                if scope:
                    self._store.delete("session:" + scope)
                    if forget_saved:
                        self._store.delete("login:" + scope)
            except SecureStorageError as error:
                raise CloudError(str(error)) from None

    def _database(self, method: str, path: str, payload=None, *, headers=None, query=None):
        if not self.is_authenticated:
            raise CloudError("Sign in to your cloud account first.")
        if self._expires_at <= time.monotonic() + 60:
            self._refresh()
        cfg = self._config()
        params = {"auth": self._id_token, **(query or {})}
        url = cfg["firebase_database_url"] + "/users/" + _safe_id(self._uid) + "/" + path + ".json?"
        return self._request(method, url + urllib.parse.urlencode(params), payload, headers=headers)

    def delete_account(self, password: str) -> None:
        with self._lock:
            if not self.is_authenticated or not password:
                raise CloudError("Enter your current password to delete this account.")
            uid, email = self._uid, self._email
            value = self._auth_request("signInWithPassword", {"email": email, "password": password, "returnSecureToken": True})
            self._session(value, expected_uid=uid)
            # Cloud sketches are deleted while the fresh credential still authorizes
            # access. If Auth deletion fails, report the partial outcome accurately.
            self._database("DELETE", "cloud_sketches")
            try:
                self._auth_request("delete", {"idToken": self._id_token})
            except CloudError:
                raise CloudError("Cloud sketches were deleted, but account deletion did not finish. "
                                 "Sign in and retry deleting the account.") from None
            try:
                self.sign_out(forget_saved=True)
            except CloudError:
                raise CloudError("Account deleted. The OS keyring could not forget saved credentials; "
                                 "remove MCU Flasher cloud entries from the keyring.") from None

    def _meta(self, sketch_id: str) -> dict:
        value, _ = self._database("GET", "cloud_sketches/" + _safe_id(sketch_id) + "/meta")
        if not isinstance(value, dict) or value.get("id") != sketch_id or value.get("owner_uid") != self._uid:
            raise CloudError("This cloud sketch is unavailable for your account.")
        if type(value.get("revision")) is not int or not 1 <= value["revision"] <= 1000000000:
            raise CloudError("The cloud sketch revision is invalid.")
        return value

    def list_sketches(self) -> list[dict]:
        with self._lock:
            value, _ = self._database("GET", "cloud_sketches", query={"shallow": "true"})
            if value is None:
                return []
            if not isinstance(value, dict) or len(value) > MAX_SKETCHES:
                raise CloudError("This account exceeds the supported 100 cloud sketches.")
            ids = [_safe_id(key) for key in value]
            # An owned, finite pool keeps metadata-only listing inexpensive and never
            # downloads every source/history just to show the project selector.
            with concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="cloud-list") as pool:
                rows = list(pool.map(self._meta, ids))
            return sorted(rows, key=lambda row: row.get("updated_at", 0), reverse=True)

    def _record(self, sketch_id: str) -> tuple[dict, str]:
        value, headers = self._database("GET", "cloud_sketches/" + _safe_id(sketch_id),
                                         headers={"X-Firebase-ETag": "true"})
        etag = next((str(v) for k, v in headers.items() if k.lower() == "etag"), "")
        if not etag:
            raise CloudError("Firebase did not provide a revision guard. No cloud files were changed.")
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise CloudError("This cloud sketch is unavailable or uses an unsupported format.")
        meta = value.get("meta", {})
        if not isinstance(meta, dict) or meta.get("id") != sketch_id or meta.get("owner_uid") != self._uid:
            raise CloudError("This cloud sketch is unavailable for your account.")
        revision = meta.get("revision")
        if type(revision) is not int or not 1 <= revision <= 1000000000:
            raise CloudError("The cloud sketch revision is invalid.")
        versions = value.get("versions")
        if not isinstance(versions, dict) or not versions or len(versions) > MAX_REVISIONS:
            raise CloudError("The cloud sketch version history is invalid.")
        if "r" + str(revision) not in versions:
            raise CloudError("The cloud sketch current revision is missing.")
        for key, entry in versions.items():
            if (not isinstance(key, str) or not _REVISION_KEY.fullmatch(key)
                    or not isinstance(entry, dict) or type(entry.get("revision")) is not int
                    or entry.get("revision") != int(key[1:])
                    or not isinstance(entry.get("files"), dict)):
                raise CloudError("The cloud sketch version history is invalid.")
        return value, etag

    def working_directory(self, sketch_id: str) -> Path:
        if not self._uid:
            raise CloudError("Sign in to your cloud account first.")
        return self._data_dir / "projects" / self._scope()[:16] / self._account_folder() / _safe_id(sketch_id)

    def _account_folder(self) -> str:
        # Short, deterministic names avoid exceeding traditional Windows path
        # limits even under a long user profile; full identities stay in the link.
        return hashlib.sha256(_safe_id(self._uid).encode("utf-8")).hexdigest()[:16]

    def _link(self, root: Path, meta: dict, files: list[str], *, source_revision=None) -> None:
        _atomic_json(root / LINK_NAME, {"schema": 1, "uid": self._uid, "provider": self._scope(),
                                     "sketch_id": meta["id"], "revision": meta["revision"],
                                     "source_revision": source_revision or meta["revision"], "files": files,
                                     "name": meta["name"]})

    def upload_project(self, root: Path | str, name: str | None = None) -> dict:
        with self._lock:
            sources = _root_snapshot(root)
            ids, _ = self._database("GET", "cloud_sketches", query={"shallow": "true"})
            if ids is not None and (not isinstance(ids, dict) or len(ids) >= MAX_SKETCHES):
                raise CloudError("This account already has 100 cloud sketches.")
            sketch_id = uuid.uuid4().hex
            display_name = (name or Path(root).name).strip()[:100]
            if not display_name or any(ord(char) < 32 for char in display_name):
                raise CloudError("Enter a valid sketch name.")
            now = int(time.time() * 1000)
            meta = {"id": sketch_id, "name": display_name, "owner_uid": self._uid, "revision": 1,
                    "created_at": now, "updated_at": now, "file_count": len(sources)}
            value = {"schema": 1, "meta": meta, "versions": {"r1": {"revision": 1, "created_at": now, "files": sources}}}
            self._database("PUT", "cloud_sketches/" + sketch_id, value, headers={"If-Match": "null_etag"})
            # Upload creates a remote sketch. The original local project retains
            # its identity; the selector explicitly pulls into a separate window.
            return dict(meta)

    def push_project(self, root: Path | str, sketch_id: str, expected_revision: int) -> dict:
        with self._lock:
            sketch_id = _safe_id(sketch_id)
            link = read_project_link(root)
            if (not link or link["uid"] != self._uid or link.get("provider") != self._scope()
                    or link["sketch_id"] != sketch_id):
                raise CloudError("Open or pull this cloud sketch with the current account before pushing.")
            if type(expected_revision) is not int or expected_revision != link["revision"]:
                raise CloudConflict("The local cloud revision changed. Reload its cloud status before pushing.")
            sources = _root_snapshot(root)
            record, etag = self._record(sketch_id)
            if record["meta"]["revision"] != expected_revision:
                raise CloudConflict("This cloud sketch changed. Pull the latest version before pushing again.")
            revision = expected_revision + 1
            now = int(time.time() * 1000)
            record["meta"].update(revision=revision, updated_at=now, file_count=len(sources))
            versions = record["versions"]
            versions["r" + str(revision)] = {"revision": revision, "created_at": now, "files": sources}
            ordered = sorted(versions, key=lambda key: int(key[1:]))
            while len(ordered) > MAX_REVISIONS or len(json.dumps(versions, ensure_ascii=False).encode("utf-8")) > MAX_HISTORY_BYTES:
                if len(ordered) == 1:
                    raise CloudError("This source snapshot is too large for cloud version history.")
                del versions[ordered.pop(0)]
            self._database("PUT", "cloud_sketches/" + sketch_id, record, headers={"If-Match": etag})
            try:
                self._link(Path(root), record["meta"], [entry["name"] for entry in sources.values()])
            except OSError:
                raise CloudError("Cloud push completed, but its local link could not be saved. "
                                 "Pull the sketch before pushing again.") from None
            return dict(record["meta"])

    def list_revisions(self, sketch_id: str) -> list[dict]:
        with self._lock:
            record, _ = self._record(_safe_id(sketch_id))
            rows = []
            for revision, entry in record["versions"].items():
                try:
                    number = int(revision[1:])
                    if number < 1 or not isinstance(entry, dict):
                        raise ValueError
                except (ValueError, TypeError):
                    raise CloudError("The cloud version history contains an invalid revision.") from None
                rows.append({"revision": number, "created_at": entry.get("created_at", 0),
                             "file_count": len(entry.get("files", {}))})
            return sorted(rows, key=lambda row: row["revision"], reverse=True)

    def pull_project(self, sketch_id: str, destination: Path | str | None = None,
                     revision: int | None = None, *, source_guard=None) -> Path:
        with self._lock:
            record, _ = self._record(_safe_id(sketch_id))
            wanted = revision if revision is not None else record["meta"]["revision"]
            if type(wanted) is not int or "r" + str(wanted) not in record["versions"]:
                raise CloudError("The requested cloud revision is no longer available.")
            sources = _validate_snapshot(record["versions"]["r" + str(wanted)].get("files"))
            root = Path(destination) if destination is not None else self.working_directory(sketch_id)
            io_root = _io_path(root)
            if io_root.is_symlink() or any(_io_path(parent).is_symlink() for parent in root.parents):
                raise CloudError("A cloud working folder cannot use symbolic links.")
            io_root.mkdir(parents=True, exist_ok=True)
            root_identity = io_root.stat()
            old_link = read_project_link(root)
            if (old_link and (old_link["uid"] != self._uid or old_link["sketch_id"] != sketch_id
                              or old_link.get("provider") != self._scope())):
                raise CloudError("This folder belongs to another cloud sketch or account.")
            tracked = set(old_link["files"] if old_link else [])
            for filename in sources:
                path = io_root / filename
                if path.exists() and filename not in tracked:
                    raise CloudError("Pull would overwrite an unlinked local file. Choose an empty cloud folder.")
            # Never follow links, overwrite directories, or delete an unknown file.
            touched = tracked | set(sources)
            old_files: dict[str, bytes] = {}
            for filename in touched:
                path = io_root / _source_name(filename)
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise CloudError("A linked source path is not a regular file. No local files were changed.")
                if path.exists():
                    if path.stat().st_size > MAX_FILE_BYTES:
                        raise CloudError("A local source is too large to preserve safely before pulling.")
                    old_files[filename] = path.read_bytes()
            link_path = io_root / LINK_NAME
            if link_path.exists() and old_link is None:
                raise CloudError("The local cloud link is damaged. Choose another empty folder.")
            old_link_bytes = link_path.read_bytes() if old_link else None
            backup = self._data_dir / "recovery" / self._scope()[:16] / self._account_folder() / sketch_id / uuid.uuid4().hex[:16]
            io_backup = _io_path(backup)
            io_backup.mkdir(parents=True, exist_ok=False)
            for filename, content in old_files.items():
                (io_backup / filename).write_bytes(content)
            if old_link_bytes is not None:
                (io_backup / LINK_NAME).write_bytes(old_link_bytes)
            _atomic_json(backup / "recovery.json", {"schema": 1, "source_revision": wanted,
                                                    "files": list(old_files), "created_at": int(time.time() * 1000)})
            staged: dict[str, Path] = {}
            changed: list[str] = []
            link_change_attempted = False
            guard = contextlib.ExitStack()
            if source_guard is not None:
                try:
                    guard.enter_context(source_guard([root / name for name in sorted(touched)]))
                except ValueError:
                    guard.close()
                    raise CloudError("Review pending AI changes before pulling or reverting this sketch.") from None
            try:
                for filename, content in sources.items():
                    temporary = io_root / (".mcu-cloud-" + uuid.uuid4().hex + ".tmp")
                    with temporary.open("xb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    staged[filename] = temporary
                current_identity = io_root.stat()
                if (io_root.is_symlink() or any(_io_path(parent).is_symlink() for parent in root.parents)
                        or (current_identity.st_dev, current_identity.st_ino) != (root_identity.st_dev, root_identity.st_ino)):
                    raise CloudError("The working folder changed during pull. No source files were replaced.")
                for filename in touched:
                    path = io_root / filename
                    if (path.is_symlink() or (path.exists() and not path.is_file())
                            or (filename in old_files and (not path.exists() or path.stat().st_size > MAX_FILE_BYTES
                                                         or path.read_bytes() != old_files[filename]))
                            or (filename not in old_files and path.exists())):
                        raise CloudError("A local source changed during pull. Save your changes and try again.")
                if ((old_link_bytes is not None and (not link_path.exists() or link_path.is_symlink()
                                                    or link_path.stat().st_size > 64 * 1024
                                                    or link_path.read_bytes() != old_link_bytes))
                        or (old_link_bytes is None and link_path.exists())):
                    raise CloudError("The local cloud link changed during pull. Try again after checking the project.")
                for filename, temporary in staged.items():
                    os.replace(temporary, io_root / filename)
                    changed.append(filename)
                for filename in tracked - set(sources):
                    (io_root / filename).unlink(missing_ok=True)
                    changed.append(filename)
                link_change_attempted = True
                self._link(root, record["meta"], list(sources), source_revision=wanted)
            except (OSError, CloudError) as failure:
                rollback_failed = False
                for filename in changed:
                    try:
                        target = io_root / filename
                        if filename in old_files:
                            recovery_temp = io_root / (".mcu-cloud-" + uuid.uuid4().hex + ".tmp")
                            recovery_temp.write_bytes(old_files[filename])
                            os.replace(recovery_temp, target)
                        else:
                            target.unlink(missing_ok=True)
                    except OSError:
                        rollback_failed = True
                if link_change_attempted:
                    try:
                        if old_link_bytes is not None:
                            _atomic_json(link_path, json.loads(old_link_bytes.decode("utf-8")))
                        else:
                            link_path.unlink(missing_ok=True)
                    except (OSError, ValueError, CloudError):
                        rollback_failed = True
                if rollback_failed:
                    raise CloudRecoveryError(
                        f"Pull could not finish or fully restore files. Recovery copies are in {backup}.",
                        project_root=root, recovery_dir=backup,
                    ) from None
                if not changed and isinstance(failure, CloudError):
                    raise failure
                raise CloudError("Pull could not finish. Previous linked files were restored and a recovery copy was kept.") from None
            finally:
                try:
                    for temporary in staged.values():
                        temporary.unlink(missing_ok=True)
                finally:
                    guard.close()
            return root
