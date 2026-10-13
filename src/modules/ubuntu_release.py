"""Native Ubuntu admission, before runtime handoff, installers or services.

Use OS release data only. There is deliberately no environment override for
production admission; verifiers patch the reader or use isolated launch copies.
"""
from __future__ import annotations

import platform
import re
import sys

MINIMUM_UBUNTU = (24, 4)
REQUIREMENT = "MCU Flasher requires Ubuntu 24.04 or newer."


def require_supported_ubuntu() -> tuple[int, ...] | None:
    """Leave Windows unchanged; fail closed for unknown Linux release data."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        release = platform.freedesktop_os_release()
    except (OSError, ValueError, AttributeError) as error:
        raise RuntimeError("Cannot read the installed Ubuntu release. " + REQUIREMENT) from error
    if not isinstance(release, dict) or release.get("ID") != "ubuntu":
        raise RuntimeError("Cannot identify this host as native Ubuntu. " + REQUIREMENT)
    value = release.get("VERSION_ID", "")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}(?:\.[0-9]{1,4})?", value):
        raise RuntimeError("The installed Ubuntu VERSION_ID is missing or malformed. " + REQUIREMENT)
    # Bound conversion and avoid treating lexical order or Python ABI as release.
    if len(value) > 32:
        raise RuntimeError("The installed Ubuntu VERSION_ID is malformed. " + REQUIREMENT)
    version = tuple(int(part) for part in value.split("."))
    if version[:2] < MINIMUM_UBUNTU:
        raise RuntimeError(REQUIREMENT)
    return version


def enforce_supported_ubuntu() -> bool:
    try:
        require_supported_ubuntu()
        return True
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return False
