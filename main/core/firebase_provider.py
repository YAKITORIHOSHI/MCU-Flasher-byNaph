"""The application's fixed, public Firebase deployment identity.

Firebase client configuration identifies an app; it does not grant database
access. Authentication and deployed server Security Rules enforce access. No
local preferences, environment variables or credential-vault records can select
a different deployment. Passwords and refresh tokens remain in the OS vault.
"""
from __future__ import annotations

from types import MappingProxyType
from urllib.parse import urlsplit


_PROVIDER = MappingProxyType({
    "firebase_api_key": "AIzaSyCYMRuv8ULKar3-BYB6t9FRn36vmLFZ-L0",
    "firebase_auth_domain": "mcu-flasher-c46e3.firebaseapp.com",
    "firebase_database_url": "https://mcu-flasher-c46e3-default-rtdb.asia-southeast1.firebasedatabase.app",
    "firebase_project_id": "mcu-flasher-c46e3",
    "firebase_storage_bucket": "mcu-flasher-c46e3.firebasestorage.app",
    "firebase_messaging_sender_id": "670589417942",
    "firebase_app_id": "1:670589417942:web:2f7873f1c6ff86979ccc77",
    "firebase_measurement_id": "G-FM51R512KL",
})


def firebase_configuration() -> dict[str, str]:
    """Return a detached copy; callers cannot mutate the built-in identity."""
    return dict(_PROVIDER)


def validate_firebase_destination(url: str) -> str:
    """Allow only this database and Firebase's two account HTTPS services.

    Validate immediately before transport so a malformed URL cannot carry an
    account token to another server. Redirects must also be disabled by callers.
    """
    try:
        endpoint = urlsplit(url)
        host = endpoint.hostname or ""
        allowed = {urlsplit(_PROVIDER["firebase_database_url"]).hostname,
                   "identitytoolkit.googleapis.com", "securetoken.googleapis.com"}
        if (endpoint.scheme != "https" or host not in allowed
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.port not in (None, 443) or endpoint.fragment
                or any(ord(char) <= 32 or ord(char) == 127 for char in url)
                or "\\" in url):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("The cloud connection must use MCU Flasher's built-in Firebase service.") from None
    return host
