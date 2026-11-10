"""Bounded cloud-path classification and read-only Windows lock diagnostics."""
from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path
import subprocess
import sys


def is_sync_managed_path(path, *, environment=None, platform=None):
    if (platform or sys.platform) != "win32":
        return False
    environment = os.environ if environment is None else environment
    candidate = ntpath.normcase(ntpath.normpath(str(path)))
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "Dropbox", "GoogleDrive"):
        root = environment.get(name, "").strip()
        if not ntpath.isabs(root):
            continue
        root = ntpath.normcase(ntpath.normpath(root))
        try:
            if ntpath.commonpath((root, candidate)) == root:
                return True
        except ValueError:
            pass
    return False


def _restart_manager_processes(path):
    import ctypes
    from ctypes import wintypes as w

    class UniqueProcess(ctypes.Structure):
        _fields_ = [("pid", w.DWORD), ("started", w.FILETIME)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [("process", UniqueProcess), ("name", w.WCHAR * 256),
                    ("service", w.WCHAR * 64), ("kind", ctypes.c_int),
                    ("status", w.ULONG), ("session", w.DWORD), ("restartable", w.BOOL)]

    manager = ctypes.WinDLL("rstrtmgr", use_last_error=True)
    manager.RmStartSession.argtypes = [ctypes.POINTER(w.DWORD), w.DWORD, w.LPWSTR]
    manager.RmRegisterResources.argtypes = [w.DWORD, w.UINT, ctypes.POINTER(w.LPCWSTR),
        w.UINT, ctypes.POINTER(UniqueProcess), w.UINT, ctypes.POINTER(w.LPCWSTR)]
    manager.RmGetList.argtypes = [w.DWORD, ctypes.POINTER(w.UINT), ctypes.POINTER(w.UINT),
        ctypes.POINTER(ProcessInfo), ctypes.POINTER(w.DWORD)]
    manager.RmEndSession.argtypes = [w.DWORD]
    session, key = w.DWORD(), ctypes.create_unicode_buffer(33)
    if manager.RmStartSession(ctypes.byref(session), 0, key):
        return []
    try:
        paths = (w.LPCWSTR * 1)(str(Path(path).absolute()))
        if manager.RmRegisterResources(session, 1, paths, 0, None, 0, None):
            return []
        needed, count, reasons = w.UINT(), w.UINT(), w.DWORD()
        result = manager.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        for _ in range(2):
            if result != 234 or not 0 < needed.value <= 64:
                return []
            count = w.UINT(needed.value)
            records = (ProcessInfo * count.value)()
            result = manager.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), records, ctypes.byref(reasons))
            if result == 0:
                return [{"pid": int(item.process.pid), "name": item.name}
                        for item in records[:count.value] if item.process.pid]
        return []
    finally:
        manager.RmEndSession(session)


def blocking_processes(path):
    """Confine Restart Manager's registry waits to a disposable two-second child."""
    if sys.platform != "win32":
        return []
    from src.modules.bootstrap_pip_monitor import _WindowsJob
    job, process = None, None
    try:
        job = _WindowsJob()
        process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).absolute()),
            "--blocking-file", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW | 0x00000004)
        job.assign_and_resume(process)
        output, _errors = process.communicate(timeout=2.0)
        value = json.loads(output.decode('utf-8')) if process.returncode == 0 else []
        return value[:8] if isinstance(value, list) else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    finally:
        # Assign before resume: a Windows venv redirector can start a second
        # Python whose inherited pipes would otherwise outlive the timeout.
        if job is not None:
            job.close()
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass


def file_access_message(path, error):
    message = str(error)
    if sys.platform != "win32":
        return message
    if getattr(error, "winerror", None) in (5, 32, 33):
        users = blocking_processes(path)
        if users:
            names = ", ".join(f"{item.get('name') or 'Application'} (PID {item.get('pid')})" for item in users)
            return f"{message}\nWindows reports this file is in use by: {names}. Release the file and save again. Your editor buffer is retained."
        return f"{message}\nCheck write permission, cloud availability and applications using this file. Your editor buffer is retained."
    return message


if __name__ == "__main__":
    if sys.platform == "win32" and len(sys.argv) == 3 and sys.argv[1] == "--blocking-file":
        try:
            print(json.dumps(_restart_manager_processes(sys.argv[2]), ensure_ascii=True))
        except (OSError, ValueError):
            print("[]")
