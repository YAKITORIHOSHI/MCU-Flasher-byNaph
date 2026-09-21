#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import time
import json
import re
import threading
from pathlib import Path


from main.core.constants import is_application_codebase_dir, SCRIPT_DIR
from main.core.theme import Theme, get_theme_mode, get_theme_settings

LOCAL_GUI_CONFIG = SCRIPT_DIR / "src" / "gui_config.json"
GUI_CONFIG_FILE = LOCAL_GUI_CONFIG if (SCRIPT_DIR / "src").exists() else (Path.home() / ".mcu_gui_config.json")

# Resolved at startup in main() before MCUUploadGUI is constructed. It lets
# startup choose a safe runtime editor independently of the saved preference
# when native Monaco dependencies are unavailable or a previous boot failed.
_RESOLVED_EDITOR_MODE = None

# ── Per-instance config key ─────────────────────────────────────────────────
# Each GUI window (process) gets a unique instance ID so two windows launched
# at the same time don't read/write each other's "last_sketch_dir" in the
# shared config file.  The config JSON becomes:
#   { "instances": { "<pid>": { "last_sketch_dir": "..." }, ... },
#     "shared": { ... }   ← reserved for truly-shared settings later }
# Old single-key format is migrated automatically on first load.
import os as _os
_INSTANCE_ID = str(_os.getpid())
del _os

# The normal launcher is deliberately single-window.  A named OS mutex is
# crash-safe (Windows releases it when a process dies) and avoids relying on
# a writable config file during the bootstrap/relaunch race.  --new-window is
# the explicit opt-in used when a user really wants an independent task.
_GUI_INSTANCE_MUTEX = None
_RESET_CACHE_MUTEX_NAME = "Local\\MCUFlasherByNaph.ResetCache"


def _try_acquire_reset_cache_lock():
    """Acquire the Windows cross-process reset-cache mutex without waiting.

    Hard Reset, Soft Reset, and Clean share app-level recovery folders even
    when ``--new-window`` starts multiple processes.  Serializing those three
    operations prevents one window from deleting a tree another is building
    or flashing.  Windows abandons an owned mutex automatically on process
    exit, so a crash cannot leave a permanent lock.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.CreateMutexW(None, False, _RESET_CACHE_MUTEX_NAME)
        if not handle:
            return None
        wait_result = kernel32.WaitForSingleObject(handle, 0)
        if wait_result in (0x00000000, 0x00000080):  # acquired / abandoned
            return int(handle)
        kernel32.CloseHandle(handle)
    except Exception:
        pass
    return None


def _release_reset_cache_lock(handle) -> None:
    """Release a handle returned by ``_try_acquire_reset_cache_lock``."""
    if not handle or sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        native_handle = wintypes.HANDLE(handle)
        kernel32.ReleaseMutex(native_handle)
        kernel32.CloseHandle(native_handle)
    except Exception:
        pass


def _claim_gui_instance() -> bool:
    """Allow multiple GUI instances to run simultaneously.

    Per-project isolation is handled dynamically by find_project_window()
    so users can work on multiple different sketches concurrently without
    artificial whole-app singleton blocking (Arduino IDE pattern).
    """
    return True


def _release_gui_instance() -> bool:
    """No-op release for multi-window support."""
    return True


_CONFIG_MEM_CACHE: dict = {}
_CONFIG_MEM_MTIME: float = 0.0

def _load_raw_config() -> dict:
    global _CONFIG_MEM_CACHE, _CONFIG_MEM_MTIME
    now = time.time()
    if _CONFIG_MEM_CACHE and (now - _CONFIG_MEM_MTIME < 2.0):
        return _CONFIG_MEM_CACHE
    # Both locations are retained for compatibility with portable copies and
    # older installs.  The per-user file is authoritative whenever it is
    # valid: an app-local file can be copied from another device or rewritten
    # by an older Bootstrap pass and otherwise hide the user's editor choice.
    candidates = []
    user_config = Path.home() / ".mcu_gui_config.json"
    for target in (user_config, LOCAL_GUI_CONFIG):
        try:
            if target.exists() and target.stat().st_size > 0:
                data = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    candidates.append((target == user_config, target.stat().st_mtime_ns, data))
        except Exception:
            pass
    if candidates:
        _CONFIG_MEM_CACHE = max(candidates, key=lambda item: (item[0], item[1]))[2]
        _CONFIG_MEM_MTIME = now
        return _CONFIG_MEM_CACHE
    _CONFIG_MEM_CACHE = {}
    _CONFIG_MEM_MTIME = now
    return {}


def _save_raw_config(data: dict):
    global _CONFIG_MEM_CACHE, _CONFIG_MEM_MTIME
    payload = json.dumps(data, indent=2)
    _CONFIG_MEM_CACHE = data.copy()
    _CONFIG_MEM_MTIME = time.time()
    for target in (LOCAL_GUI_CONFIG, Path.home() / ".mcu_gui_config.json"):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
        except Exception:
            pass


def _own_create_time():
    """This process's start time (seconds since epoch), or None if psutil
    is unavailable. Stashed in an instance's config entry at registration
    so later liveness checks can tell 'still the same process' apart from
    'a different process now sitting on the same recycled PID'."""
    try:
        import psutil as _psutil_check
        return _psutil_check.Process().create_time()
    except Exception:
        return None


_pid_create_times_cache: "dict[str, float] | None" = None
_pid_create_times_timestamp: float = 0.0
_pid_cache_lock = threading.Lock()


def _get_alive_pid_create_times() -> "dict[str, float] | None":
    """Return {pid_str: create_time} for every process currently running,
    or None if psutil isn't available. Cached for 2.0s to avoid expensive
    system-wide process enumeration on rapid successive calls."""
    global _pid_create_times_cache, _pid_create_times_timestamp
    now = time.monotonic()
    with _pid_cache_lock:
        if _pid_create_times_cache is not None and (now - _pid_create_times_timestamp) < 2.0:
            return _pid_create_times_cache
        try:
            import psutil as _psutil_check
            result: dict[str, float] = {
                str(p.pid): float(p.info.get("create_time") or 0.0)
                for p in _psutil_check.process_iter(["create_time"])
            }
            _pid_create_times_cache = result
            _pid_create_times_timestamp = now
            return result
        except Exception:
            return None


def _instance_is_alive(pid: str, inst: dict, alive: "dict[str, float] | None") -> bool:
    """True if `inst` (an entry from the instances config) still belongs to
    a live MCU Flasher process. Requires both a matching PID AND a matching
    create_time, so a dead instance's stale entry can't be revived just
    because its old PID number got handed to a different process."""
    if alive is None:
        return True  # psutil unavailable — assume alive rather than falsely evict
    if pid not in alive:
        return False
    stored_ct = inst.get("create_time")
    proc_ct = alive.get(pid)
    if proc_ct is None:
        return True  # couldn't read the live process's create_time (e.g. permissions) —
                      # can't disprove it, so don't evict on uncertain grounds
    if stored_ct is None:
        # This entry predates create_time tracking, meaning it was written
        # by a build before this fix (or by a process that crashed before
        # a create_time could ever be recorded). There's no way to verify
        # it's really the same process that once owned this PID, so don't
        # give it the benefit of the doubt — treat it as stale.
        return False
    return abs(proc_ct - stored_ct) < 2.0


def get_editor_mode() -> str:
    """Return the persisted editor mode preference: 'default' or 'monaco'."""
    data = _load_raw_config()
    return data.get("shared", {}).get("editor_mode", "default")


def set_editor_mode(mode: str):
    data = _load_raw_config()
    data.setdefault("shared", {})["editor_mode"] = mode
    _save_raw_config(data)

def _detect_system_theme() -> str:
    """Query Windows registry for system app theme preference (Dark or Light)."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        )
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return "light" if val == 1 else "default"
    except Exception:
        return "default"


def get_theme_settings() -> tuple:
    """Return (theme_mode: str, follow_system: bool)."""
    data = _load_raw_config()
    shared = data.get("shared", {})
    mode = shared.get("theme_mode", "default")
    if mode not in Theme.PALETTES:
        mode = "default"
    follow_system = bool(shared.get("theme_follow_system", False))
    return mode, follow_system


def get_theme_mode() -> str:
    """Return the active theme mode: 'default', 'light', or 'solarized_dark',
    accounting for system default preference if enabled."""
    mode, follow_system = get_theme_settings()
    if follow_system:
        return _detect_system_theme()
    return mode


def set_theme_mode(mode: str, follow_system: bool = False):
    """Persist the theme mode preference and follow_system flag, then update Theme attributes."""
    if mode not in Theme.PALETTES:
        mode = "default"
    data = _load_raw_config()
    data.setdefault("shared", {})["theme_mode"] = mode
    data["shared"]["theme_follow_system"] = bool(follow_system)
    _save_raw_config(data)
    active_mode = _detect_system_theme() if follow_system else mode
    Theme.apply_theme(active_mode)

Theme.apply_theme(get_theme_mode())


def get_autosave_settings() -> tuple:
    """Return (enabled: bool, delay_ms: int) for the Default editor's
    auto-save feature. Defaults to disabled with a 1500ms delay."""
    data = _load_raw_config()
    shared = data.get("shared", {})
    enabled = bool(shared.get("autosave_enabled", False))
    try:
        delay_ms = int(shared.get("autosave_delay_ms", 1500))
    except Exception:
        delay_ms = 1500
    if delay_ms < 200:
        delay_ms = 200
    return enabled, delay_ms


def set_autosave_settings(enabled: bool, delay_ms: int):
    data = _load_raw_config()
    shared = data.setdefault("shared", {})
    shared["autosave_enabled"] = bool(enabled)
    shared["autosave_delay_ms"] = int(delay_ms)
    _save_raw_config(data)


def get_periodic_reload_settings() -> tuple:
    """Return (enabled: bool, interval_s: int) for the periodic editor
    tab reload feature. Always disabled."""
    return False, 5


def set_periodic_reload_settings(enabled: bool, interval_s: int):
    data = _load_raw_config()
    shared = data.setdefault("shared", {})
    shared["periodic_reload_enabled"] = False
    _save_raw_config(data)


def get_monitor_font_size() -> int:
    """Return the shared Build/Serial/Syntax display size (12pt by default)."""
    try:
        size = int(_load_raw_config().get("shared", {}).get("monitor_font_size", 12))
    except Exception:
        size = 12
    return max(8, min(24, size))


def get_monaco_boot_pending() -> bool:
    """True if a previous Monaco launch never confirmed it started cleanly
    (i.e. the process crashed before it could clear the sentinel)."""
    data = _load_raw_config()
    return bool(data.get("shared", {}).get("monaco_boot_pending", False))


def set_monaco_boot_pending(pending: bool):
    data = _load_raw_config()
    data.setdefault("shared", {})["monaco_boot_pending"] = bool(pending)
    _save_raw_config(data)


def get_remembered_board_for_port(port_device: str) -> str:
    """Return the board type last successfully used with this physical port
    (e.g. 'COM16' -> 'ESP32 Dev Module'), or '' if nothing is on record.

    Stored in the 'shared' section (not per-instance) since a COM port is a
    physical machine-wide device — the same USB cable plugged into the same
    port should be remembered the same way regardless of which project/GUI
    instance is asking."""
    if not port_device:
        return ""
    data = _load_raw_config()
    return data.get("shared", {}).get("port_board_map", {}).get(port_device.upper(), "")


def remember_port_board(port_device: str, board_name: str):
    """Persist the (port -> board) association so the next time this exact
    physical port is attached — even after closing the app or opening a
    different project — the same board type is auto-selected instantly."""
    if not port_device or not board_name:
        return
    try:
        data = _load_raw_config()
        data.setdefault("shared", {}).setdefault("port_board_map", {})[port_device.upper()] = board_name
        _save_raw_config(data)
    except Exception:
        pass


def get_clear_serial_on_upload() -> bool:
    """Return whether Auto-clear Serial Monitor on Upload is enabled (defaults to True)."""
    try:
        data = _load_raw_config()
        return bool(data.get("shared", {}).get("clear_serial_on_upload", True))
    except Exception:
        return True


def set_clear_serial_on_upload(enabled: bool):
    try:
        data = _load_raw_config()
        data.setdefault("shared", {})["clear_serial_on_upload"] = bool(enabled)
        _save_raw_config(data)
    except Exception:
        pass


def get_clear_build_console_on_action() -> bool:
    """Return whether Clear Screen on Action for Build Console is enabled (defaults to True)."""
    try:
        data = _load_raw_config()
        return bool(data.get("shared", {}).get("clear_build_console_on_action", True))
    except Exception:
        return True


def set_clear_build_console_on_action(enabled: bool):
    try:
        data = _load_raw_config()
        data.setdefault("shared", {})["clear_build_console_on_action"] = bool(enabled)
        _save_raw_config(data)
    except Exception:
        pass


def get_hide_build_console_warnings() -> bool:
    """Return whether warning-tagged Build Console lines should be hidden."""
    try:
        data = _load_raw_config()
        return bool(data.get("shared", {}).get("hide_build_console_warnings", False))
    except Exception:
        return False


def set_hide_build_console_warnings(enabled: bool):
    try:
        data = _load_raw_config()
        data.setdefault("shared", {})["hide_build_console_warnings"] = bool(enabled)
        _save_raw_config(data)
    except Exception:
        pass


def get_auto_clear_serial_monitor() -> bool:
    """Return whether Auto Clear Serial Monitor is enabled (defaults to False)."""
    try:
        data = _load_raw_config()
        return bool(data.get("shared", {}).get("auto_clear_serial_monitor", False))
    except Exception:
        return False


def set_auto_clear_serial_monitor(enabled: bool):
    try:
        data = _load_raw_config()
        data.setdefault("shared", {})["auto_clear_serial_monitor"] = bool(enabled)
        _save_raw_config(data)
    except Exception:
        pass


def load_gui_config() -> dict:
    """Return this instance's config dict (creates it if absent)."""
    data = _load_raw_config()
    # Migrate old flat format {"last_sketch_dir": "..."} → new nested format
    if "instances" not in data:
        old_dir = data.get("last_sketch_dir", "")
        data = {"instances": {}, "shared": {}}
        if old_dir:
            data["instances"][_INSTANCE_ID] = {"last_sketch_dir": old_dir}
            data["shared"] = {"last_sketch_dir": old_dir}
        _save_raw_config(data)
    
    # Initialize the current PID's config block using fallback from shared or other active configs
    if _INSTANCE_ID not in data.get("instances", {}):
        if "instances" not in data:
            data["instances"] = {}
        fallback_dir = ""
        fallback_board = ""
        if "shared" in data and "last_sketch_dir" in data["shared"]:
            fallback_dir = data["shared"]["last_sketch_dir"]
        else:
            for inst in data.get("instances", {}).values():
                if inst.get("last_sketch_dir"):
                    fallback_dir = inst["last_sketch_dir"]
                    break
        if fallback_dir and is_application_codebase_dir(fallback_dir):
            fallback_dir = ""

        # Board always starts empty / unconfigured on launch (per user requirement)
        new_inst = {"last_sketch_dir": fallback_dir, "selected_board": ""}
        data["instances"][_INSTANCE_ID] = new_inst
        _save_raw_config(data)

    # Backfill create_time if this entry doesn't have one yet (covers both
    # the freshly-created case above and instances migrated from the old
    # flat format, which predate create_time tracking).
    inst = data["instances"].get(_INSTANCE_ID)
    if inst is not None and "create_time" not in inst:
        inst["create_time"] = _own_create_time()
        _save_raw_config(data)

    res = data["instances"].get(_INSTANCE_ID, {})
    if res.get("last_sketch_dir") and is_application_codebase_dir(res.get("last_sketch_dir")):
        res["last_sketch_dir"] = ""
    return res


def save_gui_config(config: dict):
    """Persist this instance's config dict without touching other instances."""
    if "last_sketch_dir" in config and is_application_codebase_dir(config["last_sketch_dir"]):
        config["last_sketch_dir"] = ""

    data = _load_raw_config()
    if "instances" not in data:
        data = {"instances": {}, "shared": {}}
    data["instances"][_INSTANCE_ID] = config
    
    # Also update the shared config so new instances can inherit it
    if "shared" not in data:
        data["shared"] = {}
    if "last_sketch_dir" in config and config["last_sketch_dir"]:
        data["shared"]["last_sketch_dir"] = config["last_sketch_dir"]
    if "selected_board" in config and config["selected_board"]:
        data["shared"]["selected_board"] = config["selected_board"]
        if config.get("last_sketch_dir"):
            data["shared"].setdefault("project_board_map", {})[config["last_sketch_dir"]] = config["selected_board"]
        
    # Prune stale instance entries only when the table accumulates entries (> 5),
    # preventing expensive system-wide psutil process enumeration on every config save.
    if len(data.get("instances", {})) > 5:
        alive = _get_alive_pid_create_times()
        if alive is not None:
            data["instances"] = {
                k: v for k, v in data["instances"].items()
                if k == _INSTANCE_ID or _instance_is_alive(k, v, alive)
            }
    _save_raw_config(data)


def get_project_remembered_board(project_dir: str) -> str:
    """Return the remembered board for a specific sketch project directory, if any."""
    if not project_dir:
        return ""
    try:
        data = _load_raw_config()
        p_str = str(Path(project_dir).resolve())
        return data.get("shared", {}).get("project_board_map", {}).get(p_str, "") or data.get("shared", {}).get("selected_board", "")
    except Exception:
        return ""


def set_project_remembered_board(project_dir: str, board_name: str) -> None:
    """Persist the board association for a specific sketch project directory."""
    if not project_dir or not board_name:
        return
    try:
        data = _load_raw_config()
        shared = data.setdefault("shared", {})
        shared["selected_board"] = board_name
        p_str = str(Path(project_dir).resolve())
        shared.setdefault("project_board_map", {})[p_str] = board_name
        _save_raw_config(data)
    except Exception:
        pass


def load_recent_projects() -> list[str]:
    """Load and return the list of recently opened project paths (up to 10),
    automatically filtering out folders that no longer exist on disk or belong to the application codebase."""
    data = _load_raw_config()
    recent = data.get("shared", {}).get("recent_projects", [])
    valid_recent = []
    changed = False
    for p in recent:
        try:
            if Path(p).is_dir() and not is_application_codebase_dir(p):
                valid_recent.append(p)
            else:
                changed = True
        except Exception:
            changed = True
    if changed:
        if "shared" not in data:
            data["shared"] = {}
        data["shared"]["recent_projects"] = valid_recent
        _save_raw_config(data)
    return valid_recent


def add_recent_project(path: str):
    """Add a project folder path to the recent list (max 10 folders),
    bumping it to the top of the list if it already exists."""
    if not path or is_application_codebase_dir(path):
        return
    data = _load_raw_config()
    if "shared" not in data:
        data["shared"] = {}
    recent = data["shared"].get("recent_projects", [])
    try:
        path = str(Path(path).resolve())
    except Exception:
        path = str(path)
    if is_application_codebase_dir(path):
        return
    if path in recent:
        recent.remove(path)
    recent.insert(0, path)
    recent = recent[:10]
    data["shared"]["recent_projects"] = recent
    _save_raw_config(data)


def load_recent_boards() -> list[str]:
    """Load and return the list of recently selected boards (up to 5)."""
    data = _load_raw_config()
    recent = data.get("shared", {}).get("recent_boards", [])
    if not isinstance(recent, list):
        return []
    return [str(b).strip() for b in recent if b and isinstance(b, str)][:5]


def add_recent_board(board_name: str) -> list[str]:
    """Add a board name to the recent list (max 5 boards),
    bumping it to the top of the list if it already exists."""
    if not board_name or not isinstance(board_name, str):
        return load_recent_boards()
    name = board_name.strip()
    if not name:
        return load_recent_boards()
    data = _load_raw_config()
    if "shared" not in data:
        data["shared"] = {}
    recent = data["shared"].get("recent_boards", [])
    if not isinstance(recent, list):
        recent = []
    if name in recent:
        recent.remove(name)
    recent.insert(0, name)
    recent = recent[:5]
    data["shared"]["recent_boards"] = recent
    _save_raw_config(data)
    return recent


def port_occupied_owner(port: str | None) -> str | None:
    """Return the owning PID (as string) if `port` is occupied by another live instance, else None."""
    if not port or str(port).startswith("─"):
        return None
    match = re.match(r"(COM\d+|/dev/\S+)", str(port))
    target = (match.group(1) if match else str(port).split()[0]).upper()
    data = _load_raw_config()
    alive = _get_alive_pid_create_times()
    instances = data.get("instances", {})
    for pid, inst in instances.items():
        if pid == _INSTANCE_ID:
            continue
        if not _instance_is_alive(pid, inst, alive):
            continue
        p = inst.get("selected_port")
        if p:
            m = re.match(r"(COM\d+|/dev/\S+)", str(p))
            dev = (m.group(1) if m else str(p).split()[0]).upper()
            if dev == target:
                return pid
    return None


def get_occupied_ports() -> set[str]:
    """Retrieve set of COM port devices currently selected by other active instances."""
    data = _load_raw_config()
    occupied = set()
    
    # Get currently alive PIDs (with create_time) to filter out stale instances
    alive = _get_alive_pid_create_times()

    instances = data.get("instances", {})
    for pid, inst in instances.items():
        if pid == _INSTANCE_ID:
            continue
        if not _instance_is_alive(pid, inst, alive):
            continue
        port = inst.get("selected_port")
        if port:
            match = re.match(r"(COM\d+|/dev/\S+)", str(port))
            dev = match.group(1) if match else str(port).split()[0]
            if dev:
                occupied.add(dev)
                occupied.add(dev.upper())
    return occupied


def get_occupied_folders() -> dict[str, str]:
    """Retrieve project folders currently active in other live instances.

    Returns {resolved_folder_path: owner_pid}, mirroring get_occupied_ports()
    so the same "another window already has this" pattern covers both the
    COM port and the sketch-folder resource.
    """
    data = _load_raw_config()
    occupied: dict[str, str] = {}

    alive = _get_alive_pid_create_times()

    instances = data.get("instances", {})
    for pid, inst in instances.items():
        if pid == _INSTANCE_ID:
            continue
        if not _instance_is_alive(pid, inst, alive):
            continue
        folder = inst.get("active_sketch_dir")
        if folder:
            try:
                resolved = str(Path(folder).resolve())
            except Exception:
                resolved = folder
            occupied[resolved] = pid
    return occupied


def folder_lock_owner(path) -> str | None:
    """Return the owning PID (as a string) if `path` is locked by another
    live instance, else None. Accepts a str or Path; resolves before
    comparing so trailing slashes / '.' segments / case-on-Windows don't
    cause false negatives."""
    info = find_project_window(path, exclude_self=True)
    return str(info["pid"]) if info else None


def find_project_window(path, exclude_self: bool = False) -> dict | None:
    """Return {'pid': int, 'hwnd': int, 'folder': str} if `path` is currently
    active in a live instance, else None.

    Resolves canonical path and matches case-insensitively on Windows.
    Automatically filters out stale/dead processes. If exclude_self is True,
    skips the current process instance.
    """
    if not path:
        return None
    try:
        cand = Path(path).resolve()
        resolved = str(cand.parent if cand.is_file() else cand)
    except Exception:
        resolved = str(path)

    data = _load_raw_config()
    alive = _get_alive_pid_create_times()
    resolved_l = resolved.lower() if sys.platform == "win32" else resolved

    for pid, inst in data.get("instances", {}).items():
        if exclude_self and pid == _INSTANCE_ID:
            continue
        if not _instance_is_alive(pid, inst, alive):
            continue
        folder = inst.get("active_sketch_dir")
        if folder:
            try:
                f_cand = Path(folder).resolve()
                f_res = str(f_cand.parent if f_cand.is_file() else f_cand)
            except Exception:
                f_res = str(folder)
            f_cmp = f_res.lower() if sys.platform == "win32" else f_res
            if f_cmp == resolved_l:
                return {
                    "pid": int(pid) if pid.isdigit() else pid,
                    "hwnd": int(inst.get("hwnd", 0) or 0),
                    "folder": f_res,
                }
    return None


def focus_project_window(hwnd: int = 0, pid: int = 0) -> bool:
    """Restore and bring the specified window to the foreground.

    If hwnd is 0 or invalid, attempts to resolve the top-level visible window
    belonging to pid. Uses Win32 AttachThreadInput to bypass foreground lock.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        target_hwnd = hwnd if (hwnd and user32.IsWindow(hwnd)) else 0

        # If no valid hwnd, search for top-level visible window by pid
        if not target_hwnd and pid:
            found_hwnds: list[int] = []

            def _enum_cb(h, lparam):
                if user32.IsWindowVisible(h):
                    proc_id = ctypes.c_ulong()
                    user32.GetWindowThreadProcessId(h, ctypes.byref(proc_id))
                    if proc_id.value == pid:
                        length = user32.GetWindowTextLengthW(h)
                        if length > 0:
                            found_hwnds.append(h)
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            cb = WNDENUMPROC(_enum_cb)
            user32.EnumWindows(cb, 0)
            if found_hwnds:
                target_hwnd = found_hwnds[0]

        if not target_hwnd or not user32.IsWindow(target_hwnd):
            return False

        SW_RESTORE = 9
        SW_SHOWNORMAL = 1
        if user32.IsIconic(target_hwnd):
            user32.ShowWindow(target_hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(target_hwnd, SW_SHOWNORMAL)

        user32.BringWindowToTop(target_hwnd)
        user32.SetForegroundWindow(target_hwnd)

        # AttachThreadInput trick to guarantee foreground lock transfer
        cur_thread = kernel32.GetCurrentThreadId()
        tgt_thread = user32.GetWindowThreadProcessId(target_hwnd, None)
        if cur_thread != tgt_thread:
            user32.AttachThreadInput(cur_thread, tgt_thread, True)
            user32.SetForegroundWindow(target_hwnd)
            user32.AttachThreadInput(cur_thread, tgt_thread, False)

        return True
    except Exception:
        return False


def set_active_sketch_dir(folder_path: str, hwnd: int = 0):
    """Set the currently active sketch directory for this instance and update hwnd."""
    cfg = load_gui_config()
    if folder_path:
        try:
            cand = Path(folder_path).resolve()
            folder_path = str(cand.parent if cand.is_file() else cand)
        except Exception:
            folder_path = str(folder_path)
    cfg["active_sketch_dir"] = folder_path or ""
    if hwnd:
        cfg["hwnd"] = int(hwnd)
    save_gui_config(cfg)


def set_instance_hwnd(hwnd: int):
    """Store the Win32 window handle for this instance."""
    if not hwnd:
        return
    cfg = load_gui_config()
    cfg["hwnd"] = int(hwnd)
    save_gui_config(cfg)


def clear_active_sketch_dir():
    """Clear the active sketch directory for this instance upon project switch/close."""
    cfg = load_gui_config()
    cfg["active_sketch_dir"] = ""
    save_gui_config(cfg)


def clean_instance_config(pid_to_remove: str = None):
    """Remove this instance's record from the instances table upon exit."""
    target_pid = str(pid_to_remove or _INSTANCE_ID)
    data = _load_raw_config()
    if "instances" in data and target_pid in data["instances"]:
        del data["instances"][target_pid]
        _save_raw_config(data)


__all__ = [
    "GUI_CONFIG_FILE",
    "LOCAL_GUI_CONFIG",
    "_GUI_INSTANCE_MUTEX",
    "_INSTANCE_ID",
    "_RESET_CACHE_MUTEX_NAME",
    "_RESOLVED_EDITOR_MODE",
    "_claim_gui_instance",
    "_detect_system_theme",
    "_get_alive_pid_create_times",
    "_instance_is_alive",
    "_load_raw_config",
    "_own_create_time",
    "_release_gui_instance",
    "_release_reset_cache_lock",
    "_save_raw_config",
    "_try_acquire_reset_cache_lock",
    "add_recent_board",
    "add_recent_project",
    "clean_instance_config",
    "clear_active_sketch_dir",
    "find_project_window",
    "focus_project_window",
    "folder_lock_owner",
    "get_auto_clear_serial_monitor",
    "get_autosave_settings",
    "get_clear_build_console_on_action",
    "get_clear_serial_on_upload",
    "get_hide_build_console_warnings",
    "get_editor_mode",
    "get_monaco_boot_pending",
    "get_monitor_font_size",
    "get_occupied_folders",
    "get_occupied_ports",
    "get_periodic_reload_settings",
    "get_project_remembered_board",
    "get_remembered_board_for_port",
    "get_theme_mode",
    "get_theme_settings",
    "load_gui_config",
    "load_recent_boards",
    "load_recent_projects",
    "port_occupied_owner",
    "remember_port_board",
    "save_gui_config",
    "set_active_sketch_dir",
    "set_auto_clear_serial_monitor",
    "set_autosave_settings",
    "set_clear_build_console_on_action",
    "set_clear_serial_on_upload",
    "set_hide_build_console_warnings",
    "set_editor_mode",
    "set_instance_hwnd",
    "set_monaco_boot_pending",
    "set_periodic_reload_settings",
    "set_project_remembered_board",
    "set_theme_mode"
]
