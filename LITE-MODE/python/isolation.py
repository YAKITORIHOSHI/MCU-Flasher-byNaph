"""Lite-only runtime redirection. Never patches files in the original app."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sys
import threading


def state_directory():
    override = os.environ.get("MCU_LITE_STATE")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("MCU_LITE_STATE must be an absolute path")
        return path.resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return (base / "mcu-flasher-lite").resolve()


def environment(state):
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith(("PLATFORMIO_", "MCU_FLASHER_")) or key in {
            "PYTHONHOME", "PYTHONPATH", "PYTHONPYCACHEPREFIX", "SCONSFLAGS",
        }:
            env.pop(key, None)
    env.update({
        "MCU_LITE_STATE": str(state),
        "PYTHONPATH": str(state / "engine"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "MCU_PACKAGE_EVENTS_ROOT": str(state / "package-events"),
        "MCU_PREF_DIR": str(state / "engine"),
        "XDG_DATA_HOME": str(state / "data"),
        "XDG_CACHE_HOME": str(state / "cache"),
        "XDG_CONFIG_HOME": str(state / "config"),
        "TMPDIR": str(state / "tmp"), "TMP": str(state / "tmp"),
        "TEMP": str(state / "tmp"),
    })
    if sys.platform == "win32":
        env["LOCALAPPDATA"] = str(state / "windows-local")
        env["APPDATA"] = str(state / "windows-roaming")
    return env


def child_setup():
    """Also runs in PlatformIO/SCons Python children via private sitecustomize."""
    state = Path(os.environ["MCU_LITE_STATE"]).resolve()
    sys.dont_write_bytecode = True
    # The main backend's settings API normally merges a portable and home copy.
    # Retain its instance/port ownership logic, but give it one private store.
    from main.core import config
    from main.core.config_store import ConfigSnapshot, merge_changes
    settings = state / "settings.json"
    lock = threading.RLock()

    def load(fresh=False):
        with lock:
            try:
                return ConfigSnapshot(json.loads(settings.read_text(encoding="utf-8")))
            except FileNotFoundError:
                return ConfigSnapshot({})

    def save(data):
        with lock:
            current = load()
            original = getattr(data, "original", None)
            merged = merge_changes(current, original, data) if original is not None else data
            temporary = settings.with_suffix(".tmp")
            try:
                temporary.write_text(json.dumps(merged, indent=2), encoding="utf-8")
                temporary.replace(settings)
                return True
            except OSError:
                return False

    config._load_raw_config = load
    config._save_raw_config = save
    config._write_raw_config = save
    config.LOCAL_GUI_CONFIG = config.GUI_CONFIG_FILE = settings
    if not hasattr(config, "_lite_config_lock_factory"):
        config._lite_config_lock_factory = config.config_lock
    original_config_lock = config._lite_config_lock_factory
    config.config_lock = lambda path: original_config_lock(settings)

    # The original Windows hook installs .pth files into sys.prefix. Lite uses
    # its own sitecustomize instead and keeps the borrowed interpreter read-only.
    import src.modules.win_subprocess_hide as hide
    hide.install_venv_site_hook = lambda *args, **kwargs: None
    hide.install_platformio_penv_hook = lambda *args, **kwargs: False
    sys.modules["win_subprocess_hide"] = hide
    from src.modules import platform_runtime, bytecode_cache
    platform_runtime.app_cache_dir = lambda: state / "cache"
    platform_runtime.native_platformio_dir = lambda: state / "platformio"
    bytecode_cache.cache_directory = lambda: state / "pycache"
    # Make missing-package notices useful in this separate product.
    platform_runtime.repair_command = lambda: "retry Compile or Upload in Lite; board tools prepare automatically"
    from src.modules import offline_runtime
    offline_runtime.bootstrap_instruction = lambda detail="Missing board dependency": (
        f"{detail}. Lite checks board tools automatically before Compile or Upload. "
        "Retry the action while online; use Resources for additional libraries."
    )
    if sys.platform == "win32":
        from main.platforms import windows
        # The main app's drive-root aliases and legacy cleanup are shared by
        # all its copies. Lite must never repoint or retire those junctions.
        def short_namespace(real_dir):
            drive = Path(real_dir).drive or state.drive or "C:"
            key = hashlib.sha256(os.path.normcase(str(state)).encode()).hexdigest()[:10]
            root = windows._prepare_mcuflasher_app_root(Path(drive + "\\.mcu-flasher-lite"))
            for candidate in ((root / key) if root else state / "windows-short", state / "windows-short"):
                prepared = windows._prepare_mcuflasher_app_root(candidate)
                if prepared is not None:
                    return prepared
            return None
        windows._MCUFLASHER_APP_DIRNAME = ".mcu-flasher-lite"
        windows._mcuflasher_app_root = short_namespace
        windows._cleanup_legacy_app_aliases = lambda *args, **kwargs: None
        windows._neutralize_conflicting_global_platformio_config = lambda: None


def protect_original(original):
    """Audit Python mutations of the source installation and selected sketches.

    Native tool subprocesses receive only private staged projects and private
    package paths. This is a file-preservation guard, not an OS security sandbox.
    """
    original = Path(original).resolve()
    lite = original / "LITE-MODE"
    roots = [original]

    def check(path, dir_fd=None):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        target = Path(os.fsdecode(path))
        if not target.is_absolute() and isinstance(dir_fd, int) and dir_fd >= 0:
            # shutil's safe POSIX deletion passes a basename plus an owned
            # directory descriptor. Resolve that descriptor, not process cwd.
            try:
                target = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / target
            except OSError as exc:
                raise PermissionError("Cannot verify the directory of a relative mutation") from exc
        target = target.resolve()
        if (target.is_relative_to(original) and not target.is_relative_to(lite)
                or any(target.is_relative_to(root) for root in roots[1:])):
            raise PermissionError(f"Lite keeps original files read-only: {target}")

    def audit(event, args):
        if event == "open":
            mode, flags = args[1:3]
            if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            ):
                check(args[0])
        elif event in {"os.remove", "os.rmdir"}:
            check(args[0], args[1])
        elif event in {"os.mkdir", "os.chmod"}:
            check(args[0], args[2])
        elif event == "os.utime":
            check(args[0], args[3])
        elif event == "os.truncate":
            check(args[0])
        elif event in {"os.rename", "os.link"}:
            check(args[0], args[2])
            check(args[1], args[3])
        elif event == "os.symlink":
            check(args[1], args[2])
    sys.addaudithook(audit)
    return roots
