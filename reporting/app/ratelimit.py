"""In-process token-bucket rate limiter (per key)."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, capacity: int = 60, refill_per_s: float = 1.0,
                 max_keys: int = 10_000) -> None:
        self._capacity = capacity
        self._refill = refill_per_s
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, at = self._buckets.get(key, (float(self._capacity), now))
            tokens = min(self._capacity, tokens + (now - at) * self._refill)
            allowed = tokens >= 1
            self._buckets[key] = (tokens - 1 if allowed else tokens, now)
            if len(self._buckets) > self._max_keys:  # drop the stalest half
                for k, _ in sorted(self._buckets.items(), key=lambda kv: kv[1][1])[
                        : self._max_keys // 2]:
                    del self._buckets[k]
            return allowed
