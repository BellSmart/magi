"""进程内 TTL 缓存。

同一问题常被反复问（尤其演示时），命中缓存能把检索费用直接降为零。
多 worker 下每个进程各有一份，命中率按 worker 均摊——对当前规模足够，
需要共享时换 Redis 即可，接口不变。
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any


class TTLCache:
    def __init__(self, ttl_seconds: float, max_entries: int = 512) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < time.monotonic():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._data) >= self._max:
                # 简单淘汰：清掉已过期的；仍满则丢最早写入的。
                now = time.monotonic()
                for stale in [k for k, (exp, _) in self._data.items() if exp < now]:
                    self._data.pop(stale, None)
                if len(self._data) >= self._max:
                    self._data.pop(next(iter(self._data)), None)
            self._data[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
