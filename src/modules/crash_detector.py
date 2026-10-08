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
import threading
import traceback
import uuid
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
_SESSION_DIR = _USER_STATE_DIR / "sessions"
_CRASH_DIR = _USER_STATE_DIR / "crashes"
_DIAGNOSTIC_STREAM = None
_TRACKING_INSTALLED = False


def _installation_identity(path: Optional[Path] = None) -> str:
    root = path if path is not None else Path(__file__).resolve().parents[2]
    return str(Path(root).resolve(strict=False)).casefold()


def _process_create_time(pid: int) -> Optional[float]:
    try:
        import psutil
        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_record(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_files(directory: Path, legacy: Path) -> list[Path]:
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        files = []
    if legacy.is_file():
        files.append(legacy)
    return files


def _belongs_to_installation(data: dict, script_dir: Optional[Path]) -> bool:
    # Old single-session records did not carry an installation identity.
    return (script_dir is None or not data.get("installation_root")
            or data["installation_root"] == _installation_identity(script_dir))


def _session_is_alive(data: dict) -> bool:
    try:
        pid = int(data.get("pid", 0))
    except (TypeError, ValueError):
        return False
    if not is_process_alive(pid):
        return False
    expected = data.get("create_time")
    actual = _process_create_time(pid)
    if expected is not None and actual is not None:
        try:
            return abs(float(expected) - actual) < 0.1
        except (ValueError, TypeError):
            return False
    return True


def running_sessions(script_dir: Path) -> list[dict]:
    """Return only live GUI sessions belonging to this installation."""
    result = []
    for path in _record_files(_SESSION_DIR, _SESSION_SENTINEL_FILE):
        data = _read_record(path)
        if (data.get("status") == "running" and _belongs_to_installation(data, script_dir)
                and _session_is_alive(data)):
            result.append(data)
    return result


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_process_alive(pid: int) -> bool:
    """Check if a process with this PID is genuinely active and running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
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


def mark_session_started(pid: Optional[int] = None, workspace_dir: Optional[Path] = None) -> None:
    """Mark the beginning of an active application session."""
    try:
        active_pid = pid or os.getpid()
        payload = {
            "pid": active_pid,
            "create_time": _process_create_time(active_pid),
            "installation_root": _installation_identity(workspace_dir),
            "status": "running",
            "started_at": _iso_now(),
            "diagnostic_log": str(_CRASH_DIR / f"native-{active_pid}.log"),
            "output_log": os.environ.get("MCU_FLASHER_GUI_LOG", ""),
        }
        _atomic_write(_SESSION_DIR / f"{active_pid}.json", payload)
    except Exception:
        pass


def mark_session_clean_exit(pid: Optional[int] = None) -> None:
    """Mark the clean termination of an application session."""
    try:
        active_pid = pid or os.getpid()
        (_SESSION_DIR / f"{active_pid}.json").unlink(missing_ok=True)
        # Upgrade the legacy record only when it belongs to this process.
        if _read_record(_SESSION_SENTINEL_FILE).get("pid") == active_pid:
            _SESSION_SENTINEL_FILE.unlink(missing_ok=True)
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
        active_pid = pid or os.getpid()
        payload = {
            "crash_type": crash_type,
            "exc_type": exc_type or "UnknownException",
            "details": details[:4000],
            "timestamp": _iso_now(),
            "pid": active_pid,
            "installation_root": _installation_identity(workspace_dir),
        }
        _atomic_write(_CRASH_DIR / f"{active_pid}.json", payload)
        
        # Also mirror to workspace log if provided
        if workspace_dir:
            try:
                log_file = Path(workspace_dir) / "logs" / f"gui_exception-{active_pid}.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.write_text(details[:8000], encoding="utf-8")
            except Exception:
                pass
    except Exception:
        pass


def install_crash_tracking(workspace_dir: Optional[Path] = None) -> None:
    """Capture native failures and unhandled worker exceptions per GUI process."""
    global _DIAGNOSTIC_STREAM, _TRACKING_INSTALLED
    if _TRACKING_INSTALLED:
        return
    _TRACKING_INSTALLED = True
    try:
        import faulthandler
        _CRASH_DIR.mkdir(parents=True, exist_ok=True)
        _DIAGNOSTIC_STREAM = (_CRASH_DIR / f"native-{os.getpid()}.log").open("a", encoding="utf-8")
        faulthandler.enable(file=_DIAGNOSTIC_STREAM, all_threads=True)
    except (OSError, RuntimeError):
        pass
    previous = threading.excepthook

    def worker_exception(args):
        try:
            if args.exc_type is not SystemExit:
                record_crash_event("unhandled_worker_exception",
                                   "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
                                   exc_type=getattr(args.exc_type, "__name__", "Exception"),
                                   workspace_dir=workspace_dir)
        finally:
            previous(args)

    threading.excepthook = worker_exception


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
    for marker in _record_files(_CRASH_DIR, _CRASH_MARKER_FILE):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("crash_type") and _belongs_to_installation(data, script_dir):
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
    for session in _record_files(_SESSION_DIR, _SESSION_SENTINEL_FILE):
        try:
            sentinel = json.loads(session.read_text(encoding="utf-8"))
            if (isinstance(sentinel, dict) and sentinel.get("status") == "running"
                    and _belongs_to_installation(sentinel, script_dir)):
                sentinel_pid = int(sentinel.get("pid", 0))
                # If the recorded PID is no longer alive, it died without clean exit
                if sentinel_pid and not _session_is_alive(sentinel):
                    started_at = sentinel.get("started_at", "unknown")
                    details = f"Session started at {started_at} terminated without clean exit."
                    for key in ("diagnostic_log", "output_log"):
                        log = str(sentinel.get(key) or "")
                        if log:
                            try:
                                with Path(log).open("rb") as stream:
                                    stream.seek(0, 2)
                                    stream.seek(max(0, stream.tell() - 8000))
                                    captured = stream.read().decode("utf-8", errors="replace").strip()
                                if captured:
                                    details += f"\n\n{log}:\n{captured}"
                            except OSError:
                                pass
                    return {
                        "crashed": True,
                        "type": "abnormal_termination",
                        "summary": f"Application terminated unexpectedly (PID {sentinel_pid})",
                        "details": details,
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
                    with gui_crash_log.open("rb") as stream:
                        stream.seek(max(0, st.st_size - 8000))
                        text = stream.read().decode("utf-8", errors="replace").strip()
                    # Older launchers redirected healthy Qt warnings/stdout to
                    # this same file. Only a recorded traceback/fatal failure
                    # is evidence of a crash without a dead session sentinel.
                    fatal_tokens = ("traceback (most recent call last)", "fatal python error",
                                    "windows fatal exception", "segmentation fault",
                                    "qthread: destroyed while thread is still running")
                    if any(token in text.casefold() for token in fatal_tokens):
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
    for marker in _record_files(_CRASH_DIR, _CRASH_MARKER_FILE):
        data = _read_record(marker)
        if _belongs_to_installation(data, script_dir):
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass

    # Successful repair clears dead records without concealing another window's lifecycle.
    for session in _record_files(_SESSION_DIR, _SESSION_SENTINEL_FILE):
        data = _read_record(session)
        if _belongs_to_installation(data, script_dir) and not _session_is_alive(data):
            try:
                session.unlink(missing_ok=True)
            except OSError:
                pass

    if script_dir:
        try:
            gui_crash_log = Path(script_dir) / "logs" / "gui_crash.log"
            if gui_crash_log.exists():
                gui_crash_log.unlink(missing_ok=True)
        except Exception:
            pass
