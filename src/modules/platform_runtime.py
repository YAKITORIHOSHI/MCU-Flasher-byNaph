"""Host capabilities shared by launchers and the GUI (no Qt imports)."""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


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


def app_cache_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / ".mcuflasher-app"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "mcu-flasher"


def native_platformio_dir() -> Path:
    """Separate native Linux toolchains from portable Windows executables."""
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "mcu-flasher" / "platformio" / platform.machine()


def serial_access_hint(port: str) -> str:
    if sys.platform.startswith("linux") and port and Path(port).exists():
        if not os.access(port, os.R_OK | os.W_OK):
            return (
                f"Access denied to {port}. Add your account to the device's serial group "
                "(usually dialout on Ubuntu), then sign out and back in. "
                "USB programmers may also require PlatformIO udev rules. See the Ubuntu setup guide."
            )
    return "Check that the device is connected and no other application is using its serial port."
