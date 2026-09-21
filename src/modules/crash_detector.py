#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crash_detector.py — Application crash detection and session lifecycle tracking.

Provides unified detection of:
1. Unhandled Python exceptions (via excepthook & crash markers).
2. Hard process terminations / segfaults / power loss (via Session Sentinel).
3. Immediate startup spawn crashes.
"""
from __future__ import annotations

import sys
import os
import time
import json
import ctypes
from pathlib import Path
from typing import Any, Optional


def _user_state_dir() -> Path:
    """Resolve per-user writable state directory dynamically."""
    override = os.environ.get("MCU_FLASHER_STATE_DIR", "").strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    base = (
        os.environ.get("LOCALAPPDATA", "").strip()
        or os.environ.get("APPDATA", "").strip()
        or str(Path.home() / "AppData" / "Local")
    )
    return Path(base) / "MCUFlasherByNaph"


_USER_STATE_DIR = _user_state_dir()
_SESSION_SENTINEL_FILE = _USER_STATE_DIR / "session_sentinel.json"
_CRASH_MARKER_FILE = _USER_STATE_DIR / "crash_marker.json"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_process_alive(pid: int) -> bool:
    """Check if a process with this PID is genuinely active and running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError):
        return False


def mark_session_started(pid: Optional[int] = None) -> None:
    """Mark the beginning of an active application session."""
    try:
        _SESSION_SENTINEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        active_pid = pid or os.getpid()
        payload = {
            "pid": active_pid,
            "status": "running",
            "started_at": _iso_now(),
        }
        tmp = _SESSION_SENTINEL_FILE.with_name(f"{_SESSION_SENTINEL_FILE.name}.tmp-{active_pid}")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, _SESSION_SENTINEL_FILE)
    except Exception:
        pass


def mark_session_clean_exit(pid: Optional[int] = None) -> None:
    """Mark the clean termination of an application session."""
    try:
        if not _SESSION_SENTINEL_FILE.parent.exists():
            return
        active_pid = pid or os.getpid()
        payload = {
            "pid": active_pid,
            "status": "clean_exit",
            "stopped_at": _iso_now(),
        }
        tmp = _SESSION_SENTINEL_FILE.with_name(f"{_SESSION_SENTINEL_FILE.name}.tmp-{active_pid}")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, _SESSION_SENTINEL_FILE)
    except Exception:
        pass


def record_crash_event(
    crash_type: str,
    details: str,
    exc_type: Optional[str] = None,
    pid: Optional[int] = None,
    workspace_dir: Optional[Path] = None,
) -> None:
    """Persist structured crash metadata to disk."""
    try:
        _CRASH_MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        active_pid = pid or os.getpid()
        payload = {
            "crash_type": crash_type,
            "exc_type": exc_type or "UnknownException",
            "details": details[:4000],
            "timestamp": _iso_now(),
            "pid": active_pid,
        }
        tmp = _CRASH_MARKER_FILE.with_name(f"{_CRASH_MARKER_FILE.name}.tmp-{active_pid}")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, _CRASH_MARKER_FILE)
        
        # Also mirror to workspace log if provided
        if workspace_dir:
            try:
                log_file = Path(workspace_dir) / "logs" / "gui_crash.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.write_text(details[:8000], encoding="utf-8")
            except Exception:
                pass
    except Exception:
        pass


def detect_previous_crash(script_dir: Optional[Path] = None) -> dict[str, Any]:
    """
    Inspect all crash vectors and determine if the previous run terminated abnormally.

    Returns:
        {
            "crashed": bool,
            "type": str,
            "summary": str,
            "details": str,
            "timestamp": str,
        }
    """
    # 1. Check explicit crash marker (unhandled exception hook)
    if _CRASH_MARKER_FILE.exists():
        try:
            data = json.loads(_CRASH_MARKER_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("crash_type"):
                return {
                    "crashed": True,
                    "type": data.get("crash_type", "unhandled_exception"),
                    "summary": f"Unhandled exception: {data.get('exc_type', 'Error')}",
                    "details": data.get("details", ""),
                    "timestamp": data.get("timestamp", ""),
                }
        except Exception:
            return {
                "crashed": True,
                "type": "corrupted_crash_marker",
                "summary": "Crash marker exists but could not be parsed",
                "details": "",
                "timestamp": _iso_now(),
            }

    # 2. Check session sentinel for abnormal termination (segfault, hard kill, brownout)
    if _SESSION_SENTINEL_FILE.exists():
        try:
            sentinel = json.loads(_SESSION_SENTINEL_FILE.read_text(encoding="utf-8"))
            if isinstance(sentinel, dict) and sentinel.get("status") == "running":
                sentinel_pid = int(sentinel.get("pid", 0))
                # If the recorded PID is no longer alive, it died without clean exit
                if sentinel_pid and not is_process_alive(sentinel_pid):
                    started_at = sentinel.get("started_at", "unknown")
                    return {
                        "crashed": True,
                        "type": "abnormal_termination",
                        "summary": f"Application terminated unexpectedly (PID {sentinel_pid})",
                        "details": f"Session started at {started_at} terminated without clean exit.",
                        "timestamp": started_at,
                    }
        except Exception:
            pass

    # 3. Check workspace crash log
    if script_dir:
        gui_crash_log = Path(script_dir) / "logs" / "gui_crash.log"
        if gui_crash_log.exists():
            try:
                st = gui_crash_log.stat()
                if st.st_size > 0:
                    text = gui_crash_log.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        mtime_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))
                        return {
                            "crashed": True,
                            "type": "gui_crash_log",
                            "summary": "GUI crash log detected",
                            "details": text[:2000],
                            "timestamp": mtime_iso,
                        }
            except Exception:
                pass

    return {
        "crashed": False,
        "type": "",
        "summary": "",
        "details": "",
        "timestamp": "",
    }


def clear_crash_state(script_dir: Optional[Path] = None) -> None:
    """Clear all crash markers and reset sentinel state after successful bootstrap/repair."""
    try:
        if _CRASH_MARKER_FILE.exists():
            _CRASH_MARKER_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    try:
        if _SESSION_SENTINEL_FILE.exists():
            # Mark clean so next launch sees a clean slate
            mark_session_clean_exit()
    except Exception:
        pass

    if script_dir:
        try:
            gui_crash_log = Path(script_dir) / "logs" / "gui_crash.log"
            if gui_crash_log.exists():
                gui_crash_log.unlink(missing_ok=True)
        except Exception:
            pass
