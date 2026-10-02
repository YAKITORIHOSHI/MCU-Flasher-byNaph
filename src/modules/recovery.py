"""Small retry budget for safe, reversible service recovery."""
from __future__ import annotations

import threading
import time
from collections import deque


class RecoveryBudget:
    """Limit failures in a time window; success does not erase a crash loop."""

    def __init__(self, delays=(0.5, 1.5, 4.0), window=60.0, clock=time.monotonic):
        self.delays = tuple(delays)
        self.window = float(window)
        self.clock = clock
        self._failures = deque(maxlen=len(self.delays) + 1)
        self._lock = threading.Lock()

    def next_delay(self) -> float | None:
        with self._lock:
            now = self.clock()
            while self._failures and now - self._failures[0] >= self.window:
                self._failures.popleft()
            index = len(self._failures)
            self._failures.append(now)
            return self.delays[index] if index < len(self.delays) else None
