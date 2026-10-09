"""Native Ubuntu toolchains/processes. No Windows bootstrap, aliases or executables."""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path

from main.core.constants import SCRIPT_DIR
from src.modules.platform_runtime import native_platformio_dir
from src.modules.offline_runtime import offline_pio_command, bootstrap_instruction

_PLATFORMIO_ENV_CONFIG_LOCK = threading.RLock()
_PLATFORMIO_INI_WRITE_LOCK = threading.RLock()


def _get_safe_platformio_core_dir(script_dir: Path = SCRIPT_DIR) -> str:
    """Read-only location selection; never adopt a copied Windows package store."""
    return str(native_platformio_dir())


def _configure_platformio_environment(script_dir: Path = SCRIPT_DIR) -> str:
    core = Path(_get_safe_platformio_core_dir(script_dir))
    locations = {
        "PLATFORMIO_CORE_DIR": core,
        "PLATFORMIO_PLATFORMS_DIR": core / "platforms",
        "PLATFORMIO_PACKAGES_DIR": core / "packages",
        "PLATFORMIO_CACHE_DIR": core / ".cache",
        "PLATFORMIO_BUILD_CACHE_DIR": core / ".cache/build",
        "PLATFORMIO_GLOBALLIB_DIR": core / "lib",
        "TMPDIR": core / ".tmp",
    }
    with _PLATFORMIO_ENV_CONFIG_LOCK:
        for name, directory in locations.items():
            directory.mkdir(parents=True, exist_ok=True)
            os.environ[name] = str(directory)
        # Exported Windows launch hints cannot select a foreign interpreter.
        for name in ("PLATFORMIO_PYTHON_EXE", "PLATFORMIO_PENV_DIR", "PYTHONHOME"):
            os.environ.pop(name, None)
        os.environ["TEMP"] = os.environ["TMP"] = os.environ["TMPDIR"]
        for name in ("PYTHONUNBUFFERED", "PLATFORMIO_UNBUFFERED", "PLATFORMIO_DISABLE_UPGRADE_CHECK",
                     "PLATFORMIO_DISABLE_PROMPTS", "PLATFORMIO_NO_TELEMETRY", "PLATFORMIO_DISABLE_TELEMETRY"):
            os.environ[name] = "1"
    return str(core)


def _ensure_platformio_environment_for_build(script_dir: Path = SCRIPT_DIR) -> None:
    _configure_platformio_environment(script_dir)


def _refresh_platformio_core_environment(script_dir: Path = SCRIPT_DIR) -> tuple[Path, bool]:
    return Path(_configure_platformio_environment(script_dir)), False


def find_pio_executable() -> list[str] | None:
    """Use PlatformIO installed in the guarded native interpreter, never PATH exe shims."""
    if not sys.platform.startswith("linux") or sys.executable.lower().endswith(".exe"):
        return None
    try:
        if importlib.util.find_spec("platformio") is not None:
            return offline_pio_command(sys.executable)
    except (ImportError, ValueError):
        pass
    return None


def ensure_platformio() -> list[str] | None:
    # Runtime dependencies are repaired explicitly by direct/ubuntu/setup.py.
    return find_pio_executable()


def ensure_scons_ready(core_dir: str | Path | None = None) -> bool:
    return bool(core_dir and (Path(core_dir) / "packages/tool-scons/package.json").is_file())


def board_toolchain_ready(core_dir, platform, board_id, framework="arduino") -> bool:
    root = Path(core_dir) / "platforms" / str(platform)
    return (root / "platform.json").is_file() and (root / "boards" / (str(board_id) + ".json")).is_file()


def prepare_platformio_board_toolchain(platform, board_id, framework="arduino", label=None, **callbacks) -> bool:
    raise RuntimeError(bootstrap_instruction(f"Toolchain {platform}:{board_id} ({framework}) is not prepared"))


def find_arduino_cli_executable() -> str | None:
    from main.platforms.ubuntu_arduino import find_arduino_cli
    return find_arduino_cli()


def _bootstrap_find_arduino_cli():
    return find_arduino_cli_executable()


def _bootstrap_ensure_arduino_cli():
    return find_arduino_cli_executable()


def _bootstrap_get_last_arduino_cli_error():
    return None


def is_opencode_installed() -> bool:
    from main.platforms.ubuntu_opencode import find_opencode_cli
    return find_opencode_cli() is not None


def process_options(*, priority: bool = False, session: bool = False) -> dict:
    return {"start_new_session": True} if session else {}


def use_native_upload(board_info: dict) -> bool:
    return True


__all__ = ["_get_safe_platformio_core_dir", "_configure_platformio_environment",
           "_ensure_platformio_environment_for_build", "_refresh_platformio_core_environment",
           "_PLATFORMIO_INI_WRITE_LOCK", "_PLATFORMIO_ENV_CONFIG_LOCK", "find_pio_executable",
           "ensure_platformio", "ensure_scons_ready", "board_toolchain_ready",
           "prepare_platformio_board_toolchain", "find_arduino_cli_executable",
           "_bootstrap_find_arduino_cli", "_bootstrap_ensure_arduino_cli",
           "_bootstrap_get_last_arduino_cli_error", "is_opencode_installed",
           "process_options", "use_native_upload"]
