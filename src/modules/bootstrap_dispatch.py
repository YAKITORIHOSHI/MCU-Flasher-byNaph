"""Bounded setup events whose destination can change without restarting setup."""
from __future__ import annotations

from collections import deque
import threading


class _Signal:
    def __init__(self, owner, name):
        self.owner, self.name = owner, name

    def emit(self, *args):
        self.owner.emit(self.name, args)


class BootstrapDispatcher:
    NAMES = ("log", "status", "progress", "stop_spinner", "update_block",
             "commit_block", "clear_block", "close", "hide", "call")
    MAX_EVENTS = 512
    MAX_CHARS = 256000

    def __init__(self, thread_id=None, sink=None):
        self.thread_id = threading.get_ident() if thread_id is None else thread_id
        self._sink = sink
        self._events = deque()
        self._condition = threading.Condition()
        self._chars = 0
        self._dropped = 0
        self._closed = False
        for name in self.NAMES:
            setattr(self, "sig_" + name, _Signal(self, name))

    def attach(self, sink):
        if threading.get_ident() != self.thread_id:
            raise RuntimeError("Setup views must be attached on their creating thread")
        self._sink = sink

    @staticmethod
    def _cost(args):
        return sum(len(value) for value in args if isinstance(value, str))

    def emit(self, name, args):
        if self._closed:
            return
        if name in ("log", "status", "update_block"):
            limit = 65536 if name == "update_block" else 8192
            args = tuple(value[:limit] if isinstance(value, str) else value for value in args)
        if threading.get_ident() == self.thread_id and self._sink is not None:
            self.dispatch(name, args)
            return
        cost = self._cost(args)
        with self._condition:
            if name in ("progress", "status", "update_block") and self._events:
                old_name, old_args, old_cost = self._events[-1]
                if old_name == name and (name != "update_block" or old_args[0] == args[0]):
                    self._events.pop()
                    self._chars -= old_cost
            while len(self._events) >= self.MAX_EVENTS or self._chars + cost > self.MAX_CHARS:
                for index, (old_name, old_args, old_cost) in enumerate(self._events):
                    if old_name == "log" and old_args[1] not in ("section", "fail"):
                        del self._events[index]
                        self._chars -= old_cost
                        self._dropped += 1
                        break
                else:
                    self._condition.wait(.05)
                    if self._closed:
                        return
                    continue
            self._events.append((name, args, cost))
            self._chars += cost

    def take(self, maximum=48):
        with self._condition:
            events = []
            while self._events and len(events) < maximum:
                name, args, cost = self._events.popleft()
                self._chars -= cost
                events.append((name, args))
            dropped, self._dropped = self._dropped, 0
            self._condition.notify_all()
            return events, dropped

    def dispatch(self, name, args):
        if not self._closed and self._sink is not None:
            self._sink(name, args)

    def drain(self, maximum=48):
        if threading.get_ident() != self.thread_id:
            raise RuntimeError("Setup events must be drained on their creating thread")
        events, dropped = self.take(maximum)
        if dropped:
            self.dispatch("log", (f"Setup display skipped {dropped} queued lines; full output remains in the setup log.", "warn"))
        for name, args in events:
            # Resolve the sink for every event. A promotion callback can change
            # it while this same batch still contains pending output.
            self.dispatch(name, args)

    def close(self):
        with self._condition:
            self._closed = True
            self._events.clear()
            self._chars = 0
            self._condition.notify_all()
