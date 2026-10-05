"""Reviewed Zephyr board availability and exact hardware-model migrations."""
from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path
import re

ALIASES = Path(__file__).with_name("zephyr_board_aliases.cmake")
_IDENTIFIERS = {
    "boards/st/stm32h747i_disco/stm32h747i_disco_stm32h747xx_m7.yaml":
        "stm32h747i_disco/stm32h747xx/m7",
    "boards/st/nucleo_h745zi_q/nucleo_h745zi_q_stm32h745xx_m7.yaml":
        "nucleo_h745zi_q/stm32h745xx/m7",
    "boards/we/oceanus1ev/we_oceanus1ev_1_1_0.yaml": "we_oceanus1ev@1.1.0",
}
_UNAVAILABLE = {
    "ebyte_e77_dev": ("ebyte_e77_dev", "stm32wle5cc"),
    "sparkfun_micromod_f405": ("sparkfun_micromod_stm32f405", "stm32f405rgt6"),
    "we_oceanus1": ("we_oceanus1", "stm32wle5ccu6 "),
}


def _read(path, limit=16384):
    with open(path, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Metadata exceeds reviewed bounds")
    return data.decode("utf-8-sig")


def _inside(path, base):
    return os.path.normcase(os.path.commonpath((path, base))) == os.path.normcase(str(base))


def _framework(framework_dir, environment):
    """Validate metadata in the configured core; external packages stay native."""
    if not isinstance(framework_dir, (str, os.PathLike)):
        return None
    core, packages = environment.get("PLATFORMIO_CORE_DIR"), environment.get("PLATFORMIO_PACKAGES_DIR")
    if not isinstance(core, str) or not isinstance(packages, str):
        return None
    try:
        core, packages, framework = Path(core).resolve(), Path(packages).resolve(), Path(framework_dir).resolve()
        if (packages != (core / "packages").resolve() or not _inside(packages, core)
                or not _inside(framework, packages) or framework == packages):
            return None
        if framework.name not in ("framework-zephyr", "framework-zephyr@3.40402.0"):
            return None
        metadata = json.loads(_read(framework / "package.json"))
        if metadata.get("name") != "framework-zephyr" or metadata.get("version") != "3.40402.0":
            return None
        version = dict(re.findall(r"^\s*(VERSION_MAJOR|VERSION_MINOR|PATCHLEVEL|VERSION_TWEAK|EXTRAVERSION)\s*=\s*([^\r\n]*)",
                                  _read(framework / "VERSION"), re.MULTILINE))
        if (version.get("VERSION_MAJOR", "").strip(), version.get("VERSION_MINOR", "").strip(),
                version.get("PATCHLEVEL", "").strip()) != ("4", "4", "2"):
            return None
        if version.get("VERSION_TWEAK", "0").strip() != "0" or version.get("EXTRAVERSION", "").strip():
            return None
        return framework
    except (OSError, TypeError, ValueError, UnicodeError):
        return None


def _cmake(command):
    if isinstance(command, (list, tuple)) and command:
        if not isinstance(command[0], (str, bytes, os.PathLike)):
            return False
        executable = os.fsdecode(command[0])
    elif isinstance(command, str):
        match = re.match(r'^\s*(?:"([^"]+)"|([^\s"]+))', command)
        if not match:
            return False
        executable = match[1] or match[2]
    else:
        return False
    return ntpath.basename(executable).lower() in ("cmake", "cmake.exe")


def _alias_metadata(framework):
    import yaml
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    try:
        for filename, expected in _IDENTIFIERS.items():
            if yaml.load(_read(framework / filename), Loader=loader).get("identifier") != expected:
                return False
        board = yaml.load(_read(framework / "boards/we/oceanus1ev/board.yml"), Loader=loader).get("board", {})
        return (board.get("name") == "we_oceanus1ev"
                and board.get("full_name") == "Oceanus-I EV"
                and [soc.get("name") for soc in board.get("socs", [])] == ["stm32wle5xx"]
                and board.get("revision", {}).get("default") == "1.1.0")
    except (OSError, AttributeError, TypeError, ValueError, UnicodeError, yaml.YAMLError):
        return False


def cmake_environment(command, environment):
    """Add only proven board aliases to Zephyr CMake, preserving explicit aliases."""
    if (not environment or not _cmake(command) or "ZEPHYR_BOARD_ALIASES" in environment
            or "BOARD_ROOT" in environment):
        return environment
    framework = _framework(environment.get("ZEPHYR_BASE"), environment)
    if framework is None or not _alias_metadata(framework) or not ALIASES.is_file():
        return environment
    result = dict(environment)
    result["ZEPHYR_BOARD_ALIASES"] = ALIASES.resolve().as_posix()
    return result


def _board_value(board, key, default=None):
    if isinstance(board, dict):
        value = board
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    return board.get(key, default)


def _definition_absent(framework, target):
    """Check current board names without following external board-root links."""
    import yaml
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    boards = framework / "boards"
    visited = 0
    try:
        if not boards.is_dir() or not _inside(boards.resolve(), framework):
            return False
        for root, dirs, files in os.walk(boards, followlinks=False):
            if not _inside(Path(root).resolve(), boards.resolve()):
                dirs[:] = []
                continue
            if "board.yml" not in files:
                continue
            visited += 1
            if visited > 4096:
                return False
            data = yaml.load(_read(Path(root) / "board.yml"), Loader=loader)
            models = data.get("boards", [data.get("board", {})])
            if any(board.get("name") == target or board.get("extend") == target for board in models):
                return False
        return visited > 0
    except (OSError, AttributeError, TypeError, ValueError, UnicodeError, yaml.YAMLError):
        return False


def unavailable_framework_reason(platform_name, platform_version, board, framework_dir):
    """Explain only reviewed phantom STM32 Zephyr declarations; unknowns stay native."""
    if (platform_name != "ststm32" or str(platform_version) != "20.0.0"
            or "ZEPHYR_BOARD_ALIASES" in os.environ or "BOARD_ROOT" in os.environ):
        return None
    ident = board.get("id") if isinstance(board, dict) else getattr(board, "id", None)
    expected = _UNAVAILABLE.get(ident)
    if expected is None:
        return None
    target, mcu = expected
    if (_board_value(board, "build.zephyr.variant", ident) != target
            or _board_value(board, "build.mcu") != mcu
            or "zephyr" not in _board_value(board, "frameworks", [])):
        return None
    framework = _framework(framework_dir, os.environ)
    if framework is None or not _definition_absent(framework, target):
        return None
    return (f"Zephyr 4.4.2 has no board definition for {ident} ({target}); "
            "ststm32 20.0.0 declares this framework without a matching target. "
            "Other declared frameworks remain available.")
