from __future__ import annotations

import threading

from cachetools import TTLCache


class ProxyCache:
    def __init__(self) -> None:
        self._segment_bytes = TTLCache(maxsize=200, ttl=60)
        self._asset_bytes = TTLCache(maxsize=64, ttl=300)
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()

    def get_segment(self, key: str) -> bytes | None:
        with self._lock:
            return self._segment_bytes.get(key)

    def set_segment(self, key: str, value: bytes) -> None:
        with self._lock:
            self._segment_bytes[key] = value

    def get_asset(self, key: str) -> bytes | None:
        with self._lock:
            return self._asset_bytes.get(key)

    def set_asset(self, key: str, value: bytes) -> None:
        with self._lock:
            self._asset_bytes[key] = value

    def mark_in_flight(self, key: str) -> bool:
        with self._lock:
            if key in self._in_flight:
                return False
            self._in_flight.add(key)
            return True

    def clear_in_flight(self, key: str) -> None:
        with self._lock:
            self._in_flight.discard(key)
