from __future__ import annotations

import threading

from cachetools import TTLCache


class ProxyCache:
    def __init__(self) -> None:
        self._segment_bytes = TTLCache(maxsize=200, ttl=60)
        self._asset_bytes = TTLCache(maxsize=64, ttl=300)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._in_flight: set[str] = set()

    def get_segment(self, key: str) -> bytes | None:
        with self._lock:
            return self._segment_bytes.get(key)

    def set_segment(self, key: str, value: bytes) -> None:
        with self._condition:
            self._segment_bytes[key] = value
            self._condition.notify_all()

    def get_asset(self, key: str) -> bytes | None:
        with self._lock:
            return self._asset_bytes.get(key)

    def set_asset(self, key: str, value: bytes) -> None:
        with self._lock:
            self._asset_bytes[key] = value

    def mark_in_flight(self, key: str) -> bool:
        with self._condition:
            if key in self._in_flight:
                return False
            self._in_flight.add(key)
            return True

    def clear_in_flight(self, key: str) -> None:
        with self._condition:
            self._in_flight.discard(key)
            self._condition.notify_all()

    def wait_for_segment(self, key: str, timeout: float) -> bytes | None:
        with self._condition:
            self._condition.wait_for(
                lambda: key not in self._in_flight or key in self._segment_bytes,
                timeout=timeout,
            )
            return self._segment_bytes.get(key)
