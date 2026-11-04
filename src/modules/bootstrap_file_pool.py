"""Bounded parallel per-file work for Bootstrap package preparation.

The Arduino AVR packages alone hold about 55,000 very small files. Real-time
antivirus makes each open/close wait in the calling thread, so strictly serial
extraction, copying, hashing and deletion can take many minutes while appearing
frozen. Those waits release the GIL, so a small pool multiplies throughput.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time

MAX_WORKERS = 16
PROGRESS_SECONDS = 1.5


def file_workers(paths=None) -> int:
    """Scale with CPU count, within the slowest relevant drive's random-I/O cap."""
    workers = min(MAX_WORKERS, max(2, os.cpu_count() or 2))
    try:
        from main.core.storage_resources import storage_worker_limit
        limit = storage_worker_limit(paths, wait=True)
    except Exception:
        limit = None
    return max(1, min(workers, limit)) if limit else workers


class FilePool:
    """Run calls concurrently with backpressure; the first failure wins.

    Submission blocks once enough work or bytes are queued, so memory stays
    bounded and reported progress tracks completed writes. ``finish`` re-raises
    the first error; later queued work is skipped rather than retried.
    """

    def __init__(self, workers=None, *, max_pending=None, max_bytes=128 * 1024 * 1024):
        self.workers = workers or file_workers()
        self._executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="mcu-package-files")
        self._condition = threading.Condition()
        self._pending = self._bytes = 0
        self._max_pending = max_pending or self.workers * 8
        self._max_bytes = max_bytes
        self._error = None
        self._closed = False

    def _run(self, function, args, size):
        try:
            if self._error is None:
                function(*args)
        except BaseException as exc:  # pylint: disable=broad-except
            with self._condition:
                if self._error is None:
                    self._error = exc
        finally:
            with self._condition:
                self._pending -= 1
                self._bytes -= size
                self._condition.notify_all()

    def submit(self, function, *args, size=0):
        with self._condition:
            while self._error is None and self._pending and (
                    self._pending >= self._max_pending or self._bytes + size > self._max_bytes):
                self._condition.wait()
            if self._error is not None:
                raise self._error
            self._pending += 1
            self._bytes += size
        try:
            self._executor.submit(self._run, function, args, size)
        except BaseException:
            with self._condition:
                self._pending -= 1
                self._bytes -= size
                self._condition.notify_all()
            raise

    def wait(self):
        """Wait for queued work without closing the pool, then surface errors."""
        with self._condition:
            while self._pending:
                self._condition.wait()
            if self._error is not None:
                raise self._error

    def finish(self):
        try:
            self.wait()
        finally:
            self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            with self._condition:
                if self._error is None:
                    # An abandoned pool skips queued work during its own failure.
                    self._error = self._error or _Closed()
            self._executor.shutdown(wait=True)
            with self._condition:
                if isinstance(self._error, _Closed):
                    self._error = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.finish()
        else:
            self.close()
        return False


class _Closed(Exception):
    pass


class Progress:
    """Report whole percents at a bounded rate from any worker thread."""

    def __init__(self, log, label, total):
        self._log, self._label, self._total = log, label, max(1, int(total))
        self._lock = threading.Lock()
        self._done = 0
        self._next = time.monotonic() + PROGRESS_SECONDS
        self._last = -1

    def step(self, count=1):
        if self._log is None:
            return
        with self._lock:
            self._done += count
            now = time.monotonic()
            if now < self._next and self._done < self._total:
                return
            self._next = now + PROGRESS_SECONDS
            percent = min(100, self._done * 100 // self._total)
            if percent == self._last:
                return
            self._last = percent
        self._log(f"Package files: {self._label} {percent}%")


def run_all(function, items, *, workers=None, progress=None):
    """Apply ``function`` to every item concurrently; raise the first failure."""
    if not items:
        return
    with FilePool(workers) as pool:
        def call(item):
            function(item)
            if progress is not None:
                progress.step()
        for item in items:
            pool.submit(call, item)
