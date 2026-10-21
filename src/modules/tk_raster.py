"""Bounded CPU-only material rendering; all native image work stays on Tk."""
from __future__ import annotations

from collections import OrderedDict
import sys
import threading
import weakref


class RasterQueue:
    """One encoder, latest request per surface and one bounded result.

    The worker owns only primitive render inputs and PNG bytes. It never owns
    widgets, bound UI methods, or a Tcl interpreter, including during shutdown.
    """

    MAX_SURFACES = 16
    MAX_RESULT_BYTES = 8 * 1024 * 1024

    def __init__(self, encode):
        self.encode = encode
        self._condition = threading.Condition()
        self._pending = OrderedDict()
        self._versions = {}
        self._result = None
        self._thread = None
        self._active = None
        self._closed = False

    def request(self, token, generation, args):
        with self._condition:
            if self._closed:
                return False
            if token not in self._versions and len(self._versions) >= self.MAX_SURFACES:
                raise RuntimeError("Too many native material requests")
            self._versions[token] = generation
            self._pending[token] = (generation, args)
            if self._active is not None and self._active[0] == token:
                self._active[2].set()
            if self._thread is None:
                try:
                    self._thread = threading.Thread(target=self._run, daemon=True, name="TkMaterialRaster")
                    self._thread.start()
                except Exception:
                    self._thread = None
                    self._pending.pop(token, None)
                    self._versions.pop(token, None)
                    raise
            self._condition.notify_all()
        return True

    def discard(self, token):
        with self._condition:
            self._versions.pop(token, None)
            self._pending.pop(token, None)
            if self._active is not None and self._active[0] == token:
                self._active[2].set()
            if self._result is not None and self._result[0] == token:
                self._result = None
            self._condition.notify_all()

    def take(self):
        with self._condition:
            result, self._result = self._result, None
            if result is not None and self._versions.get(result[0]) == result[1]:
                self._versions.pop(result[0], None)
            self._condition.notify_all()
            return result

    def busy(self):
        with self._condition:
            return bool(self._thread is not None or self._pending or self._result is not None)

    def close(self):
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._versions.clear()
            self._result = None
            if self._active is not None:
                self._active[2].set()
            self._condition.notify_all()

    def _run(self):
        while True:
            with self._condition:
                while self._result is not None and not self._closed:
                    self._condition.wait()
                if self._closed or not self._pending:
                    self._thread = None
                    return
                token, (generation, args) = self._pending.popitem(last=False)
                cancel = threading.Event()
                self._active = (token, generation, cancel)
            try:
                data, error = self.encode(args, cancel), None
                if not isinstance(data, bytes) or len(data) > self.MAX_RESULT_BYTES:
                    raise ValueError("Native material exceeds the completed PNG budget")
            except Exception as failure:
                data, error = None, f"{type(failure).__name__}: {failure}"
            with self._condition:
                self._active = None
                if not self._closed and self._versions.get(token) == generation:
                    self._result = token, generation, data, error
            # Do not retain an extra completed PNG while the UI consumes it.
            data = None


class ResourceRaster:
    """Tk-thread delivery and weak widget lifetimes for a root's encoder."""

    def __init__(self, root, encode):
        self._root = weakref.ref(root)
        self._queue = RasterQueue(encode)
        self._targets = {}
        self._generation = 0
        self._timer = None
        self._closed = False
        root.bind("<Destroy>", self._destroy_root, add="+")

    def request(self, widget, key, args):
        if self._closed or getattr(widget, "_resource_request", None) == key:
            return
        token = id(widget)
        self._generation += 1
        generation = self._generation
        reference = weakref.ref(widget)
        if not getattr(widget, "_resource_lifetime_bound", False):
            widget._resource_lifetime_bound = True

            def invalidate(event):
                target = reference()
                if event.widget is target:
                    self.discard(target)

            widget.bind("<Unmap>", invalidate, add="+")
            widget.bind("<Destroy>", invalidate, add="+")
        widget._resource_request = key
        self._targets[token] = reference, generation, key
        try:
            self._queue.request(token, generation, args)
        except Exception:
            self._targets.pop(token, None)
            widget._resource_request = None
            root = self._root()
            if root is not None:
                root.report_callback_exception(*sys.exc_info())
        self._schedule()

    def discard(self, widget):
        token = id(widget)
        self._queue.discard(token)
        self._targets.pop(token, None)
        widget._resource_request = None
        widget._resource_ready = None

    def _schedule(self):
        root = self._root()
        if not self._closed and root is not None and self._timer is None and self._queue.busy():
            self._timer = root.after(16, self._deliver)

    def _deliver(self):
        self._timer = None
        if self._closed:
            return
        try:
            self._accept(self._queue.take())
        finally:
            self._schedule()

    def _accept(self, result):
        if result is not None:
            token, generation, data, error = result
            target = self._targets.get(token)
            if target is not None and target[1] == generation:
                self._targets.pop(token, None)
                reference, _generation, key = target
                widget = reference()
                if widget is not None:
                    widget._resource_request = None
                    if widget.winfo_exists() and widget.winfo_viewable():
                        if error is not None:
                            root = self._root()
                            try:
                                raise RuntimeError(f"Native material rendering failed: {error}")
                            except RuntimeError:
                                root.report_callback_exception(*sys.exc_info())
                        else:
                            widget._resource_ready = key, data
                            # _draw recomputes actual size/theme before creating
                            # a PhotoImage; a newer Configure may still be debounced.
                            pending = getattr(widget, "_redraw_id", None)
                            if pending is not None:
                                widget.after_cancel(pending)
                                widget._redraw_id = None
                            try:
                                widget._draw()
                            finally:
                                widget._resource_ready = None

    def _destroy_root(self, event):
        if event.widget is not self._root() or self._closed:
            return
        self._closed = True
        self._queue.close()
        self._targets.clear()
        if self._timer is not None:
            event.widget.after_cancel(self._timer)
            self._timer = None
