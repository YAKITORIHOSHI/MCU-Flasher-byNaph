"""OS-protected cloud credentials. No passwords, tokens or keys in the checkout.

Windows uses Credential Manager (the current user's protected credential vault).
Ubuntu uses the desktop Secret Service through ``secret-tool``. Missing or locked
secure storage is an error; there is deliberately no plaintext/key-file fallback.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


class SecureStorageError(RuntimeError):
    """A safe, user-facing storage failure without platform exception details."""


def user_cloud_directory() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        if not base.is_absolute():
            base = Path.home() / "AppData" / "Local"
        return base / "MCUFlasher" / "cloud"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    if not base.is_absolute():
        base = Path.home() / ".local" / "share"
    return base / "mcu-flasher" / "cloud"


class CredentialStore:
    """Small JSON records kept exclusively in the OS credential vault."""

    SERVICE = "MCUFlasher.Cloud.v1"

    def __init__(self, *, platform: str | None = None):
        self._platform = platform or sys.platform

    @property
    def status(self) -> tuple[bool, str]:
        if self._platform == "win32":
            return True, "Windows Credential Manager protects saved credentials for this user."
        if self._platform.startswith("linux") and shutil.which("secret-tool"):
            return True, "The desktop Secret Service protects saved credentials for this user."
        return False, (
            "Secure credential saving is unavailable. On Ubuntu install libsecret-tools "
            "and unlock a desktop keyring, or sign in without saving credentials."
        )

    def _target(self, name: str) -> str:
        return self.SERVICE + ":" + hashlib.sha256(name.encode("utf-8")).hexdigest()

    def get(self, name: str) -> dict[str, Any] | None:
        raw = self._windows_read(name) if self._platform == "win32" else self._linux_read(name)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, TypeError):
            raise SecureStorageError("Saved cloud credentials are damaged. Forget them and sign in again.") from None

    def set(self, name: str, value: dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        # Credential Manager's generic credential blob limit is 5 * 512 bytes.
        if len(raw.encode("utf-8")) > 2560:
            raise SecureStorageError("This credential is too large for secure storage.")
        if self._platform == "win32":
            self._windows_write(name, raw)
        else:
            self._linux_write(name, raw)

    def delete(self, name: str) -> None:
        if self._platform == "win32":
            api, _ = self._windows_api()
            if not api.CredDeleteW(self._target(name), 1, 0) and ctypes.get_last_error() != 1168:
                raise SecureStorageError("Windows could not forget the saved cloud credential.")
        else:
            self._secret_tool("clear", name)

    def _windows_api(self):
        from ctypes import wintypes

        class Credential(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]
        try:
            api = ctypes.WinDLL("advapi32", use_last_error=True)
            api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
            api.CredWriteW.restype = wintypes.BOOL
            api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.POINTER(ctypes.POINTER(Credential))]
            api.CredReadW.restype = wintypes.BOOL
            api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
            api.CredDeleteW.restype = wintypes.BOOL
            api.CredFree.argtypes = [ctypes.c_void_p]
            api.CredFree.restype = None
            return api, Credential
        except (AttributeError, OSError):
            raise SecureStorageError("Windows Credential Manager is unavailable.") from None

    def _windows_read(self, name: str) -> str | None:
        api, Credential = self._windows_api()
        pointer = ctypes.POINTER(Credential)()
        if not api.CredReadW(self._target(name), 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:
                return None
            raise SecureStorageError("Windows could not read saved cloud credentials.")
        try:
            value = pointer.contents
            if value.CredentialBlobSize > 2560:
                raise SecureStorageError("The saved cloud credential is too large.")
            return ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize).decode("utf-8")
        except UnicodeError:
            raise SecureStorageError("The saved cloud credential is damaged.") from None
        finally:
            api.CredFree(pointer)

    def _windows_write(self, name: str, raw: str) -> None:
        api, Credential = self._windows_api()
        encoded = raw.encode("utf-8")
        blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
        credential = Credential()
        credential.Type = 1  # CRED_TYPE_GENERIC
        credential.TargetName = self._target(name)
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE; current OS user only
        credential.UserName = self.SERVICE
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise SecureStorageError("Windows could not securely save cloud credentials.")

    def _secret_tool(self, action: str, name: str, raw: str | None = None) -> str | None:
        binary = shutil.which("secret-tool") if self._platform.startswith("linux") else None
        if not binary:
            raise SecureStorageError(self.status[1])
        arguments = [binary, action]
        if action == "store":
            arguments += ["--label=MCU Flasher cloud credential"]
        arguments += ["service", self.SERVICE, "record", self._target(name)]
        try:
            result = subprocess.run(arguments, input=raw, text=True, encoding="utf-8",
                                    capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise SecureStorageError("The desktop keyring did not respond. Unlock it and try again.") from None
        if result.returncode == 1 and action in ("lookup", "clear") and not result.stderr.strip():
            return None
        if result.returncode != 0:
            raise SecureStorageError("The desktop keyring is unavailable or locked. Unlock it and try again.")
        return result.stdout.rstrip("\r\n") if action == "lookup" else None

    def _linux_read(self, name: str) -> str | None:
        return self._secret_tool("lookup", name)

    def _linux_write(self, name: str, raw: str) -> None:
        self._secret_tool("store", name, raw)


_CONFIG_KEYS = {"firebase_api_key", "firebase_database_url", "firebase_project_id"}


def load_cloud_configuration(store: CredentialStore | None = None) -> dict[str, Any]:
    """Read provider settings only from the current user's OS credential vault."""
    cfg: dict[str, Any] = {}
    try:
        cfg.update((store or CredentialStore()).get("provider_configuration") or {})
    except SecureStorageError:
        pass
    return {key: cfg.get(key, "") for key in _CONFIG_KEYS}


def save_cloud_configuration(updates: dict[str, Any], store: CredentialStore | None = None) -> bool:
    """Save and read back a provider configuration; never silently fall back."""
    vault = store or CredentialStore()
    old = vault.get("provider_configuration") or {}
    old.update({key: str(value) for key, value in updates.items() if key in _CONFIG_KEYS})
    vault.set("provider_configuration", old)
    if vault.get("provider_configuration") != old:
        raise SecureStorageError("Cloud configuration could not be verified after saving.")
    return True
