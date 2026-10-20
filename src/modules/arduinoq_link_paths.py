"""Tool-only SCons hook for the verified Arduino Q rc.4 linker-path defect.

This script executes inside PlatformIO, never as an application backend service.
It changes only two linker-script argument representations, not package files,
board identity, upload behavior or linker-script contents.
"""
from pathlib import Path
import hashlib
import json
import sys

_PLATFORM_SHA256 = "2ae4b31c96c795424f3b38579530b62aa0251004d1f2b28ed10f1ddcc7e7c8c0"
_BOARD_SHA256 = "8cbeb02c478524a13cb79267212ae4a768fc4afdf5ae9b7ad5e1084ed5943832"
_LINKER_SHA256 = "d5547a1d22fa78a5c84a44ec2d53e1dade6d29d37f56c999299b1de789437ee1"


def _bounded(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("Arduino Q compatibility requires regular bounded metadata")
    with path.open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError("Arduino Q compatibility metadata exceeds its byte bound")
    return raw


def _repair(env):
    if sys.platform != "linux" or env.subst("$BOARD") != "uno_q":
        return
    platform = env.PioPlatform()
    platform_dir = Path(platform.get_dir()).resolve()
    for path, expected in ((platform_dir / "platform.json", _PLATFORM_SHA256),
                           (platform_dir / "boards/uno_q.json", _BOARD_SHA256),
                           (platform_dir / "builder/artifacts/zephyr-llext.py", _LINKER_SHA256)):
        if hashlib.sha256(_bounded(path)).hexdigest() != expected:
            return
    framework_dir = Path(platform.get_package_dir("framework-arduino-zephyr")).resolve()
    package = json.loads(_bounded(framework_dir / "package.json"))
    if not isinstance(package, dict) or (package.get("name"), package.get("version")) != ("framework-arduino-zephyr", "0.56.0-leelabskku+unoq"):
        return
    node = env.File(env.subst("$BUILD_DIR/${PROGNAME}_debug.elf"))
    link_env = node.get_env()
    flags = list(link_env.get("LINKFLAGS", []))
    expected_rodata = Path(link_env.subst("$BUILD_DIR/rodata_split.ld")).resolve()
    changed = False
    for index, flag in enumerate(flags):
        if not index or str(flags[index - 1]) != "-T":
            continue
        value = flag.get_abspath() if hasattr(flag, "get_abspath") else link_env.subst(str(flag))
        path = Path(value).resolve()
        allowed = path == expected_rodata or (path.name == "build-dynamic.ld" and path.is_relative_to(framework_dir))
        if allowed:
            # A SCons File node retains one path argument through substitution
            # and native process argument escaping, including generated files.
            flags[index] = link_env.File(str(path))
            changed = True
    if changed:
        link_env.Replace(LINKFLAGS=flags)


# SCons owns this entry point. Tests extract the pure hook with fake native envs;
# the application never imports or starts a Python service from this file.
from SCons.Script import Import
Import("env")
_repair(env)
