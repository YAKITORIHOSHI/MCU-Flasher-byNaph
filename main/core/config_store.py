"""Cross-process preference transactions and snapshot merging."""
from __future__ import annotations

import copy
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_threads = threading.RLock()
_local = threading.local()


class ConfigSnapshot(dict):
    """A detached editable snapshot, with its original values for merging."""

    def __init__(self, data):
        super().__init__(copy.deepcopy(data))
        self.original = copy.deepcopy(data)


def merge_changes(current, original, edited):
    """Apply only changed keys; retain writes made by other project windows."""
    result = copy.deepcopy(current)
    for key in original.keys() - edited.keys():
        result.pop(key, None)
    for key, value in edited.items():
        if key in original and value == original[key]:
            continue
        if isinstance(value, dict) and (isinstance(original.get(key), dict) or
                                        key not in original and isinstance(result.get(key), dict)):
            result[key] = merge_changes(result.get(key, {}), original.get(key, {}), value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@contextmanager
def config_lock(path: Path):
    """Lock a settings transaction on Windows/Ubuntu; process exit releases it."""
    with _threads:
        if getattr(_local, "depth", 0):
            _local.depth += 1
            try:
                yield
            finally:
                _local.depth -= 1
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_name(path.name + ".lock").open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                stream.seek(0, 2)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                def acquire():
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                def release():
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def acquire():
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                def release():
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            deadline = time.monotonic() + 2
            while True:
                try:
                    acquire()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise OSError("Another MCU Flasher window is updating settings. Try again.")
                    time.sleep(.01)
            _local.depth = 1
            try:
                yield
            finally:
                _local.depth = 0
                release()
