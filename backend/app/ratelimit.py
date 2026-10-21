"""In-memory rate limiters (per process)."""
from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindow:
    """`limit` events per `window` seconds. allow() -> (allowed, retry_after_seconds)."""

    def __init__(self, window: float = 60.0):
        self.window = window
        self._hits: dict[object, deque[float]] = defaultdict(deque)

    def allow(self, key: object, limit: int, now: float | None = None) -> tuple[bool, int]:
        t = now if now is not None else time.monotonic()
        q = self._hits[key]
        cutoff = t - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) >= limit:
            return False, max(1, int(q[0] + self.window - t) + 1)
        q.append(t)
        return True, 0

    def hit(self, key: object, now: float | None = None) -> None:
        self._hits[key].append(now if now is not None else time.monotonic())

    def count(self, key: object, now: float | None = None) -> int:
        t = now if now is not None else time.monotonic()
        q = self._hits.get(key)
        if not q:
            return 0
        cutoff = t - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        return len(q)

    def reset(self, key: object) -> None:
        self._hits.pop(key, None)

    def prune(self) -> None:
        t = time.monotonic()
        for k in list(self._hits):
            q = self._hits[k]
            while q and q[0] <= t - self.window:
                q.popleft()
            if not q:
                del self._hits[k]
