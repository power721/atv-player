from __future__ import annotations

from hashlib import sha256
import threading

import httpx

from atv_player.proxy.cache import ProxyCache
from atv_player.proxy.session import ProxySessionRegistry
from atv_player.proxy.stripper import repair_segment_bytes


class SegmentProxy:
    def __init__(
        self,
        session_registry: ProxySessionRegistry,
        get=httpx.get,
        cache: ProxyCache | None = None,
    ) -> None:
        self._session_registry = session_registry
        self._get = get
        self._cache = cache or ProxyCache()

    def fetch_segment(self, token: str, index: int, *, prefetch: bool = False) -> bytes:
        session = self._session_registry.get(token)
        if session is None:
            raise ValueError("missing proxy session")
        segment = session.segments[index]
        cache_key = self._segment_cache_key(segment.url, session.headers)
        cached = self._cache.get_segment(cache_key)
        if cached is not None:
            return cached
        response = self._get(
            segment.url,
            headers=dict(session.headers),
            timeout=10.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        repaired = repair_segment_bytes(bytes(response.content))
        self._cache.set_segment(cache_key, repaired)
        if not prefetch:
            self.schedule_prefetch(token, index)
        return repaired

    def fetch_asset(self, token: str, url: str) -> bytes:
        session = self._session_registry.get(token)
        if session is None:
            raise ValueError("missing proxy session")
        response = self._get(
            url,
            headers=dict(session.headers),
            timeout=10.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        return bytes(response.content)

    def schedule_prefetch(self, token: str, current_index: int) -> None:
        session = self._session_registry.get(token)
        if session is None:
            return
        for next_index in range(current_index + 1, min(current_index + 3, len(session.segments))):
            self._prefetch_segment(token, next_index)

    def _prefetch_segment(self, token: str, segment_index: int) -> None:
        threading.Thread(
            target=lambda: self.fetch_segment(token, segment_index, prefetch=True),
            daemon=True,
        ).start()

    @staticmethod
    def _segment_cache_key(url: str, headers: dict[str, str]) -> str:
        return sha256(f"{url}|{sorted(headers.items())}".encode("utf-8")).hexdigest()
