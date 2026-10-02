"""Compact catalog caches and bounded workers for the Tk package browser."""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from collections import OrderedDict

_CATALOGS = OrderedDict()
_CATALOG_LOCK = threading.Lock()
_WRITES = OrderedDict()
_WRITE_LOCK = threading.Lock()
_WRITER = None
CATALOG_SCHEMA = 1


def source_signature(paths):
    result = []
    for path in paths:
        path = os.path.abspath(path)
        try:
            stat = os.stat(path)
            result.append([path, stat.st_mtime_ns, stat.st_size])
        except OSError:
            result.append([path, None, None])
    return result


def _queue_cache_write(cache_path, sources, signature, items):
    global _WRITER
    with _WRITE_LOCK:
        _WRITES[cache_path] = (sources, signature, items)
        while len(_WRITES) > 2:
            _WRITES.popitem(last=False)
        if _WRITER is None:
            _WRITER = threading.Thread(target=_write_pending, daemon=True, name='CatalogCache')
            _WRITER.start()


def _write_pending():
    global _WRITER
    while True:
        with _WRITE_LOCK:
            if not _WRITES:
                _WRITER = None
                return
            cache_path, (sources, signature, items) = _WRITES.popitem(last=False)
        if source_signature(sources) != signature:
            continue
        temporary = f'{cache_path}.tmp-{os.getpid()}-{threading.get_ident()}'
        try:
            with open(temporary, 'w', encoding='utf-8') as stream:
                json.dump(dict(schema=CATALOG_SCHEMA, sources=signature, items=items),
                          stream, ensure_ascii=False, separators=(',', ':'))
            if source_signature(sources) == signature:
                os.replace(temporary, cache_path)
        except (OSError, ValueError, TypeError):
            pass  # The derived cache is optional and never replaces its source.
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def load_catalog(cache_path, sources, builder):
    """Reuse grouped metadata only while its source files and schema match."""
    signature = source_signature(sources)
    if not any(item[1] is not None for item in signature):
        return None
    with _CATALOG_LOCK:
        cached = _CATALOGS.get(cache_path)
        if cached and cached[0] == signature:
            _CATALOGS.move_to_end(cache_path)
            return cached[1]
        _CATALOGS.pop(cache_path, None)
    items = None
    try:
        with open(cache_path, encoding='utf-8') as stream:
            saved = json.load(stream)
        candidate = saved.get('items')
        if (saved.get('schema') == CATALOG_SCHEMA and saved.get('sources') == signature
                and isinstance(candidate, dict)
                and all(isinstance(value, dict) and isinstance(value.get('versions'), list)
                        and all(isinstance(version, dict) and isinstance(version.get('version'), str)
                                and isinstance(version.get('url'), str) for version in value['versions'])
                        for value in candidate.values())):
            items = candidate
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # A damaged derived cache is rebuilt from the original indexes.
    if items is None:
        items = builder()
        if items is None:
            return None
        if source_signature(sources) != signature:
            return items  # Do not persist data assembled during a source replacement.
        # Return ready data first; one writer saves at most two pending catalogs.
        _queue_cache_write(cache_path, sources, signature, items)
    with _CATALOG_LOCK:
        _CATALOGS[cache_path] = (signature, items)
        while len(_CATALOGS) > 2:
            _CATALOGS.popitem(last=False)
    return items


class TkTasks:
    """Workers enqueue callbacks; only the Tk thread calls Tcl or schedules after."""

    def __init__(self, root):
        self.root = root
        self._queue = queue.SimpleQueue()
        self._latest = {}
        self._lock = threading.Lock()
        self._active = 0
        self._timer = None
        self.closed = False
        self._cancellations = []
        root.bind('<Destroy>', self._destroy, add='+')

    def begin(self):
        with self._lock:
            if self.closed:
                return False
            self._active += 1
        if self._timer is None:
            self._timer = self.root.after(25, self._drain)
        return True

    def end(self):
        with self._lock:
            self._active = max(0, self._active - 1)

    def post(self, callback, *args, key=None):
        with self._lock:
            if self.closed:
                return
            if key is not None:
                self._latest[key] = (callback, args)
            else:
                self._queue.put((callback, args))

    def start(self, target, *args, failed=None):
        if not self.begin():
            return
        def run():
            try:
                target(*args)
            except Exception as error:
                if failed:
                    self.post(failed, str(error))
            finally:
                self.end()
        threading.Thread(target=run, daemon=True, name='BrowserTask').start()

    def _drain(self):
        self._timer = None
        deadline = time.perf_counter() + .004
        for _ in range(32):
            try:
                callback, args = self._queue.get_nowait()
            except queue.Empty:
                break
            self._call(callback, args)
            if time.perf_counter() >= deadline:
                break
        with self._lock:
            latest, self._latest = self._latest, {}
        for callback, args in latest.values():
            self._call(callback, args)
        with self._lock:
            pending = self._active or self._latest or not self._queue.empty()
        if pending and not self.closed:
            self._timer = self.root.after(25, self._drain)

    def _call(self, callback, args):
        if not self.closed:
            try:
                callback(*args)
            except Exception:
                self.root.report_callback_exception(*sys.exc_info())

    def _destroy(self, event):
        if event.widget is self.root:
            with self._lock:
                self.closed = True
                self._latest.clear()
            for cancel in self._cancellations:
                cancel()
            if self._timer is not None:
                self.root.after_cancel(self._timer)
                self._timer = None


class LatestScan:
    """One active disk scan and one latest request; superseded scans cancel."""

    def __init__(self, tasks, scan):
        self.tasks, self.scan = tasks, scan
        self._lock = threading.Lock()
        self._pending = None
        self._running = False
        self._cancel = None
        tasks._cancellations.append(self.cancel)

    def cancel(self):
        with self._lock:
            self._pending = None
            if self._cancel:
                self._cancel.set()

    def submit(self, request, completed):
        with self._lock:
            self._pending = request, completed
            if self._cancel:
                self._cancel.set()
            if self._running:
                return
            self._running = True
        self.tasks.start(self._run)

    def _run(self):
        while not self.tasks.closed:
            with self._lock:
                pending, self._pending = self._pending, None
                if pending is None:
                    self._running = False
                    self._cancel = None
                    return
                self._cancel = cancel = threading.Event()
            request, completed = pending
            try:
                value, error = self.scan(request, cancel), None
            except Exception as exc:
                value, error = None, str(exc)
            if not cancel.is_set() and not self.tasks.closed:
                self.tasks.post(completed, value, error)
        with self._lock:
            self._running = False
            self._pending = None


def scan_package_details(item, cancel):
    """Collect disk size and examples/board names in one cancellable traversal."""
    path = item.get('path', '')
    if os.path.isfile(path):
        return os.path.getsize(path), [], []
    size, examples, boards = 0, [], set()
    library = item.get('type') == 'Library'
    for root, _dirs, files in os.walk(path):
        if cancel.is_set():
            return None
        parts = os.path.relpath(root, path).casefold().split(os.sep)
        for name in files:
            if cancel.is_set():
                return None
            filename = os.path.join(root, name)
            try:
                size += os.path.getsize(filename)
            except OSError:
                continue
            if library and 'examples' in parts and name.lower().endswith(('.ino', '.pde')):
                examples.append(filename)
            elif not library and name == 'boards.txt':
                try:
                    with open(filename, encoding='utf-8', errors='replace') as stream:
                        for line in stream:
                            if cancel.is_set():
                                return None
                            key, separator, value = line.strip().partition('.name=')
                            if separator and key and '.' not in key and not key.startswith('#') and value.strip():
                                boards.add(value.strip())
                except OSError:
                    pass
    examples.sort(key=lambda name: os.path.basename(name).lower())
    return size, examples, sorted(boards, key=str.lower)
