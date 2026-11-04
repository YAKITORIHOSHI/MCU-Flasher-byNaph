"""Host capabilities shared by launchers and the GUI (no Qt imports)."""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath


def host_info() -> dict[str, str | bool]:
    system = platform.system()
    release = {}
    if system == "Linux":
        try:
            release = platform.freedesktop_os_release()
        except (OSError, AttributeError):
            pass
    distro = release.get("ID", "")
    return {
        "system": system,
        "name": release.get("PRETTY_NAME", system),
        "distribution": distro,
        "ubuntu": distro == "ubuntu",
        "ubuntu_like": distro == "ubuntu" or "ubuntu" in release.get("ID_LIKE", "").split(),
        "windows": system == "Windows",
        "linux": system == "Linux",
        "architecture": platform.machine(),
    }


def native_env_name() -> str:
    """This host's private environment folder; each OS keeps its own."""
    return ".venv-macos" if sys.platform == "darwin" else ".venv-linux"


def native_tools_name() -> str:
    """This host's app-owned CLI tool folder; receipts name the exact OS and CPU."""
    return ".macos-tools" if sys.platform == "darwin" else ".ubuntu-tools"


def repair_command() -> str:
    """The entry that repairs this host's runtime, for messages that name it."""
    if sys.platform == "darwin":
        return "bash direct/macos/run.sh --repair"
    if sys.platform.startswith("linux"):
        return "bash direct/ubuntu/run.sh --repair"
    return "direct/windows/run.vbs --repair"


def _native_xdg_base(name: str, fallback: Path) -> Path:
    """Ignore relative and copied Windows values when selecting native stores."""
    value = os.environ.get(name) or ""
    if value and PurePosixPath(value).is_absolute() and not PureWindowsPath(value).drive:
        return Path(value)
    return fallback


def app_cache_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / ".mcuflasher-app"
    return _native_xdg_base("XDG_CACHE_HOME", Path.home() / ".cache") / "mcu-flasher"


def native_platformio_dir() -> Path:
    """Separate native toolchains from portable Windows executables.

    macOS keeps this path too: PlatformIO toolchains are unreliable under a folder
    with spaces, which rules out Application Support.
    """
    base = _native_xdg_base("XDG_DATA_HOME", Path.home() / ".local" / "share")
    return base / "mcu-flasher" / "platformio" / platform.machine()


def serial_access_hint(port: str) -> str:
    if sys.platform.startswith("linux") and port and Path(port).exists():
        if not os.access(port, os.R_OK | os.W_OK):
            return (
                f"Access denied to {port}. Add your account to the device's serial group "
                "(usually dialout on Linux), then sign out and back in. "
                "USB programmers may also require PlatformIO udev rules. See the Linux setup guide."
            )
    return "Check that the device is connected and no other application is using its serial port."
