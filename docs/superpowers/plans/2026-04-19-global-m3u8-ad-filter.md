# Global M3U8 Ad Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a conservative shared M3U8 ad filter that removes explicit `/adjump/` ad segments for all playback sources before mpv starts playback, while always falling back to the original URL on failure.

**Architecture:** Implement a focused `M3U8AdFilter` service in the shared playback layer. First build and test the pure playlist rewrite logic, then add remote fetch plus local cache-file output, and finally integrate the service into `PlayerWindow`'s async playback preparation path so all sources benefit without controller-specific changes.

**Tech Stack:** Python 3.12, PySide6, `httpx`, `pytest`, `pytest-qt`

---

## File Map

- Create: `src/atv_player/player/m3u8_ad_filter.py`
  Responsibility: pure M3U8 rewrite helpers plus a small service object that fetches remote playlists and writes cleaned local `.m3u8` files.

- Modify: `src/atv_player/ui/player_window.py`
  Responsibility: inject the shared ad-filter service, run it asynchronously before playback, and fall back to the original URL when filtering fails.

- Create: `tests/test_m3u8_ad_filter.py`
  Responsibility: unit tests for explicit ad-segment deletion, master-playlist pass-through, fetch behavior, and temporary-file output.

- Modify: `tests/test_player_window_ui.py`
  Responsibility: UI-level playback tests that prove the shared player path uses the ad filter before handing the final URL to the video widget.

## Task 1: Rewrite Media Playlists By Explicit Ad Signature

**Files:**
- Create: `src/atv_player/player/m3u8_ad_filter.py`
- Create: `tests/test_m3u8_ad_filter.py`

- [ ] **Step 1: Write the failing test**

```python
from atv_player.player.m3u8_ad_filter import rewrite_media_playlist


def test_rewrite_media_playlist_removes_explicit_adjumps_and_redundant_discontinuities() -> None:
    playlist = """#EXTM3U
#EXTINF:4.170833,
0000073.ts
#EXTINF:5.171833,
0000074.ts
#EXT-X-DISCONTINUITY
#EXTINF:3,
/video/adjump/time/17739416073640000000.ts
#EXTINF:2,
/video/adjump/time/17739416073640000001.ts
#EXT-X-DISCONTINUITY
#EXTINF:1.042711,
0000075.ts
"""

    result = rewrite_media_playlist(
        playlist,
        "https://media.example/path/index.m3u8",
    )

    assert result.changed is True
    assert "/video/adjump/" not in result.text
    assert result.text.count("#EXT-X-DISCONTINUITY") == 0
    assert "https://media.example/path/0000073.ts" in result.text
    assert "https://media.example/path/0000074.ts" in result.text
    assert "https://media.example/path/0000075.ts" in result.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_m3u8_ad_filter.py::test_rewrite_media_playlist_removes_explicit_adjumps_and_redundant_discontinuities -v`

Expected: `FAIL` with `ModuleNotFoundError` or `ImportError` because `atv_player.player.m3u8_ad_filter` and `rewrite_media_playlist` do not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

_AD_MARKERS = ("/adjump/", "/video/adjump/")


@dataclass(slots=True, frozen=True)
class PlaylistRewriteResult:
    text: str
    changed: bool
    is_master_playlist: bool = False


def _is_ad_segment(line: str) -> bool:
    return any(marker in line for marker in _AD_MARKERS)


def _is_media_uri(line: str) -> bool:
    return bool(line) and not line.startswith("#")


def _remove_redundant_discontinuities(lines: list[str]) -> tuple[list[str], bool]:
    changed = False
    cleaned: list[str] = []
    media_seen = False
    for index, line in enumerate(lines):
        if line != "#EXT-X-DISCONTINUITY":
            cleaned.append(line)
            media_seen = media_seen or _is_media_uri(line)
            continue
        media_ahead = any(_is_media_uri(candidate) for candidate in lines[index + 1 :])
        if media_seen and media_ahead:
            cleaned.append(line)
            continue
        changed = True
    return cleaned, changed


def rewrite_media_playlist(text: str, playlist_url: str) -> PlaylistRewriteResult:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        return PlaylistRewriteResult(text=text, changed=False, is_master_playlist=True)
    output: list[str] = []
    changed = False
    pending_extinf: str | None = None

    for line in lines:
        if line.startswith("#EXTINF:"):
            pending_extinf = line
            continue
        if line.startswith("#"):
            output.append(line)
            continue
        absolute_line = urljoin(playlist_url, line)
        if _is_ad_segment(absolute_line):
            changed = True
            pending_extinf = None
            continue
        if pending_extinf is not None:
            output.append(pending_extinf)
            pending_extinf = None
        output.append(absolute_line)

    output, removed_discontinuity = _remove_redundant_discontinuities(output)
    changed = changed or removed_discontinuity
    return PlaylistRewriteResult(
        text="\n".join(output) + "\n",
        changed=changed,
        is_master_playlist=False,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_m3u8_ad_filter.py::test_rewrite_media_playlist_removes_explicit_adjumps_and_redundant_discontinuities -v`

Expected: `PASS`

- [ ] **Step 5: Commit**

```bash
git add tests/test_m3u8_ad_filter.py src/atv_player/player/m3u8_ad_filter.py
git commit -m "feat: rewrite explicit m3u8 ad segments"
```

## Task 2: Fetch Remote M3U8 Playlists And Materialize Cleaned Local Files

**Files:**
- Modify: `src/atv_player/player/m3u8_ad_filter.py`
- Modify: `tests/test_m3u8_ad_filter.py`

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path

from atv_player.player.m3u8_ad_filter import M3U8AdFilter


def test_m3u8_ad_filter_writes_cleaned_playlist_to_cache(tmp_path: Path) -> None:
    requests: list[tuple[str, dict[str, str], float, bool]] = []

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool) -> FakeResponse:
        requests.append((url, headers, timeout, follow_redirects))
        return FakeResponse(
            """#EXTM3U
#EXTINF:4.0,
0001.ts
#EXTINF:2.0,
/video/adjump/time/0002.ts
#EXTINF:4.0,
0003.ts
"""
        )

    ad_filter = M3U8AdFilter(cache_dir=tmp_path, get=fake_get)

    prepared = ad_filter.prepare(
        "https://media.example/path/index.m3u8",
        {"Referer": "https://site.example"},
    )

    prepared_path = Path(prepared)
    assert prepared_path.exists() is True
    assert prepared_path.suffix == ".m3u8"
    assert "/video/adjump/" not in prepared_path.read_text(encoding="utf-8")
    assert requests == [
        (
            "https://media.example/path/index.m3u8",
            {"Referer": "https://site.example"},
            10.0,
            True,
        )
    ]


def test_m3u8_ad_filter_returns_original_url_when_fetch_fails(tmp_path: Path) -> None:
    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool):
        raise RuntimeError("network down")

    ad_filter = M3U8AdFilter(cache_dir=tmp_path, get=fake_get)

    original = "https://media.example/path/index.m3u8"
    prepared = ad_filter.prepare(original, {"Referer": "https://site.example"})

    assert prepared == original


def test_m3u8_ad_filter_returns_original_url_for_master_playlist(tmp_path: Path) -> None:
    class FakeResponse:
        text = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1280000
sub/playlist.m3u8
"""

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool) -> FakeResponse:
        return FakeResponse()

    ad_filter = M3U8AdFilter(cache_dir=tmp_path, get=fake_get)

    original = "https://media.example/master.m3u8"
    prepared = ad_filter.prepare(original)

    assert prepared == original
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_m3u8_ad_filter.py::test_m3u8_ad_filter_writes_cleaned_playlist_to_cache tests/test_m3u8_ad_filter.py::test_m3u8_ad_filter_returns_original_url_when_fetch_fails tests/test_m3u8_ad_filter.py::test_m3u8_ad_filter_returns_original_url_for_master_playlist -v`

Expected: `FAIL` because `M3U8AdFilter` and `prepare()` do not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
from hashlib import sha256
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from atv_player.paths import app_cache_dir

_PLAYLIST_TIMEOUT_SECONDS = 10.0


def _is_remote_m3u8_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and ".m3u8" in url.lower()


class M3U8AdFilter:
    def __init__(
        self,
        cache_dir: Path | None = None,
        get: Callable[..., object] = httpx.get,
    ) -> None:
        self._cache_dir = cache_dir or (app_cache_dir() / "playlists")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._get = get

    def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
        if not _is_remote_m3u8_url(url):
            return url
        request_headers = dict(headers or {})
        try:
            response = self._get(
                url,
                headers=request_headers,
                timeout=_PLAYLIST_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
            response.raise_for_status()
        except Exception:
            return url
        playlist_text = str(getattr(response, "text", "") or "")
        if not playlist_text.startswith("#EXTM3U"):
            return url
        rewritten = rewrite_media_playlist(playlist_text, url)
        if rewritten.is_master_playlist or not rewritten.changed:
            return url
        cache_path = self._cache_dir / f"{sha256(url.encode('utf-8')).hexdigest()}.m3u8"
        cache_path.write_text(rewritten.text, encoding="utf-8")
        return str(cache_path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_m3u8_ad_filter.py -v`

Expected: `PASS` for the rewrite tests and the new fetch/cache tests.

- [ ] **Step 5: Commit**

```bash
git add tests/test_m3u8_ad_filter.py src/atv_player/player/m3u8_ad_filter.py
git commit -m "feat: materialize cleaned m3u8 playlists locally"
```

## Task 3: Apply The Shared Filter In PlayerWindow Before Playback Starts

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_player_window_filters_remote_m3u8_before_video_load(qtbot) -> None:
    class FakeM3U8AdFilter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            self.calls.append((url, dict(headers or {})))
            return "/tmp/cleaned-playlist.m3u8"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://media.example/path/index.m3u8",
                headers={"Referer": "https://site.example"},
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    filter_service = FakeM3U8AdFilter()
    video = RecordingVideo()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=filter_service)
    qtbot.addWidget(window)
    window.video = video

    window.open_session(session)
    qtbot.waitUntil(lambda: video.load_calls == [("/tmp/cleaned-playlist.m3u8", 0)])

    assert filter_service.calls == [
        (
            "https://media.example/path/index.m3u8",
            {"Referer": "https://site.example"},
        )
    ]


def test_player_window_falls_back_to_original_url_when_filtering_fails(qtbot) -> None:
    class FailingM3U8AdFilter:
        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            raise RuntimeError("network down")

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="正片", url="https://media.example/path/index.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    video = RecordingVideo()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=FailingM3U8AdFilter())
    qtbot.addWidget(window)
    window.video = video

    window.open_session(session)
    qtbot.waitUntil(lambda: video.load_calls == [("https://media.example/path/index.m3u8", 0)])

    assert "广告过滤失败" in window.log_view.toPlainText()


def test_player_window_filters_resolved_m3u8_after_detail_lookup(qtbot) -> None:
    class ResolvingPlayerController(FakePlayerController):
        def resolve_play_item_detail(self, session, play_item):
            play_item.url = "https://media.example/path/resolved.m3u8"
            return VodItem(
                vod_id="movie-1",
                vod_name="Resolved Movie",
                items=[PlayItem(title="正片", url=play_item.url)],
            )

    class FakeM3U8AdFilter:
        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            return "/tmp/resolved-cleaned.m3u8"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="正片", url="", vod_id="detail-1")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        detail_resolver=lambda item: VodItem(vod_id=item.vod_id, vod_name="Resolved Movie"),
    )
    video = RecordingVideo()
    window = PlayerWindow(ResolvingPlayerController(), m3u8_ad_filter=FakeM3U8AdFilter())
    qtbot.addWidget(window)
    window.video = video

    window.open_session(session)
    qtbot.waitUntil(lambda: video.load_calls == [("/tmp/resolved-cleaned.m3u8", 0)])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_filters_remote_m3u8_before_video_load tests/test_player_window_ui.py::test_player_window_falls_back_to_original_url_when_filtering_fails tests/test_player_window_ui.py::test_player_window_filters_resolved_m3u8_after_detail_lookup -v`

Expected: `FAIL` because `PlayerWindow` does not accept `m3u8_ad_filter`, and the current playback path starts video playback immediately without shared URL preparation.

- [ ] **Step 3: Write minimal implementation**

```python
from atv_player.player.m3u8_ad_filter import M3U8AdFilter


class _PlaybackPrepareSignals(QObject):
    succeeded = Signal(int, str)
    failed = Signal(int, str)


class PlayerWindow(QWidget):
    def __init__(self, controller, config=None, save_config=None, m3u8_ad_filter=None) -> None:
        super().__init__()
        self.controller = controller
        self.config = config
        self._save_config = save_config or (lambda: None)
        self._m3u8_ad_filter = m3u8_ad_filter or M3U8AdFilter()
        self._playback_prepare_signals = _PlaybackPrepareSignals(self)
        self._playback_prepare_signals.succeeded.connect(self._handle_playback_prepare_succeeded)
        self._playback_prepare_signals.failed.connect(self._handle_playback_prepare_failed)

    def _start_playback_prepare(
        self,
        *,
        previous_index: int,
        start_position_seconds: int,
        pause: bool,
    ) -> bool:
        if self.session is None:
            return False
        current_item = self.session.playlist[self.current_index]
        if ".m3u8" not in current_item.url.lower():
            return False
        self._play_item_request_id += 1
        request_id = self._play_item_request_id
        self._pending_play_item_load = _PendingPlayItemLoad(
            index=self.current_index,
            previous_index=previous_index,
            start_position_seconds=start_position_seconds,
            pause=pause,
            wait_for_load=True,
        )

        def prepare() -> None:
            try:
                prepared_url = self._m3u8_ad_filter.prepare(current_item.url, current_item.headers)
            except Exception as exc:
                if self._is_window_alive():
                    self._playback_prepare_signals.failed.emit(request_id, str(exc))
                return
            if self._is_window_alive():
                self._playback_prepare_signals.succeeded.emit(request_id, prepared_url)

        self._enqueue_controller_task("播放地址预处理失败", prepare)
        return True

    def _prepare_current_play_item(
        self,
        *,
        previous_index: int,
        start_position_seconds: int,
        pause: bool,
    ) -> bool:
        if self.session is None:
            return True
        current_item = self.session.playlist[self.current_index]
        resolved_vod = self._resolve_current_play_item()
        if self.session.playback_loader is not None:
            self.session.playback_loader(current_item)
        if current_item.url:
            if self._start_playback_prepare(
                previous_index=previous_index,
                start_position_seconds=start_position_seconds,
                pause=pause,
            ):
                return False
            if resolved_vod is None and current_item.vod_id and self.session.detail_resolver is not None:
                self._start_play_item_resolution(
                    previous_index=previous_index,
                    start_position_seconds=start_position_seconds,
                    pause=pause,
                    wait_for_load=False,
                )
            return True
        if current_item.vod_id and self.session.detail_resolver is not None:
            self._start_play_item_resolution(
                previous_index=previous_index,
                start_position_seconds=start_position_seconds,
                pause=pause,
                wait_for_load=True,
            )
            return False
        return True

    def _handle_playback_prepare_succeeded(self, request_id: int, prepared_url: str) -> None:
        if request_id != self._play_item_request_id:
            return
        pending_load = self._pending_play_item_load
        self._pending_play_item_load = None
        if pending_load is None or self.session is None or self.current_index != pending_load.index:
            return
        self.session.playlist[self.current_index].url = prepared_url
        self._start_current_item_playback(
            start_position_seconds=pending_load.start_position_seconds,
            pause=pending_load.pause,
        )

    def _handle_playback_prepare_failed(self, request_id: int, message: str) -> None:
        if request_id != self._play_item_request_id:
            return
        pending_load = self._pending_play_item_load
        self._pending_play_item_load = None
        if pending_load is None or self.session is None or self.current_index != pending_load.index:
            return
        self._append_log(f"广告过滤失败，继续播放原地址: {message}")
        self._start_current_item_playback(
            start_position_seconds=pending_load.start_position_seconds,
            pause=pending_load.pause,
        )

    def _handle_play_item_resolve_succeeded(self, request_id: int, resolved_vod: VodItem | None) -> None:
        if request_id != self._play_item_request_id:
            return
        pending_load = self._pending_play_item_load
        self._pending_play_item_load = None
        if resolved_vod is not None:
            self._apply_resolved_vod(resolved_vod)
        if pending_load is None or not pending_load.wait_for_load:
            return
        if self.session is None or self.current_index != pending_load.index:
            return
        current_item = self.session.playlist[self.current_index]
        if not current_item.url:
            self._restore_current_index(pending_load.previous_index)
            self._append_log(f"播放失败: 没有可用的播放地址: {current_item.title}")
            return
        if self._start_playback_prepare(
            previous_index=pending_load.previous_index,
            start_position_seconds=pending_load.start_position_seconds,
            pause=pending_load.pause,
        ):
            return
        self._start_current_item_playback(
            start_position_seconds=pending_load.start_position_seconds,
            pause=pending_load.pause,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_m3u8_ad_filter.py tests/test_player_window_ui.py::test_player_window_filters_remote_m3u8_before_video_load tests/test_player_window_ui.py::test_player_window_falls_back_to_original_url_when_filtering_fails tests/test_player_window_ui.py::test_player_window_filters_resolved_m3u8_after_detail_lookup -v`

Expected: `PASS`

- [ ] **Step 5: Commit**

```bash
git add tests/test_m3u8_ad_filter.py tests/test_player_window_ui.py src/atv_player/player/m3u8_ad_filter.py src/atv_player/ui/player_window.py
git commit -m "feat: filter explicit m3u8 ads before playback"
```

## Final Verification

- Run: `uv run pytest tests/test_m3u8_ad_filter.py tests/test_player_window_ui.py -v`
  Expected: all targeted M3U8 filter and player-window tests pass.

- Run: `uv run pytest tests/test_player_controller.py tests/test_spider_plugin_controller.py tests/test_live_controller.py -v`
  Expected: unrelated playback-routing tests still pass, proving the shared integration did not regress existing source handling.
