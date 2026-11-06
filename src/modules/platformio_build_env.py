"""One PlatformIO environment for Compile, Upload and the reset builds.

PlatformIO deletes ``.pio/build`` whenever its project checksum changes, and that
checksum includes every ``PLATFORMIO_*`` option present in the environment. Two
actions launched with different variables therefore erase each other's objects.
Every application launch shares this environment and passes ``RUN_OPTIONS``, so
only a real source, flag or dependency change can rebuild anything.
"""
from __future__ import annotations

import os
from pathlib import Path

HOOK = Path(__file__).with_name("platformio_build_hook.py")
RUN_OPTIONS = ("--disable-auto-clean",)
USED_LIBRARIES = "mcu-used-libraries.json"
OBJECT_CACHE_ENV = "MCU_FLASHER_OBJECT_CACHE"
OBJECT_CACHE_LIMIT = 2 * 1024 * 1024 * 1024
_PRUNE_TARGET = OBJECT_CACHE_LIMIT * 3 // 4


def object_cache_dir(core_dir) -> Path:
    return Path(core_dir) / ".cache" / "objects"


def build_environment(base, core_dir, jobs) -> dict:
    """Return the launch environment shared by every PlatformIO build action."""
    from src.modules.bytecode_cache import cache_directory
    core = Path(core_dir)
    env = dict(base)
    jobs = str(jobs)
    env.update({
        "PLATFORMIO_CORE_DIR": str(core),
        "PLATFORMIO_CACHE_DIR": str(core / ".cache"),
        "PLATFORMIO_GLOBALLIB_DIR": str(core / "lib"),
        "TMP": str(core / ".tmp"), "TEMP": str(core / ".tmp"), "TMPDIR": str(core / ".tmp"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONWARNINGS": "ignore",
        "PLATFORMIO_UNBUFFERED": "1",
        "PLATFORMIO_SETTING_ENABLE_CACHE": "true",
        "PLATFORMIO_DISABLE_UPGRADE_CHECK": "1",
        "PLATFORMIO_DISABLE_PROMPTS": "1",
        "PLATFORMIO_NO_TELEMETRY": "1",
        "PLATFORMIO_DISABLE_TELEMETRY": "1",
        "PLATFORMIO_BUILD_JOBS": jobs,
        "PLATFORMIO_RUN_JOBS": jobs,
        "SCONSFLAGS": f"-j{jobs}",
        "PLATFORMIO_EXTRA_SCRIPTS": f"pre:{HOOK.as_posix()}\npost:{HOOK.as_posix()}",
        OBJECT_CACHE_ENV: str(object_cache_dir(core)),
    })
    # PlatformIO moves SCons' signature database into this directory when the
    # variable exists, sharing one ever-growing file across every project. The
    # hook enables the object cache and leaves each workspace its own database.
    env.pop("PLATFORMIO_BUILD_CACHE_DIR", None)
    env.pop("PYTHONOPTIMIZE", None)
    # Any non-empty value, including "0", disables bytecode writing. Without
    # bytecode every build recompiles PlatformIO and SCons from source.
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env["PYTHONPYCACHEPREFIX"] = str(cache_directory())
    return env


def clear_object_cache(core_dir) -> tuple[int, int]:
    """Delete every cached object for Clean and return (removed, could not remove)."""
    removed = failed = 0
    try:
        folders = [entry.path for entry in os.scandir(object_cache_dir(core_dir)) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return 0, 0
    for folder in folders:
        try:
            with os.scandir(folder) as files:
                paths = [entry.path for entry in files if entry.is_file(follow_symlinks=False)]
        except OSError:
            failed += 1
            continue
        for path in paths:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                failed += 1
    return removed, failed


def prune_object_cache(core_dir, *, limit=OBJECT_CACHE_LIMIT, target=None) -> int:
    """Evict least-recently-used objects; a miss only recompiles that one file."""
    cache = object_cache_dir(core_dir)
    entries, total = [], 0
    try:
        folders = [entry.path for entry in os.scandir(cache) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return 0
    for folder in folders:
        try:
            with os.scandir(folder) as files:
                for entry in files:
                    if entry.is_file(follow_symlinks=False):
                        info = entry.stat(follow_symlinks=False)
                        entries.append((info.st_mtime_ns, info.st_size, entry.path))
                        total += info.st_size
        except OSError:
            continue
    if total <= limit:
        return 0
    target = _PRUNE_TARGET if target is None else target
    removed = 0
    for _, size, path in sorted(entries):
        if total <= target:
            break
        try:
            os.unlink(path)
        except OSError:
            continue
        total -= size
        removed += size
    return removed
