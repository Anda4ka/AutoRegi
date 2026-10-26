"""Global HTTP concurrency and sliding-window request throttling."""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    """Allow at most ``limit`` acquisitions during a rolling time window."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(1.0, float(window_seconds))
        self._events: deque[float] = deque()
        self._condition = threading.Condition()

    def acquire(self) -> None:
        while True:
            with self._condition:
                now = time.monotonic()
                cutoff = now - self.window_seconds
                while self._events and self._events[0] <= cutoff:
                    self._events.popleft()
                if len(self._events) < self.limit:
                    self._events.append(now)
                    return
                wait_for = max(0.01, self._events[0] + self.window_seconds - now)
                self._condition.wait(timeout=wait_for)
