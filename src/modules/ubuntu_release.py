"""Native host admission, before runtime handoff, installers or services.

Use OS release data only. There is deliberately no environment override for
production admission; verifiers patch the reader or use isolated launch copies.
Each admitted Linux distribution is its own reviewed policy: ``ID_LIKE`` never
admits a host, and unknown, missing or malformed release data always stops here.
"""
from __future__ import annotations

from dataclasses import dataclass
import platform
import re
import sys

MINIMUM_UBUNTU = (24, 4)
MINIMUM_FEDORA = 44
# PySide6's macOS wheels are tagged macosx_14_0, so nothing older can run the GUI.
MINIMUM_MACOS = (14, 0)
# A Mint release is defined by its Ubuntu base. Debian-based LMDE has no
# UBUNTU_CODENAME and stays refused until it has its own qualification.
MINT_UBUNTU_BASES = {22: ("noble", (24, 4))}
REQUIREMENT = "MCU Flasher requires Ubuntu 24.04 or newer."
MINT_REQUIREMENT = "MCU Flasher requires Linux Mint 22.x, based on Ubuntu 24.04."
FEDORA_REQUIREMENT = "MCU Flasher requires Fedora 44 or newer."
MACOS_REQUIREMENT = "MCU Flasher requires macOS 14 or newer."
ANY_LINUX_REQUIREMENT = "MCU Flasher requires Ubuntu 24.04 or newer, Linux Mint 22.x or Fedora 44 or newer."


@dataclass(frozen=True)
class LinuxDistribution:
    family: str
    label: str
    version: tuple[int, ...]
    package_manager: str
    # The Ubuntu release whose packages this host uses; Fedora has none.
    ubuntu_base: tuple[int, ...] | None = None


def _numeric_version(value, pattern) -> tuple[int, ...] | None:
    # Bound conversion and avoid treating lexical order or Python ABI as release.
    if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(pattern, value):
        return None
    return tuple(int(part) for part in value.split("."))


def linux_distribution() -> LinuxDistribution:
    """Return this host's reviewed policy, or raise for anything unrecognized."""
    try:
        release = platform.freedesktop_os_release()
    except (OSError, ValueError, AttributeError) as error:
        raise RuntimeError("Cannot read the installed Linux release. " + ANY_LINUX_REQUIREMENT) from error
    identity = release.get("ID") if isinstance(release, dict) else None
    if identity == "ubuntu":
        version = _numeric_version(release.get("VERSION_ID", ""), r"[0-9]{1,4}\.[0-9]{1,4}(?:\.[0-9]{1,4})?")
        if version is None:
            raise RuntimeError("The installed Ubuntu VERSION_ID is missing or malformed. " + REQUIREMENT)
        if version[:2] < MINIMUM_UBUNTU:
            raise RuntimeError(REQUIREMENT)
        return LinuxDistribution("ubuntu", "Ubuntu", version, "apt", version)
    if identity == "linuxmint":
        version = _numeric_version(release.get("VERSION_ID", ""), r"[0-9]{1,4}(?:\.[0-9]{1,4}){0,2}")
        if version is None:
            raise RuntimeError("The installed Linux Mint VERSION_ID is missing or malformed. " + MINT_REQUIREMENT)
        codename, base = MINT_UBUNTU_BASES.get(version[0], (None, None))
        if base is None or release.get("UBUNTU_CODENAME") != codename:
            raise RuntimeError(MINT_REQUIREMENT)
        return LinuxDistribution("linuxmint", "Linux Mint", version, "apt", base)
    if identity == "fedora":
        version = _numeric_version(release.get("VERSION_ID", ""), r"[0-9]{1,4}")
        if version is None:
            raise RuntimeError("The installed Fedora VERSION_ID is missing or malformed. " + FEDORA_REQUIREMENT)
        if version[0] < MINIMUM_FEDORA:
            raise RuntimeError(FEDORA_REQUIREMENT)
        return LinuxDistribution("fedora", "Fedora", version, "dnf")
    raise RuntimeError("Cannot identify this host as a supported Linux desktop. " + ANY_LINUX_REQUIREMENT)


def linux_label() -> str:
    """Name the host in Linux setup messages, without deciding admission."""
    try:
        return linux_distribution().label
    except RuntimeError:
        return "Ubuntu"


def package_manager() -> str:
    """Select the Linux adapter's package tables; admission stays a separate check."""
    try:
        return linux_distribution().package_manager
    except RuntimeError:
        return "apt"


def require_supported_macos() -> tuple[int, ...]:
    version = _numeric_version(platform.mac_ver()[0], r"[0-9]{1,4}(?:\.[0-9]{1,4}){0,2}")
    if version is None:
        raise RuntimeError("The installed macOS version is missing or malformed. " + MACOS_REQUIREMENT)
    if version[:2] < MINIMUM_MACOS:
        raise RuntimeError(MACOS_REQUIREMENT)
    return version


def require_supported_ubuntu() -> tuple[int, ...] | None:
    """Leave Windows unchanged; fail closed for unknown Linux or macOS release data.

    Returns the Ubuntu-compatible version where one exists, so a caller comparing
    Ubuntu releases treats Linux Mint as its Ubuntu base.
    """
    if sys.platform == "darwin":
        return require_supported_macos()
    if not sys.platform.startswith("linux"):
        return None
    distribution = linux_distribution()
    return distribution.ubuntu_base or distribution.version


def enforce_supported_ubuntu() -> bool:
    try:
        require_supported_ubuntu()
        return True
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return False
