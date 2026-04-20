# Local HLS Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a built-in local HLS proxy on `127.0.0.1:2323` so every remote `.m3u8` URL plays through a shared proxy path that rewrites playlists, proxies dependent assets, repairs disguised PNG-plus-TS segments, applies conservative ad removal, and uses bounded in-memory cache plus short-range prefetching.

**Architecture:** Keep `PlayerWindow` and `MpvWidget` as the control plane and add a focused `src/atv_player/proxy/` package as the data plane. Build the feature in layers: first pure byte and playlist helpers, then session-aware segment and asset fetching, then a threaded local HTTP server, and finally app/player integration so the proxy is process-owned and all remote `.m3u8` playback defaults to localhost.

**Tech Stack:** Python 3.12+, PySide6, `httpx`, `cachetools`, stdlib `http.server`, `pytest`, `pytest-qt`

---

## File Map

- Create: `src/atv_player/proxy/__init__.py`
  Responsibility: export the public proxy entry points used by the playback layer.

- Create: `src/atv_player/proxy/adblock.py`
  Responsibility: conservative segment-level ad-detection rules based on duration and absolute URL.

- Create: `src/atv_player/proxy/stripper.py`
  Responsibility: repair disguised TS segment payloads by stripping PNG preambles, locating TS sync bytes, and realigning packet boundaries when safe.

- Create: `src/atv_player/proxy/cache.py`
  Responsibility: bounded TTL/LRU caches plus in-flight request deduplication for playlists, assets, and repaired segment bytes.

- Create: `src/atv_player/proxy/session.py`
  Responsibility: short-lived proxy session registry keyed by opaque token, storing origin playlist URL, outbound headers, and parsed playlist metadata.

- Create: `src/atv_player/proxy/m3u8.py`
  Responsibility: parse and rewrite media/master playlists plus dependent tag URIs into local proxy URLs.

- Create: `src/atv_player/proxy/segment.py`
  Responsibility: fetch, cache, repair, prefetch, and return segment or asset payloads using shared proxy session state.

- Create: `src/atv_player/proxy/server.py`
  Responsibility: threaded local HTTP server, route dispatch for `/m3u`, `/seg`, and `/asset`, and process-lifetime start/stop behavior on `127.0.0.1:2323`.

- Modify: `src/atv_player/player/m3u8_ad_filter.py`
  Responsibility: stop writing local cleaned playlist files and instead become the shared playback entry point that decides whether a remote URL should be proxied and returns a localhost proxy URL.

- Modify: `src/atv_player/ui/player_window.py`
  Responsibility: keep using the async playback-preparation path, but prepare remote `.m3u8` URLs through the local proxy and keep direct-play fallback plus player log output on failure.

- Modify: `src/atv_player/ui/main_window.py`
  Responsibility: inject a shared proxy-preparation service into `PlayerWindow` so the player window does not create its own server lifecycle implicitly.

- Modify: `src/atv_player/app.py`
  Responsibility: create one shared proxy-preparation service per app process, pass it into `MainWindow`, and shut it down cleanly when windows close or the process exits.

- Modify: `pyproject.toml`
  Responsibility: add `cachetools` dependency for TTL/LRU cache support.

- Create: `tests/test_hls_proxy_stripper.py`
  Responsibility: unit tests for PNG stripping, TS sync detection, fallback behavior, and packet alignment.

- Create: `tests/test_hls_proxy_m3u8.py`
  Responsibility: unit tests for media playlist rewrite, master playlist rewrite, dependent tag rewrite, and conservative ad removal.

- Create: `tests/test_hls_proxy_segment.py`
  Responsibility: unit tests for header propagation, cache hits, in-flight deduplication, repair, and prefetch scheduling.

- Create: `tests/test_hls_proxy_server.py`
  Responsibility: unit tests for session-backed proxy preparation and localhost URL generation without relying on a real external network.

- Modify: `tests/test_player_window_ui.py`
  Responsibility: assert that remote `.m3u8` items are prepared as `127.0.0.1:2323` proxy URLs and that prepare failures fall back to the original URL.

- Modify: `tests/test_app.py`
  Responsibility: assert that `AppCoordinator` provides one shared proxy-preparation service to the main window and shuts it down.

## Task 1: Add Proxy Primitives And Dependency Support

**Files:**
- Modify: `pyproject.toml`
- Create: `src/atv_player/proxy/__init__.py`
- Create: `src/atv_player/proxy/adblock.py`
- Create: `src/atv_player/proxy/stripper.py`
- Create: `tests/test_hls_proxy_stripper.py`

- [ ] **Step 1: Write the failing tests**

```python
from atv_player.proxy.adblock import is_ad_segment
from atv_player.proxy.stripper import repair_segment_bytes


def test_repair_segment_bytes_strips_png_preamble_and_returns_ts_sync() -> None:
    png_then_ts = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00" * 32
        b"IEND\xaeB`\x82"
        b"junk"
        + (b"\x47" + b"\x00" * 187) * 2
    )

    repaired = repair_segment_bytes(png_then_ts)

    assert repaired.startswith(b"\x47")
    assert len(repaired) == 376


def test_repair_segment_bytes_preserves_plain_ts_payload() -> None:
    plain_ts = (b"\x47" + b"\x01" * 187) * 3

    repaired = repair_segment_bytes(plain_ts)

    assert repaired == plain_ts


def test_repair_segment_bytes_falls_back_to_original_when_no_sync_found() -> None:
    payload = b"\x89PNG\r\n\x1a\nnot-ts"

    repaired = repair_segment_bytes(payload)

    assert repaired == payload


def test_is_ad_segment_uses_duration_and_url_signals_conservatively() -> None:
    assert is_ad_segment(0.5, "https://cdn.example/live/0001.ts") is True
    assert is_ad_segment(5.0, "https://media.example/video/adjump/0002.ts") is True
    assert is_ad_segment(5.0, "https://media.example/path/ad-0003.ts") is True
    assert is_ad_segment(5.0, "https://media.example/path/main-0004.ts") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hls_proxy_stripper.py -v`

Expected: `FAIL` with `ModuleNotFoundError` because `atv_player.proxy` modules do not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
# pyproject.toml
dependencies = [
  "PySide6>=6.11.0",
  "beautifulsoup4>=4.14.3",
  "cachetools>=5.5",
  "httpx>=0.28",
  "lxml>=6.0.4",
  "mpv>=1.0.6",
  "pycryptodome>=3.23.0",
  "pyquery>=2.0.1",
  "requests>=2.33.1",
]
```

```python
# src/atv_player/proxy/adblock.py
from __future__ import annotations


def is_ad_segment(duration: float | None, absolute_url: str) -> bool:
    candidate = absolute_url.lower()
    if duration is not None and duration < 1.0:
        return True
    return any(marker in candidate for marker in ("/adjump/", "/video/adjump/", "/ad-", "/ad/"))
```

```python
# src/atv_player/proxy/stripper.py
from __future__ import annotations

PNG_END = b"\x49\x45\x4E\x44\xAE\x42\x60\x82"
TS_SYNC = 0x47
TS_PACKET_SIZE = 188


def repair_segment_bytes(data: bytes) -> bytes:
    stripped = _strip_png_prefix(data)
    sync_index = stripped.find(bytes([TS_SYNC]))
    if sync_index < 0:
        return data
    candidate = stripped[sync_index:]
    aligned = _align_ts_packets(candidate)
    return aligned if aligned else candidate


def _strip_png_prefix(data: bytes) -> bytes:
    png_end_index = data.find(PNG_END)
    if png_end_index < 0:
        return data
    return data[png_end_index + len(PNG_END) :]


def _align_ts_packets(data: bytes) -> bytes:
    if len(data) < TS_PACKET_SIZE:
        return data
    for offset in range(min(TS_PACKET_SIZE, len(data))):
        if data[offset] != TS_SYNC:
            continue
        probe = data[offset : offset + TS_PACKET_SIZE * 2]
        if len(probe) >= TS_PACKET_SIZE * 2 and probe[TS_PACKET_SIZE] == TS_SYNC:
            trimmed = data[offset:]
            usable = len(trimmed) - (len(trimmed) % TS_PACKET_SIZE)
            return trimmed[:usable] if usable else trimmed
    return data
```

```python
# src/atv_player/proxy/__init__.py
from atv_player.proxy.adblock import is_ad_segment
from atv_player.proxy.stripper import repair_segment_bytes

__all__ = ["is_ad_segment", "repair_segment_bytes"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hls_proxy_stripper.py -v`

Expected: `PASS`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/atv_player/proxy/__init__.py src/atv_player/proxy/adblock.py src/atv_player/proxy/stripper.py tests/test_hls_proxy_stripper.py
git commit -m "feat: add hls proxy primitives"
```

## Task 2: Build Session Registry And Playlist Rewriter

**Files:**
- Create: `src/atv_player/proxy/session.py`
- Create: `src/atv_player/proxy/m3u8.py`
- Create: `tests/test_hls_proxy_m3u8.py`

- [ ] **Step 1: Write the failing tests**

```python
from atv_player.proxy.m3u8 import rewrite_playlist
from atv_player.proxy.session import ProxySessionRegistry


def test_rewrite_playlist_rewrites_media_segments_and_assets() -> None:
    registry = ProxySessionRegistry()
    token = registry.create_session(
        playlist_url="https://media.example/path/index.m3u8",
        headers={"Referer": "https://site.example"},
    )
    content = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="enc.key"
#EXT-X-MAP:URI="init.mp4"
#EXTINF:5.0,
main-0001.ts
#EXTINF:0.5,
ad-0002.ts
#EXTINF:5.0,
main-0003.ts
"""

    rewritten = rewrite_playlist(
        token=token,
        playlist_url="https://media.example/path/index.m3u8",
        content=content,
        session_registry=registry,
        proxy_base_url="http://127.0.0.1:2323",
    )

    assert 'URI="http://127.0.0.1:2323/asset?token=' in rewritten.text
    assert "http://127.0.0.1:2323/seg?token=" in rewritten.text
    assert "ad-0002.ts" not in rewritten.text
    session = registry.get(token)
    assert [segment.url for segment in session.segments] == [
        "https://media.example/path/main-0001.ts",
        "https://media.example/path/main-0003.ts",
    ]


def test_rewrite_playlist_rewrites_master_playlist_to_child_tokens() -> None:
    registry = ProxySessionRegistry()
    token = registry.create_session(
        playlist_url="https://media.example/master.m3u8",
        headers={},
    )
    content = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1280000
video/720.m3u8
"""

    rewritten = rewrite_playlist(
        token=token,
        playlist_url="https://media.example/master.m3u8",
        content=content,
        session_registry=registry,
        proxy_base_url="http://127.0.0.1:2323",
    )

    assert "http://127.0.0.1:2323/m3u?token=" in rewritten.text
    assert rewritten.is_master is True


def test_proxy_session_registry_expires_stale_sessions() -> None:
    registry = ProxySessionRegistry(ttl_seconds=5.0)
    token = registry.create_session("https://media.example/master.m3u8", {})

    registry.expire_stale(now=registry.get(token).created_at + 6.0)

    assert registry.contains(token) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hls_proxy_m3u8.py -v`

Expected: `FAIL` because `ProxySessionRegistry`, `rewrite_playlist()`, and `expire_stale()` do not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/proxy/session.py
from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import time


@dataclass(slots=True)
class PlaylistSegment:
    index: int
    url: str
    duration: float | None = None


@dataclass(slots=True)
class ProxySession:
    token: str
    playlist_url: str
    headers: dict[str, str]
    segments: list[PlaylistSegment] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)


class ProxySessionRegistry:
    def __init__(self, ttl_seconds: float = 900.0) -> None:
        self._sessions: dict[str, ProxySession] = {}
        self._ttl_seconds = ttl_seconds

    def create_session(self, playlist_url: str, headers: dict[str, str]) -> str:
        token = secrets.token_urlsafe(9)
        self._sessions[token] = ProxySession(token=token, playlist_url=playlist_url, headers=dict(headers))
        return token

    def get(self, token: str) -> ProxySession:
        session = self._sessions[token]
        session.last_accessed_at = time.time()
        return session

    def contains(self, token: str) -> bool:
        return token in self._sessions

    def expire_stale(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - self._ttl_seconds
        expired_tokens = [
            token for token, session in self._sessions.items() if session.last_accessed_at < cutoff
        ]
        for token in expired_tokens:
            self._sessions.pop(token, None)
```

```python
# src/atv_player/proxy/m3u8.py
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urljoin
import re

from atv_player.proxy.adblock import is_ad_segment
from atv_player.proxy.session import PlaylistSegment, ProxySessionRegistry

_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')


@dataclass(slots=True, frozen=True)
class RewrittenPlaylist:
    text: str
    is_master: bool


def rewrite_playlist(*, token: str, playlist_url: str, content: str, session_registry: ProxySessionRegistry, proxy_base_url: str) -> RewrittenPlaylist:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    session = session_registry.get(token)
    session.segments = []
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        output: list[str] = []
        for line in lines:
            if line.startswith("#"):
                output.append(line)
                continue
            child_url = urljoin(playlist_url, line)
            child_token = session_registry.create_session(child_url, session.headers)
            output.append(f"{proxy_base_url}/m3u?token={quote(child_token)}")
        return RewrittenPlaylist(text="\n".join(output) + "\n", is_master=True)

    output = []
    pending_duration: float | None = None
    segment_index = 0
    for line in lines:
        if line.startswith("#EXTINF:"):
            pending_duration = float(line.split(":", 1)[1].split(",", 1)[0])
            output.append(line)
            continue
        if line.startswith("#"):
            output.append(_rewrite_tag_uris(line, token, playlist_url, proxy_base_url))
            continue
        absolute_url = urljoin(playlist_url, line)
        if is_ad_segment(pending_duration, absolute_url):
            output.pop()
            pending_duration = None
            continue
        session.segments.append(PlaylistSegment(index=segment_index, url=absolute_url, duration=pending_duration))
        output.append(f"{proxy_base_url}/seg?token={quote(token)}&i={segment_index}")
        segment_index += 1
        pending_duration = None
    return RewrittenPlaylist(text="\n".join(output) + "\n", is_master=False)


def _rewrite_tag_uris(line: str, token: str, playlist_url: str, proxy_base_url: str) -> str:
    def repl(match: re.Match[str]) -> str:
        absolute_url = urljoin(playlist_url, match.group(1))
        return f'URI="{proxy_base_url}/asset?token={quote(token)}&url={quote(absolute_url, safe="")}"'

    return _URI_ATTR_RE.sub(repl, line)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hls_proxy_m3u8.py -v`

Expected: `PASS`

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/proxy/session.py src/atv_player/proxy/m3u8.py tests/test_hls_proxy_m3u8.py
git commit -m "feat: rewrite proxied hls playlists"
```

## Task 3: Add Segment Fetching, Cache, And Prefetch

**Files:**
- Create: `src/atv_player/proxy/cache.py`
- Create: `src/atv_player/proxy/segment.py`
- Create: `tests/test_hls_proxy_segment.py`

- [ ] **Step 1: Write the failing tests**

```python
from atv_player.proxy.segment import SegmentProxy
from atv_player.proxy.session import PlaylistSegment, ProxySessionRegistry


def test_segment_proxy_repairs_bytes_and_reuses_cache(tmp_path) -> None:
    requests: list[str] = []

    class FakeResponse:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool) -> FakeResponse:
        requests.append(url)
        return FakeResponse(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20 + b"IEND\xaeB`\x82" + (b"\x47" + b"\x00" * 187))

    registry = ProxySessionRegistry()
    token = registry.create_session("https://media.example/path/index.m3u8", {"Referer": "https://site.example"})
    registry.get(token).segments = [PlaylistSegment(index=0, url="https://media.example/path/0001.png", duration=5.0)]
    proxy = SegmentProxy(session_registry=registry, get=fake_get)

    first = proxy.fetch_segment(token, 0)
    second = proxy.fetch_segment(token, 0)

    assert first.startswith(b"\x47")
    assert second == first
    assert requests == ["https://media.example/path/0001.png"]


def test_segment_proxy_schedules_prefetch_for_next_segments() -> None:
    scheduled: list[tuple[str, int]] = []

    registry = ProxySessionRegistry()
    token = registry.create_session("https://media.example/path/index.m3u8", {})
    registry.get(token).segments = [
        PlaylistSegment(index=0, url="https://media.example/path/0001.ts", duration=5.0),
        PlaylistSegment(index=1, url="https://media.example/path/0002.ts", duration=5.0),
        PlaylistSegment(index=2, url="https://media.example/path/0003.ts", duration=5.0),
    ]
    proxy = SegmentProxy(session_registry=registry)
    proxy._prefetch_segment = lambda session_token, segment_index: scheduled.append((session_token, segment_index))

    proxy.schedule_prefetch(token, 0)

    assert scheduled == [(token, 1), (token, 2)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hls_proxy_segment.py -v`

Expected: `FAIL` because `SegmentProxy` and cache helpers do not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/proxy/cache.py
from __future__ import annotations

from cachetools import TTLCache
import threading


class ProxyCache:
    def __init__(self) -> None:
        self.segment_bytes = TTLCache(maxsize=200, ttl=60)
        self.asset_bytes = TTLCache(maxsize=64, ttl=300)
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()

    def get_segment(self, key: str) -> bytes | None:
        return self.segment_bytes.get(key)

    def set_segment(self, key: str, value: bytes) -> None:
        self.segment_bytes[key] = value

    def mark_in_flight(self, key: str) -> bool:
        with self._lock:
            if key in self._in_flight:
                return False
            self._in_flight.add(key)
            return True

    def clear_in_flight(self, key: str) -> None:
        with self._lock:
            self._in_flight.discard(key)
```

```python
# src/atv_player/proxy/segment.py
from __future__ import annotations

from hashlib import sha256
import threading
import httpx

from atv_player.proxy.cache import ProxyCache
from atv_player.proxy.session import ProxySessionRegistry
from atv_player.proxy.stripper import repair_segment_bytes


class SegmentProxy:
    def __init__(self, session_registry: ProxySessionRegistry, get=httpx.get, cache: ProxyCache | None = None) -> None:
        self._session_registry = session_registry
        self._get = get
        self._cache = cache or ProxyCache()

    def fetch_segment(self, token: str, index: int) -> bytes:
        session = self._session_registry.get(token)
        segment = session.segments[index]
        cache_key = self._segment_cache_key(segment.url, session.headers)
        cached = self._cache.get_segment(cache_key)
        if cached is not None:
            return cached
        response = self._get(segment.url, headers=session.headers, timeout=10.0, follow_redirects=True)
        response.raise_for_status()
        repaired = repair_segment_bytes(bytes(response.content))
        self._cache.set_segment(cache_key, repaired)
        self.schedule_prefetch(token, index)
        return repaired

    def fetch_asset(self, token: str, url: str) -> bytes:
        session = self._session_registry.get(token)
        response = self._get(url, headers=session.headers, timeout=10.0, follow_redirects=True)
        response.raise_for_status()
        return bytes(response.content)

    def schedule_prefetch(self, token: str, current_index: int) -> None:
        session = self._session_registry.get(token)
        for next_index in range(current_index + 1, min(current_index + 3, len(session.segments))):
            self._prefetch_segment(token, next_index)

    def _prefetch_segment(self, token: str, segment_index: int) -> None:
        threading.Thread(
            target=lambda: self.fetch_segment(token, segment_index),
            daemon=True,
        ).start()

    @staticmethod
    def _segment_cache_key(url: str, headers: dict[str, str]) -> str:
        return sha256(f"{url}|{sorted(headers.items())}".encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hls_proxy_segment.py -v`

Expected: `PASS`

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/proxy/cache.py src/atv_player/proxy/segment.py tests/test_hls_proxy_segment.py
git commit -m "feat: add hls segment proxy cache"
```

## Task 4: Add Threaded Local Proxy Server And Shared Proxy Preparation Service

**Files:**
- Create: `src/atv_player/proxy/server.py`
- Modify: `src/atv_player/player/m3u8_ad_filter.py`
- Create: `tests/test_hls_proxy_server.py`

- [ ] **Step 1: Write the failing tests**

```python
from atv_player.player.m3u8_ad_filter import M3U8AdFilter


def test_m3u8_ad_filter_returns_proxy_url_for_remote_m3u8() -> None:
    class FakeServer:
        def start(self) -> None:
            return None

        def create_playlist_url(self, url: str, headers: dict[str, str] | None = None) -> str:
            assert headers == {"Referer": "https://site.example"}
            return "http://127.0.0.1:2323/m3u?token=test-token"

        def close(self) -> None:
            return None

    ad_filter = M3U8AdFilter(proxy_server=FakeServer())

    prepared = ad_filter.prepare(
        "https://media.example/path/index.m3u8",
        {"Referer": "https://site.example"},
    )

    assert prepared == "http://127.0.0.1:2323/m3u?token=test-token"


def test_m3u8_ad_filter_leaves_non_m3u8_url_unchanged() -> None:
    ad_filter = M3U8AdFilter()

    assert ad_filter.should_prepare("https://media.example/video.mp4") is False


def test_local_hls_proxy_server_returns_404_for_missing_token() -> None:
    server = LocalHlsProxyServer()

    status, headers, body = server.handle_request("GET", "/m3u?token=missing")

    assert status == 404
    assert body == b"missing proxy session"


def test_local_hls_proxy_server_returns_502_when_playlist_fetch_fails() -> None:
    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool):
        raise RuntimeError("origin down")

    server = LocalHlsProxyServer(get=fake_get)
    playlist_url = server.create_playlist_url("https://media.example/path/index.m3u8", {})
    token = playlist_url.rsplit("=", 1)[-1]

    status, headers, body = server.handle_request("GET", f"/m3u?token={token}")

    assert status == 502
    assert body == b"origin down"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hls_proxy_server.py -v`

Expected: `FAIL` because `M3U8AdFilter` still writes local playlists instead of returning proxy URLs, and the local proxy server abstraction plus testable request handler does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/proxy/server.py
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
import threading

from atv_player.proxy.m3u8 import rewrite_playlist
from atv_player.proxy.segment import SegmentProxy
from atv_player.proxy.session import ProxySessionRegistry


class LocalHlsProxyServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 2323, get=httpx.get) -> None:
        self.host = host
        self.port = port
        self._registry = ProxySessionRegistry()
        self._segment_proxy = SegmentProxy(self._registry, get=get)
        self._get = get
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = ThreadingHTTPServer((self.host, self.port), self._handler_type())
        self._server.proxy_server = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None

    def create_playlist_url(self, url: str, headers: dict[str, str] | None = None) -> str:
        self.start()
        token = self._registry.create_session(url, dict(headers or {}))
        return f"http://{self.host}:{self.port}/m3u?token={quote(token)}"

    def handle_request(self, method: str, path: str) -> tuple[int, list[tuple[str, str]], bytes]:
        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        try:
            if method != "GET":
                return 405, [], b"method not allowed"
            if parsed.path == "/m3u":
                token = query["token"][0]
                if not self._registry.contains(token):
                    return 404, [], b"missing proxy session"
                session = self._registry.get(token)
                response = self._get(session.playlist_url, headers=session.headers, timeout=10.0, follow_redirects=True)
                response.raise_for_status()
                rewritten = rewrite_playlist(
                    token=token,
                    playlist_url=session.playlist_url,
                    content=response.text,
                    session_registry=self._registry,
                    proxy_base_url=f"http://{self.host}:{self.port}",
                )
                payload = rewritten.text.encode("utf-8")
                return 200, [("Content-Type", "application/vnd.apple.mpegurl")], payload
            if parsed.path == "/seg":
                token = query["token"][0]
                if not self._registry.contains(token):
                    return 404, [], b"missing proxy session"
                index = int(query["i"][0])
                payload = self._segment_proxy.fetch_segment(token, index)
                return 200, [("Content-Type", "video/MP2T")], payload
            if parsed.path == "/asset":
                token = query["token"][0]
                if not self._registry.contains(token):
                    return 404, [], b"missing proxy session"
                url = query["url"][0]
                payload = self._segment_proxy.fetch_asset(token, url)
                return 200, [], payload
        except Exception as exc:
            return 502, [], str(exc).encode("utf-8")
        return 404, [], b"not found"

    def _handler_type(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                status, headers, payload = parent.handle_request("GET", self.path)
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args) -> None:
                return None

        return Handler
```

```python
# src/atv_player/player/m3u8_ad_filter.py
from __future__ import annotations

from urllib.parse import urlparse

from atv_player.proxy.server import LocalHlsProxyServer


class M3U8AdFilter:
    def __init__(self, proxy_server: LocalHlsProxyServer | None = None) -> None:
        self._proxy_server = proxy_server or LocalHlsProxyServer()

    def should_prepare(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and ".m3u8" in url.lower()

    def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
        if not self.should_prepare(url):
            return url
        return self._proxy_server.create_playlist_url(url, headers=dict(headers or {}))

    def close(self) -> None:
        self._proxy_server.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hls_proxy_server.py -v`

Expected: `PASS`

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/proxy/server.py src/atv_player/player/m3u8_ad_filter.py tests/test_hls_proxy_server.py
git commit -m "feat: add local hls proxy server"
```

## Task 5: Integrate Remote M3U8 Playback Through Localhost In PlayerWindow

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_player_window_rewrites_remote_m3u8_to_local_proxy_url(qtbot) -> None:
    class FakeProxyFilter:
        def should_prepare(self, url: str) -> bool:
            return url.endswith(".m3u8")

        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            return "http://127.0.0.1:2323/m3u?token=proxy-1"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="正片", url="https://media.example/path/index.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=FakeProxyFilter())
    video = RecordingVideo()
    qtbot.addWidget(window)
    window.video = video

    window.open_session(session)
    qtbot.waitUntil(lambda: video.load_calls == [("http://127.0.0.1:2323/m3u?token=proxy-1", 0)])


def test_player_window_logs_proxy_prepare_failure_and_plays_original_url(qtbot) -> None:
    class FailingProxyFilter:
        def should_prepare(self, url: str) -> bool:
            return True

        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            raise RuntimeError("port 2323 busy")

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="正片", url="https://media.example/path/index.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=FailingProxyFilter())
    video = RecordingVideo()
    qtbot.addWidget(window)
    window.video = video

    window.open_session(session)
    qtbot.waitUntil(lambda: video.load_calls == [("https://media.example/path/index.m3u8", 0)])
    assert "port 2323 busy" in window.log_view.toPlainText()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_rewrites_remote_m3u8_to_local_proxy_url tests/test_player_window_ui.py::test_player_window_logs_proxy_prepare_failure_and_plays_original_url -v`

Expected: `FAIL` because the current player path still expects playlist filtering behavior and does not log localhost proxy startup failures in a proxy-specific flow.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/ui/player_window.py
def _handle_playback_prepare_failed(self, request_id: int, message: str) -> None:
    if request_id != self._playback_prepare_request_id:
        return
    pending_prepare = self._pending_playback_prepare
    self._pending_playback_prepare = None
    if pending_prepare is None:
        return
    if self.session is None or self.current_index != pending_prepare.index:
        return
    self._append_log(f"播放代理失败，继续播放原地址: {message}")
    try:
        self._start_current_item_playback(
            start_position_seconds=pending_prepare.start_position_seconds,
            pause=pending_prepare.pause,
        )
    except Exception as exc:
        self._restore_current_index(pending_prepare.previous_index)
        self._append_log(f"播放失败: {exc}")
```

```python
# src/atv_player/ui/player_window.py
def _handle_playback_prepare_succeeded(self, request_id: int, prepared_url: str) -> None:
    if request_id != self._playback_prepare_request_id:
        return
    pending_prepare = self._pending_playback_prepare
    self._pending_playback_prepare = None
    if pending_prepare is None:
        return
    if self.session is None or self.current_index != pending_prepare.index:
        return
    current_item = self.session.playlist[self.current_index]
    current_item.url = prepared_url
    self._start_current_item_playback(
        start_position_seconds=pending_prepare.start_position_seconds,
        pause=pending_prepare.pause,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_rewrites_remote_m3u8_to_local_proxy_url tests/test_player_window_ui.py::test_player_window_logs_proxy_prepare_failure_and_plays_original_url -v`

Expected: `PASS`

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: route player window m3u8 playback through proxy"
```

## Task 6: Give The App One Shared Proxy Lifecycle

**Files:**
- Modify: `src/atv_player/ui/main_window.py`
- Modify: `src/atv_player/app.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_app_coordinator_passes_shared_m3u8_filter_into_main_window(monkeypatch) -> None:
    repo = FakeRepoWithToken()
    captured_filters: list[object] = []

    class DummyMainWindow:
        def __init__(self, *args, m3u8_ad_filter=None, **kwargs) -> None:
            captured_filters.append(m3u8_ad_filter)

        def show(self) -> None:
            return None

    monkeypatch.setattr("atv_player.app.MainWindow", DummyMainWindow)

    coordinator = AppCoordinator(repo)
    coordinator._build_api_client = lambda: FakeApiClient()
    coordinator._load_capabilities = lambda client: {"emby": False, "jellyfin": False}

    coordinator._show_main()

    assert captured_filters[0] is coordinator._m3u8_ad_filter


def test_app_coordinator_closes_m3u8_filter_when_shutting_down() -> None:
    coordinator = AppCoordinator(FakeRepo())

    class DummyFilter:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    coordinator._m3u8_ad_filter = DummyFilter()
    coordinator.close()

    assert coordinator._m3u8_ad_filter.closed is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_app.py::test_app_coordinator_passes_shared_m3u8_filter_into_main_window tests/test_app.py::test_app_coordinator_closes_m3u8_filter_when_shutting_down -v`

Expected: `FAIL` because `AppCoordinator` does not own a shared `M3U8AdFilter`, `MainWindow` does not accept it, and shutdown does not close it.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/ui/main_window.py
def __init__(
    self,
    browse_controller,
    history_controller,
    player_controller,
    config,
    save_config=None,
    douban_controller=None,
    telegram_controller=None,
    live_controller=None,
    live_source_manager=None,
    emby_controller=None,
    jellyfin_controller=None,
    spider_plugins=None,
    plugin_manager=None,
    drive_detail_loader=None,
    show_emby_tab: bool = True,
    show_jellyfin_tab: bool = True,
    m3u8_ad_filter=None,
) -> None:
    super().__init__()
    self._save_config = save_config or (lambda: None)
    self._m3u8_ad_filter = m3u8_ad_filter
```

```python
# src/atv_player/ui/main_window.py
def _apply_open_player(self, request, session, restore_paused_state: bool = False) -> None:
    if self.player_window is None:
        self.player_window = PlayerWindow(
            self.player_controller,
            self.config,
            self._save_config,
            m3u8_ad_filter=self._m3u8_ad_filter,
        )
```

```python
# src/atv_player/app.py
from atv_player.player.m3u8_ad_filter import M3U8AdFilter


class AppCoordinator(QObject):
    def __init__(self, repo: SettingsRepository) -> None:
        super().__init__()
        self.repo = repo
        self.login_window: LoginWindow | None = None
        self.main_window: MainWindow | None = None
        self._api_client: ApiClient | None = None
        self._m3u8_ad_filter = M3U8AdFilter()

    def close(self) -> None:
        close_filter = getattr(self._m3u8_ad_filter, "close", None)
        if callable(close_filter):
            close_filter()
```

```python
# src/atv_player/app.py
self.main_window = MainWindow(
    browse_controller=browse_controller,
    history_controller=history_controller,
    player_controller=player_controller,
    config=config,
    save_config=lambda: self.repo.save_config(config),
    douban_controller=douban_controller,
    telegram_controller=telegram_controller,
    live_controller=live_controller,
    live_source_manager=live_source_manager,
    emby_controller=emby_controller,
    jellyfin_controller=jellyfin_controller,
    spider_plugins=spider_plugins,
    plugin_manager=self._plugin_manager,
    drive_detail_loader=drive_detail_loader,
    show_emby_tab=bool(capabilities.get("emby")),
    show_jellyfin_tab=bool(capabilities.get("jellyfin")),
    m3u8_ad_filter=self._m3u8_ad_filter,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_app.py::test_app_coordinator_passes_shared_m3u8_filter_into_main_window tests/test_app.py::test_app_coordinator_closes_m3u8_filter_when_shutting_down -v`

Expected: `PASS`

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/ui/main_window.py src/atv_player/app.py tests/test_app.py
git commit -m "feat: share proxy lifecycle across app windows"
```

## Task 7: Final Proxy Regression Sweep

**Files:**
- Modify: `src/atv_player/proxy/m3u8.py`
- Modify: `src/atv_player/proxy/segment.py`
- Modify: `tests/test_hls_proxy_m3u8.py`
- Modify: `tests/test_hls_proxy_segment.py`

- [ ] **Step 1: Write the failing regression tests**

```python
def test_rewrite_playlist_keeps_non_ad_short_tags_and_redundant_discontinuities_stable() -> None:
    registry = ProxySessionRegistry()
    token = registry.create_session("https://media.example/path/index.m3u8", {})
    content = """#EXTM3U
#EXTINF:5.0,
main-0001.ts
#EXT-X-DISCONTINUITY
#EXTINF:5.0,
main-0002.ts
"""

    rewritten = rewrite_playlist(
        token=token,
        playlist_url="https://media.example/path/index.m3u8",
        content=content,
        session_registry=registry,
        proxy_base_url="http://127.0.0.1:2323",
    )

    assert rewritten.text.count("#EXT-X-DISCONTINUITY") == 1


def test_segment_proxy_uses_session_headers_for_asset_fetch() -> None:
    seen_headers: list[dict[str, str]] = []

    class FakeResponse:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool) -> FakeResponse:
        seen_headers.append(headers)
        return FakeResponse(b"key-bytes")

    registry = ProxySessionRegistry()
    token = registry.create_session("https://media.example/path/index.m3u8", {"Referer": "https://site.example"})
    proxy = SegmentProxy(session_registry=registry, get=fake_get)

    payload = proxy.fetch_asset(token, "https://media.example/path/key.bin")

    assert payload == b"key-bytes"
    assert seen_headers == [{"Referer": "https://site.example"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hls_proxy_m3u8.py tests/test_hls_proxy_segment.py -v`

Expected: at least one `FAIL` because the initial implementation does not yet fully preserve discontinuity behavior and asset-header propagation coverage.

- [ ] **Step 3: Write minimal implementation**

```python
# src/atv_player/proxy/m3u8.py
def _drop_redundant_discontinuities(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for index, line in enumerate(lines):
        if line != "#EXT-X-DISCONTINUITY":
            cleaned.append(line)
            continue
        before = any(not value.startswith("#") for value in cleaned[-2:])
        after = any(not candidate.startswith("#") for candidate in lines[index + 1 :])
        if before and after:
            cleaned.append(line)
    return cleaned
```

```python
# src/atv_player/proxy/segment.py
def fetch_asset(self, token: str, url: str) -> bytes:
    session = self._session_registry.get(token)
    response = self._get(url, headers=dict(session.headers), timeout=10.0, follow_redirects=True)
    response.raise_for_status()
    return bytes(response.content)
```

- [ ] **Step 4: Run the focused suite to verify it passes**

Run: `uv run pytest tests/test_hls_proxy_stripper.py tests/test_hls_proxy_m3u8.py tests/test_hls_proxy_segment.py tests/test_hls_proxy_server.py tests/test_player_window_ui.py -k 'proxy or m3u8 or stripper' -v`

Expected: `PASS`

- [ ] **Step 5: Run the broader integration checks**

Run: `uv run pytest tests/test_player_window_ui.py tests/test_app.py -v`

Expected: `PASS`

- [ ] **Step 6: Commit**

```bash
git add src/atv_player/proxy/m3u8.py src/atv_player/proxy/segment.py tests/test_hls_proxy_m3u8.py tests/test_hls_proxy_segment.py tests/test_player_window_ui.py tests/test_app.py
git commit -m "feat: finalize local hls proxy regressions"
```
