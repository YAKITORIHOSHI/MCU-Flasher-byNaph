"""Bounded package progress and cross-process tool-store coordination.

Only background workers read/write these files. The workspace remains offline;
explicit downloader preparation runs in its own process.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
TERMINAL = frozenset(("ready", "failed", "cancelled", "unavailable", "interrupted"))
STAGES = frozenset(("queued", "downloading", "verifying", "extracting", "preparing", "refreshing")) | TERMINAL
_publish_lock = threading.Lock()


def event_directory(root=None):
    override = root or os.environ.get("MCU_PACKAGE_EVENTS_ROOT")
    return Path(override) if override else ROOT / "logs" / "package-jobs"


def package_core_directory():
    """Read-only lease identity; selecting it must not create stores/junctions."""
    import sys
    if sys.platform == "win32":
        return ROOT / "src" / ".platformio-mcu-gui"
    from src.modules.platform_runtime import native_platformio_dir
    return Path(native_platformio_dir())


def _job_path(job_id, root=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(job_id)):
        raise ValueError("Invalid package job identity")
    return event_directory(root) / (str(job_id) + ".json")


def _read(path):
    if path.stat().st_size > 65536:
        raise ValueError("Package job record exceeds its limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid package job record")
    return value


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with scratch.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(scratch, path)
    finally:
        scratch.unlink(missing_ok=True)


def _bounded(value, depth=0):
    if depth > 3:
        return str(value)[:512]
    if isinstance(value, str):
        return value[:2048]
    if isinstance(value, dict):
        return {str(k)[:80]: _bounded(v, depth + 1) for k, v in list(value.items())[:24]}
    if isinstance(value, (tuple, list)):
        return [_bounded(v, depth + 1) for v in value[:32]]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:512]


def publish_event(job_id, stage, *, root=None, title=None, message=None, progress=None, **details):
    """Atomically replace live progress; retain transitions, never percentage chatter."""
    if stage not in STAGES:
        raise ValueError("Unknown package preparation stage")
    path = _job_path(job_id, root)
    with _publish_lock:
        try:
            previous = _read(path)
        except (OSError, ValueError):
            previous = {}
        now = time.time()
        event = {"schema": 1, "job_id": str(job_id), "pid": os.getpid(),
                 "seq": int(previous.get("seq", 0)) + 1, "stage": stage,
                 "created": previous.get("created", now), "updated": now,
                 "title": str(title or previous.get("title") or "Board preparation")[:200],
                 "message": str(message or stage.capitalize())[:2048],
                 "progress": max(0, min(100, int(progress))) if progress is not None else None,
                 "details": _bounded(details)}
        history = previous.get("history", [])[-31:]
        if previous.get("stage") != stage:
            history.append({key: event[key] for key in ("seq", "stage", "updated", "message")})
        event["history"] = history
        while len(json.dumps(event, ensure_ascii=False).encode("utf-8")) > 60000:
            if event["details"]:
                event["details"] = {}
            elif len(event["history"]) > 1:
                event["history"].pop(0)
            else:
                event["message"] = event["message"][:256]
        _atomic(path, event)
        if not previous or stage in TERMINAL:
            # Keep a bounded amount of completed job recovery data. Active jobs
            # are never removed by retention.
            completed = []
            for candidate in path.parent.glob("*.json"):
                try:
                    item = _read(candidate)
                    if (item.get("stage") in TERMINAL or
                            (now - item.get("updated", now) > 3 and not process_alive(item.get("pid")))):
                        completed.append((item.get("updated", 0), candidate))
                except (OSError, ValueError):
                    continue
            for _, old in sorted(completed)[:-48]:
                old.unlink(missing_ok=True)
                for folder in ("requests", "reports"):
                    (path.parent / folder / old.name).unlink(missing_ok=True)
        return event


def write_request(job_id, metadata, root=None):
    """Pass index metadata in a file, avoiding shell and command-line size limits."""
    _job_path(job_id, root)
    path = event_directory(root) / "requests" / (str(job_id) + ".json")
    _atomic(path, metadata)
    return path


def get_job_snapshot(job_id, root=None):
    """Read the last complete record on a worker; absent/damaged state is None."""
    try:
        return _read(_job_path(job_id, root))
    except (OSError, ValueError):
        return None


def write_report(job_id, coverage, root=None):
    _job_path(job_id, root)
    path = event_directory(root) / "reports" / (str(job_id) + ".json")
    _atomic(path, coverage)
    return path


def process_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            api.OpenProcess.restype = wintypes.HANDLE
            api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = api.OpenProcess(0x00100000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5  # Access denied is not proof of exit.
            try:
                return api.WaitForSingleObject(handle, 0) == 258
            finally:
                api.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


class JobReader:
    """One independent reader per workspace; tolerates handoff and partial writes."""
    def __init__(self, root=None, since=None):
        self.root = event_directory(root)
        self.since = time.time() if since is None else since
        self.seen = {}

    def read_updates(self):
        updates = []
        def modified(path):
            try:
                return path.stat().st_mtime
            except OSError:
                return 0
        paths = sorted(self.root.glob("*.json"), key=modified, reverse=True)[:128]
        for path in paths:
            try:
                item = _read(path)
                job = item["job_id"]
                if path != _job_path(job, self.root) or item.get("schema") != 1 or item.get("stage") not in STAGES:
                    continue
                seq = int(item["seq"])
                old = self.seen.get(job)
                if (item["stage"] not in TERMINAL and time.time() - item["updated"] > 3
                        and not process_alive(item.get("pid"))):
                    item = dict(item, stage="interrupted", progress=None,
                                message="Preparation process stopped. Open Boards & Libraries to retry.")
                    seq += 1
                    item["seq"] = seq
                    item["history"] = [*item.get("history", []),
                                       {"seq": seq, "stage": "interrupted", "updated": time.time(), "message": item["message"]}]
                if old is not None and seq <= old:
                    continue
                transitions = [v for v in item.get("history", [])
                               if isinstance(v, dict) and int(v.get("seq", 0)) > (old or 0)
                               and (old is not None or v.get("updated", 0) >= self.since)]
                self.seen[job] = seq
                if len(self.seen) > 256:
                    self.seen.pop(next(iter(self.seen)))
                if old is not None or item["stage"] not in TERMINAL or item["updated"] >= self.since:
                    updates.append((item, transitions))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return sorted(updates, key=lambda pair: pair[0]["updated"])


class PackageStoreBusy(RuntimeError):
    pass


class _GateBusy(Exception):
    pass


def _store_directory(core, root=None):
    identity = os.path.normcase(str(Path(core).resolve()))
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return event_directory(root) / "leases" / key


@contextmanager
def _gate(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "gate.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise _GateBusy() from exc
            raise
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _live_leases(directory):
    leases = []
    for path in directory.glob("*.json"):
        try:
            entry = _read(path)
            if process_alive(entry.get("pid")):
                leases.append((path, entry))
            else:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            # Unknown state cannot grant permission to mutate the tool store.
            leases.append((path, {"mode": "prepare"}))
    return leases


@contextmanager
def package_store_lease(core, mode="use", *, wait=False, cancel=None, on_wait=None, root=None):
    """Allow concurrent builds; prepare waits for all users and excludes new users.

    Never call a waiting lease from the GUI thread. A cancelled waiting writer
    releases its reservation. The OS gate makes read/reserve transactions atomic.
    """
    if mode not in ("use", "prepare"):
        raise ValueError("Invalid package store lease mode")
    directory = _store_directory(core, root)
    token = directory / (uuid4().hex + ".json")
    reserved = False
    announced = False
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Package preparation cancelled while queued")
            acquired = False
            try:
                with _gate(directory):
                    others = [(p, v) for p, v in _live_leases(directory) if p != token]
                    writers = any(v.get("mode") != "use" for _, v in others)
                    if mode == "use" and not writers:
                        _atomic(token, {"pid": os.getpid(), "mode": mode})
                        reserved = acquired = True
                    elif mode == "prepare" and not writers:
                        if not others:
                            _atomic(token, {"pid": os.getpid(), "mode": mode})
                            reserved = acquired = True
                        elif wait and not reserved:
                            _atomic(token, {"pid": os.getpid(), "mode": "waiting"})
                            reserved = True
            except _GateBusy:
                pass
            if acquired:
                break
            if not wait:
                raise PackageStoreBusy("Board preparation is using the tool store. Wait for it to finish and try again.")
            if on_wait is not None and not announced:
                on_wait()
                announced = True
            if cancel is not None:
                cancel.wait(.2)
            else:
                time.sleep(.2)
        yield
    finally:
        if reserved:
            token.unlink(missing_ok=True)


def guarded_package_operation(method):
    """Protect hardware/build workers before they touch shared packages."""
    from functools import wraps

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        lease = package_store_lease(package_core_directory(),
                                    root=getattr(self, "_package_event_root", None))
        try:
            lease.__enter__()
        except (PackageStoreBusy, OSError) as exc:
            self.emit("console:log", {"text": str(exc), "tag": "warning", "newline": True})
            self.emit("notification", {"title": "Board preparation in progress" if isinstance(exc, PackageStoreBusy)
                       else "Package coordination unavailable", "message": str(exc), "type": "warning"})
            self._release_requested_operation()
            return False
        try:
            return method(self, *args, **kwargs)
        finally:
            try:
                lease.__exit__(None, None, None)
            except OSError as exc:
                self.emit("console:log", {"text": f"Could not release the package-use marker: {exc}",
                                          "tag": "warning", "newline": True})
    return wrapped
