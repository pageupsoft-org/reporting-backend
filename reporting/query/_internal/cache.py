"""Result cache keyed by a hash of (SQL, parameters, connection, snapshot version).

Uses Redis when a URL is configured, otherwise a bounded in-process LRU with TTL.
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Protocol

import pandas as pd


def cache_key(sql: str, params: dict[str, Any], connection_id: str, version: int,
              max_rows: int) -> str:
    payload = json.dumps([sql, params, connection_id, version, max_rows], sort_keys=True,
                         default=str)
    return "reporting:result:" + hashlib.sha256(payload.encode()).hexdigest()


class ResultCache(Protocol):
    def get(self, key: str, max_age_s: float | None = None) -> tuple[pd.DataFrame, bool] | None: ...
    def set(self, key: str, frame: pd.DataFrame, truncated: bool, ttl_s: int) -> None: ...


class MemoryCache:
    def __init__(self, max_entries: int = 256) -> None:
        self._data: OrderedDict[str, tuple[float, float, pd.DataFrame, bool]] = OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()

    def get(self, key: str, max_age_s: float | None = None) -> tuple[pd.DataFrame, bool] | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            stored, expires, frame, truncated = item
            now = time.monotonic()
            if expires < now:
                del self._data[key]
                return None
            if max_age_s is not None and now - stored > max_age_s:
                return None
            self._data.move_to_end(key)
            return frame.copy(), truncated

    def set(self, key: str, frame: pd.DataFrame, truncated: bool, ttl_s: int) -> None:
        with self._lock:
            now = time.monotonic()
            self._data[key] = (now, now + ttl_s, frame.copy(), truncated)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)


class RedisCache:
    """Shared across worker processes and servers."""

    def __init__(self, url: str, client: Any = None) -> None:
        if client is None:
            import redis

            client = redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
        self._r = client

    def get(self, key: str, max_age_s: float | None = None) -> tuple[pd.DataFrame, bool] | None:
        raw = self._r.get(key)
        if raw is None:
            return None
        stored, truncated, body = float(raw[:20]), raw[20:21] == b"1", raw[21:]
        if max_age_s is not None and time.time() - stored > max_age_s:
            return None
        return pd.read_parquet(io.BytesIO(body)), truncated

    def set(self, key: str, frame: pd.DataFrame, truncated: bool, ttl_s: int) -> None:
        buf = io.BytesIO()
        frame.to_parquet(buf)
        stamp = f"{time.time():020.6f}".encode()
        self._r.setex(key, ttl_s, stamp + (b"1" if truncated else b"0") + buf.getvalue())


def make_cache(url: str | None) -> ResultCache:
    return RedisCache(url) if url else MemoryCache()
