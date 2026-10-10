"""Explicit offline preference and owned preparation extras for each host.

Installed runtimes, toolchains and sketch caches are shared application inputs;
they are never offline cleanup targets. All filesystem mutations here belong
to Bootstrap, after it acquires the package-store preparation lease.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
OWNER = ".mcu-offline-extras.json"
EXTRA_NAMES = frozenset((OWNER, "readiness.json", "archives"))


def _configuration():
    from main.core.config import _load_raw_config
    return _load_raw_config(fresh=True)


def offline_enabled(config=None):
    data = _configuration() if config is None else config
    shared = data.get("shared", {})
    return isinstance(shared, dict) and shared.get("offline_enabled") is True


def transition_pending(config=None):
    data = _configuration() if config is None else config
    shared = data.get("shared", {})
    return isinstance(shared, dict) and shared.get("offline_preparation_pending") is True


def startup_ready(core, plan=None, *, config=None, ignore_pending=False):
    """Require the prepared board/library inputs for this host and saved mode."""
    data = _configuration() if config is None else config
    if not ignore_pending and transition_pending(data):
        return False
    from src.modules.offline_bootstrap import ASSETS, ready
    if sys.platform == "win32" or offline_enabled(data):
        return ready(core, plan)
    core = Path(core)
    # Ubuntu's native launcher separately checks its configured board packs.
    # Keep this lower-level online runtime check for its setup/transition flow.
    return all((ROOT / name).is_file() for name in ASSETS) and all(
        (core / name).is_file() for name in (
            "packages/tool-scons/package.json", "packages/tool-scons/.piopm",
            "packages/tool-scons/scons.py",
        ))


def transition_blocker(backend=None):
    if backend is not None and (getattr(backend, "is_busy", False) or
                                getattr(backend, "active_operation", None)):
        return "Wait for the current operation to finish before changing offline mode."
    from main.core.config import _get_alive_pid_create_times, _instance_is_alive
    data = _configuration()
    alive = _get_alive_pid_create_times()
    for pid, instance in data.get("instances", {}).items():
        if (str(pid) != str(os.getpid()) and isinstance(instance, dict) and
                (instance.get("active_sketch_dir") or instance.get("hwnd")) and
                _instance_is_alive(str(pid), instance, alive)):
            return "Close the other MCU Flasher project windows before changing offline mode."
    return ""


def cancel_transition(previous):
    """Roll back only the mode request when saving/closing the sketch is cancelled."""
    data = _configuration()
    data.setdefault("shared", {})["offline_enabled"] = bool(previous)
    data["shared"]["offline_preparation_pending"] = False
    from main.core.config import _save_raw_config
    return _save_raw_config(data) is not False


def extras_directory(root=None, host=None):
    host = sys.platform if host is None else host
    if host not in ("win32", "linux"):
        raise RuntimeError("Offline preparation supports Windows and native Ubuntu.")
    return Path(root or ROOT) / "src" / "offline-extras" / host


def _is_link(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) &
                                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _checked_extras(root=None, host=None):
    root = Path(root or ROOT).resolve()
    extra = extras_directory(root, host)
    # Reject links at every parent, including the host directory itself.
    for parent in (root / "src", root / "src/offline-extras", extra):
        if parent.exists() and _is_link(parent):
            raise RuntimeError(f"Offline extras use a linked directory; files were retained: {parent}")
    if not extra.resolve().is_relative_to(root):
        raise RuntimeError("Offline extras leave the application directory.")
    return root, extra


def _owner(root, extra):
    return {"schema": 1, "installation": str(root).casefold(), "host": extra.name,
            "purpose": "MCU Flasher offline preparation extras"}


def _atomic(path, value):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def record_preparation(core, *, root=None, host=None):
    """Retain the certified plan separately from the shared installed packages."""
    root, extra = _checked_extras(root, host)
    if extra.exists():
        if not (extra / OWNER).is_file() or json.loads((extra / OWNER).read_text()) != _owner(root, extra):
            raise RuntimeError("Offline extras ownership is unavailable; existing files were retained.")
    else:
        extra.mkdir(parents=True)
        _atomic(extra / OWNER, _owner(root, extra))
    from src.modules.offline_bootstrap import MARKER
    certificate = json.loads((Path(core) / MARKER).read_text(encoding="utf-8"))
    _atomic(extra / "readiness.json", certificate)


def cleanup_extras(*, root=None, host=None):
    """Delete only an authenticated host extras tree; preserve unknown files."""
    root, extra = _checked_extras(root, host)
    if not extra.exists():
        return
    if not (extra / OWNER).is_file() or json.loads((extra / OWNER).read_text()) != _owner(root, extra):
        raise RuntimeError("Offline extras ownership is unavailable; existing files were retained.")
    children = list(extra.iterdir())
    if any(child.name not in EXTRA_NAMES for child in children):
        raise RuntimeError("Unrecognized offline extras were retained; cleanup was cancelled.")
    # Validate the entire finite cleanup target before removing even one file.
    def scan_failed(error):
        raise error
    for directory, directories, files in os.walk(extra, followlinks=False, onerror=scan_failed):
        for name in directories + files:
            item = Path(directory) / name
            if _is_link(item) or not item.resolve().is_relative_to(extra.resolve()):
                raise RuntimeError("Linked offline extras were retained; cleanup was cancelled.")
    for child in children:
        if child.name == OWNER:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    (extra / OWNER).unlink()
    extra.rmdir()


def finish_bootstrap(core):
    """Complete or retry a requested mode transition after successful setup."""
    data = _configuration()
    if not startup_ready(core, config=data, ignore_pending=True):
        raise RuntimeError("The selected mode is not fully prepared; restart preparation again.")
    if offline_enabled(data):
        record_preparation(core)
    elif transition_pending(data):
        cleanup_extras()
    if transition_pending(data):
        data.setdefault("shared", {})["offline_preparation_pending"] = False
        from main.core.config import _save_raw_config
        if _save_raw_config(data) is False:
            raise RuntimeError("Offline preparation finished, but the mode preference could not be saved. Retry Bootstrap.")
