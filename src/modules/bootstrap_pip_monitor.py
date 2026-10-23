"""Finite pip child monitoring without using quiet stdout as a failure signal.

This module uses only the standard library before pip has installed psutil.
It never starts pip at import time or records commands or environment values.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit

_LOG_LOCK = threading.Lock()
_TAIL_CHARS = 262144
_RETAINED_JOBS = []


class PipCleanupError(RuntimeError):
    """Terminal setup failure, raised only after owned writers have stopped."""


class _WindowsJob:
    """Assign a suspended child before allowing it to create any descendants."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel = self.kernel
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wintypes.DWORD, ctypes.c_void_p)
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, process):
        ctypes, wintypes, kernel = self.ctypes, self.wintypes, self.kernel
        if not kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("thread_id", wintypes.DWORD), ("process_id", wintypes.DWORD),
                        ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                        ("flags", wintypes.DWORD)]
        kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
        kernel.Thread32First.restype = wintypes.BOOL
        kernel.Thread32Next.argtypes = kernel.Thread32First.argtypes
        kernel.Thread32Next.restype = wintypes.BOOL
        kernel.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.GetProcessIdOfThread.argtypes = (wintypes.HANDLE,)
        kernel.GetProcessIdOfThread.restype = wintypes.DWORD
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ResumeThread.argtypes = (wintypes.HANDLE,)
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        opened, owned = [], []
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            resumed = False
            while found:
                if entry.process_id == process.pid:
                    # Pin identity before any resume. TIDs from a snapshot can
                    # disappear or be reused by an unrelated process.
                    thread = kernel.OpenThread(0x100802, False, entry.thread_id)
                    if not thread:
                        if ctypes.get_last_error() == 87:  # Vanished helper.
                            found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
                            continue
                        raise ctypes.WinError(ctypes.get_last_error())
                    opened.append(thread)
                    if kernel.WaitForSingleObject(thread, 0) == 0:
                        found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
                        continue
                    owner = kernel.GetProcessIdOfThread(thread)
                    if owner == process.pid:
                        owned.append(thread)
                    elif not owner and kernel.WaitForSingleObject(thread, 0) != 0:
                        raise ctypes.WinError(ctypes.get_last_error())
                found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            for thread in owned:
                if kernel.WaitForSingleObject(thread, 0) == 0:
                    continue  # A pinned helper may finish after enumeration.
                previous = kernel.ResumeThread(thread)
                if previous == 0xFFFFFFFF:
                    if kernel.WaitForSingleObject(thread, 0) == 0:
                        continue
                    raise ctypes.WinError(ctypes.get_last_error())
                if previous > 1:
                    raise OSError("Owned pip thread remains suspended after resume")
                # A running helper has count zero. It does not establish that
                # the initially suspended child ran.
                resumed = resumed or previous == 1
            if not resumed:
                raise OSError("Suspended pip child has no resumable thread")
        finally:
            for thread in opened:
                kernel.CloseHandle(thread)
            kernel.CloseHandle(snapshot)

    def stop(self):
        ctypes, wintypes = self.ctypes, self.wintypes
        class Accounting(ctypes.Structure):
            _fields_ = [("times", ctypes.c_int64 * 4), ("faults", wintypes.DWORD),
                        ("total", wintypes.DWORD), ("active", wintypes.DWORD),
                        ("terminated", wintypes.DWORD)]
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + 5
        while True:
            accounting = Accounting()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting),
                                                         ctypes.sizeof(accounting), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not accounting.active:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("An owned pip child has not stopped")
            time.sleep(.02)

    def close(self):
        if self.handle:
            if not self.kernel.CloseHandle(self.handle):
                if self not in _RETAINED_JOBS:
                    _RETAINED_JOBS.append(self)
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            self.handle = None
            if self in _RETAINED_JOBS:
                _RETAINED_JOBS.remove(self)


def download_timeout(package_id, *, critical=True):
    """Allow large Qt wheels enough time while retaining a finite deadline."""
    if package_id in {"pyside6", "pyqt5_qscintilla"}:
        return 900
    if not critical:
        return 600
    return 360 if package_id == "platformio" else 180


def install_timeout(package_ids, *, recovery=False):
    if {"pyside6", "pyqt5_qscintilla"}.intersection(package_ids):
        return 900
    return 600 if recovery else 300


def _redact(text):
    def url(match):
        try:
            value = urlsplit(match.group())
            host = value.netloc.rsplit("@", 1)[-1]
            return urlunsplit((value.scheme, host, value.path,
                               "[redacted]" if value.query else "", ""))
        except ValueError:
            return "[redacted URL]"
    text = re.sub(r"https?://[^\s<>]+", url, str(text), flags=re.I)
    text = re.sub(r"(?i)\bauthorization\s*:\s*[^\r\n]+", "Authorization: [redacted]", text)
    return re.sub(r"(?i)\b(password|token|api[_-]?key)(\s*[:=]\s*)\S+",
                  r"\1\2[redacted]", text)


@dataclass(frozen=True)
class PipResult:
    returncode: int | None
    elapsed: float
    reason: str
    output: str

    @property
    def ok(self):
        return self.reason == "completed" and self.returncode == 0

    @property
    def summary(self):
        return f"{self.reason}; {self.elapsed:.2f}s elapsed; exit code {self.returncode}"


def _exited(process):
    if sys.platform == "win32":
        return process.poll() is not None
    # Keep the original PID reserved until group teardown, including when the
    # parent exits while a descendant still owns the stdout pipe.
    return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def _stop(process, job):
    """Stop only the job/session created for this child, then reap its parent."""
    if job is not None:
        job.stop()
        # Assignment can fail before a suspended child enters the Job.
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def _persist(log_path, label, result):
    if log_path is None:
        return
    try:
        path = Path(log_path)
        with _LOG_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as output:
                output.write(f"\n=== {_redact(label)}: {result.summary} ===\n")
                if not result.ok and result.output:
                    output.write(_redact(result.output) + "\n")
    except OSError:
        pass  # A diagnostic write failure must not change an install outcome.


def run(command, *, env, timeout, cancelled=None, on_line=None, log_path=None, label="pip",
        on_cleanup_failure=None):
    """Run one owned child to completion, cancellation or an absolute deadline.

    Network waits remain pip's responsibility. A quiet transfer or local wheel
    installation is allowed until the finite monotonic deadline. Diagnostics
    retain a bounded output tail for ordinary failures and interrupted attempts.
    """
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("pip timeout must be finite and positive")
    started = time.monotonic()
    pending = Queue(maxsize=32)
    stopped = threading.Event()
    reader_done = threading.Event()
    tail, tail_chars = deque(), 0
    process = reader = job = None
    reason = "completed"
    cleanup_error = None

    def consume(line):
        nonlocal tail_chars
        line = _redact(line.rstrip("\r\n"))
        tail.append(line)
        tail_chars += len(line) + 1
        while tail_chars > _TAIL_CHARS and len(tail) > 1:
            tail_chars -= len(tail.popleft()) + 1
        if on_line is not None:
            try:
                on_line(line)
            except Exception:
                pass

    def read_output():
        try:
            while not stopped.is_set():
                text = process.stdout.readline(16384)
                if not text:
                    break
                while not stopped.is_set():
                    try:
                        pending.put(text, timeout=.1)
                        break
                    except Full:
                        pass
        finally:
            reader_done.set()

    try:
        if cancelled is not None and cancelled():
            reason = "cancelled before launch"
        else:
            options = dict(env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                           errors="replace", bufsize=1)
            if sys.platform == "win32":
                job = _WindowsJob()
                options["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                             | 0x4)  # CREATE_SUSPENDED: assign before resume.
            else:
                options["start_new_session"] = True
            process = subprocess.Popen(command, **options)
            if job is not None:
                job.assign_and_resume(process)
            reader = threading.Thread(target=read_output, name="bootstrap-pip-output", daemon=True)
            reader.start()
            exit_seen = None
            while True:
                now = time.monotonic()
                if cancelled is not None and cancelled():
                    reason = "cancelled"
                    break
                if _exited(process):
                    if reader_done.is_set() and pending.empty():
                        break
                    exit_seen = now if exit_seen is None else exit_seen
                    if now - exit_seen >= 2 and pending.empty():
                        reason = "output pipe remained open after child exit"
                        break
                if now - started >= timeout:
                    reason = f"absolute deadline ({timeout:g}s budget)"
                    break
                try:
                    consume(pending.get(timeout=min(.1, max(.001, timeout - (now - started)))))
                    # Backpressured queued output after exit is healthy work.
                    # Only an idle inherited pipe gets the short drain bound.
                    if exit_seen is not None:
                        exit_seen = time.monotonic()
                except Empty:
                    pass
    except Exception as exc:
        reason = "could not start" if process is None else "monitor failed"
        consume(f"{type(exc).__name__}: {_redact(exc)}")
    finally:
        if process is not None or job is not None:
            tree_stopped = process is None
            while True:
                try:
                    if not tree_stopped:
                        _stop(process, job)
                        tree_stopped = True
                    if job is not None:
                        job.close()
                    break
                except Exception as exc:
                    if cleanup_error is None:
                        cleanup_error = f"{type(exc).__name__}: {_redact(exc)}"
                        reason = "child cleanup failed"
                        consume(cleanup_error)
                        _persist(log_path, label, PipResult(None, time.monotonic() - started,
                            "child cleanup incomplete; setup is waiting for owned writers to stop", "\n".join(tail)))
                        if on_cleanup_failure is not None:
                            try:
                                on_cleanup_failure(cleanup_error)
                            except Exception:
                                pass
                    # Keep the host and its ownership alive. No retry, staging
                    # deletion or lease release is safe while a writer survives.
                    time.sleep(.2)
        if process is not None:
            drain_deadline = time.monotonic() + 1
            while reader is not None and (not reader_done.is_set() or not pending.empty()):
                try:
                    consume(pending.get(timeout=.05))
                except Empty:
                    pass
                if time.monotonic() >= drain_deadline:
                    break
            stopped.set()
            if reader is not None:
                reader.join(timeout=.2)
            if reader is None or not reader.is_alive():
                process.stdout.close()
        elapsed = time.monotonic() - started
    result = PipResult(None if process is None else process.poll(), elapsed, reason, "\n".join(tail))
    _persist(log_path, label, result)
    if cleanup_error is not None:
        raise PipCleanupError(f"pip cleanup failed; owned writers have now stopped. {result.summary}: {cleanup_error}")
    return result
