# Folder Item Detail Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve playable URLs and metadata for folder-played video files through `get_detail()` before playback, reuse resolved episode detail from an in-memory session cache, and keep previous/next navigation stable when detail resolution fails.

**Architecture:** Keep folder-item resolution inside `BrowseController`, pass a narrow resolver and seeded cache through `OpenPlayerRequest` into `PlayerSession`, and let `PlayerWindow` resolve an episode immediately before loading it. The player window stays responsible for UI state and failure messaging, while the controller/session layer owns detail caching and mapping raw detail payloads into reusable `VodItem` objects.

**Tech Stack:** Python 3.12, PySide6 widgets, pytest, pytest-qt, dataclasses, existing HTTP API client

---

## File Structure

- `src/atv_player/models.py`
  - Extend `PlayItem` and `OpenPlayerRequest` so folder playback can carry `vod_id`, a detail resolver callback, and a seeded resolved-detail cache.
- `src/atv_player/controllers/browse_controller.py`
  - Build folder playlists with `vod_id`, resolve the initially clicked file through `get_detail()`, and expose a reusable per-item folder detail resolver.
- `src/atv_player/controllers/player_controller.py`
  - Extend `PlayerSession` and `PlayerController` with an in-memory resolved-detail cache and a helper for per-episode resolution.
- `src/atv_player/ui/main_window.py`
  - Pass request-level detail resolver and cache seed into `PlayerController.create_session()`.
- `src/atv_player/ui/player_window.py`
  - Resolve the target episode before loading it, refresh metadata/presentation from resolved detail, and prevent index changes when resolution fails.
- `tests/test_browse_controller.py`
  - Add focused tests for clicked-item detail resolution and folder playlist `vod_id` preservation.
- `tests/test_player_controller.py`
  - Add focused tests for session cache seeding and cache-backed repeated episode resolution.
- `tests/test_player_window_ui.py`
  - Add focused tests for previous/next resolution, cache reuse, and failure behavior.
- `tests/test_app.py`
  - Keep folder-mode restore coverage green after request/session signatures gain resolver and cache fields.

### Task 1: Resolve The Initially Clicked Folder Item Through Detail

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/controllers/browse_controller.py`
- Modify: `tests/test_browse_controller.py`
- Test: `tests/test_browse_controller.py`

- [ ] **Step 1: Write the failing browse-controller tests**

Update the imports at the top of `tests/test_browse_controller.py`:

```python
from atv_player.models import PlayItem, VodItem
```

Update `FakeApiClient.__init__()` so detail lookups can be asserted:

```python
class FakeApiClient:
    def __init__(self) -> None:
        self.resolved_links: list[str] = []
        self.list_vod_calls: list[tuple[str, int, int]] = []
        self.search_keywords: list[str] = []
        self.search_payload: list[dict] = []
        self.detail_calls: list[str] = []
        self.detail_payload = {
            "list": [
                {
                    "vod_id": "detail-1",
                    "vod_name": "Movie",
                    "vod_pic": "pic",
                    "vod_play_url": "http://m/1.m3u8",
                    "items": [
                        {"title": "Episode 1", "url": "1.m3u8"},
                        {"title": "Episode 2", "url": "2.m3u8"},
                    ],
                }
            ]
        }
```

Update `FakeApiClient.get_detail()`:

```python
    def get_detail(self, vod_id: str) -> dict:
        self.detail_calls.append(vod_id)
        return self.detail_payload
```

Add these tests after `test_build_playlist_from_folder_starts_at_clicked_video`:

```python
def test_build_playlist_from_folder_preserves_vod_ids_for_playable_files() -> None:
    controller = BrowseController(FakeApiClient())
    folder_items = [
        VodItem(vod_id="f1", vod_name="folder", type=1, path="/TV/folder"),
        VodItem(vod_id="v1", vod_name="Ep1", type=2, vod_play_url="", path="/TV/Ep1.mkv"),
        VodItem(vod_id="v2", vod_name="Ep2", type=2, vod_play_url="", path="/TV/Ep2.mkv"),
    ]

    playlist, start_index = controller.build_playlist_from_folder(folder_items, clicked_vod_id="v2")

    assert [(item.title, item.vod_id) for item in playlist] == [("Ep1", "v1"), ("Ep2", "v2")]
    assert start_index == 1


def test_build_request_from_folder_item_resolves_clicked_item_detail_before_playback() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1$91483$1",
                "vod_name": "Resolved Episode",
                "vod_pic": "resolved-poster.jpg",
                "vod_play_url": "http://192.168.50.60:4567/p/web/1@91483?ac=web&ids=1$91483$1",
                "type_name": "剧情",
                "vod_content": "resolved content",
                "items": [
                    {
                        "id": 91483,
                        "title": "Resolved Episode",
                        "url": "http://192.168.50.60:4567/p/web/1@91483?ac=web&ids=1$91483$1",
                        "path": "/TV/Ep1.mkv",
                        "size": 123,
                    }
                ],
            }
        ]
    }
    controller = BrowseController(api)
    clicked_item = VodItem(
        vod_id="1$91483$1",
        vod_name="Folder Episode",
        path="/TV/Ep1.mkv",
        type=2,
        vod_play_url="",
        vod_content="folder content",
    )

    request = controller.build_request_from_folder_item(clicked_item, [clicked_item])

    assert api.detail_calls == ["1$91483$1"]
    assert request.vod.vod_name == "Resolved Episode"
    assert request.vod.vod_content == "resolved content"
    assert request.playlist[0].url == "http://192.168.50.60:4567/p/web/1@91483?ac=web&ids=1$91483$1"
    assert request.playlist[0].vod_id == "1$91483$1"
    assert request.resolved_vod_by_id["1$91483$1"].vod_name == "Resolved Episode"
```

- [ ] **Step 2: Run the focused browse-controller tests to verify they fail**

Run:

```bash
uv run pytest tests/test_browse_controller.py::test_build_playlist_from_folder_preserves_vod_ids_for_playable_files tests/test_browse_controller.py::test_build_request_from_folder_item_resolves_clicked_item_detail_before_playback -q
```

Expected: FAIL because `PlayItem` does not yet expose `vod_id`, `OpenPlayerRequest` does not carry a resolved-detail cache, and folder playback still uses the raw folder item without calling `get_detail()`.

- [ ] **Step 3: Write the minimal browse-controller and model implementation**

Update `src/atv_player/models.py` imports and dataclasses:

```python
from collections.abc import Callable
from dataclasses import dataclass, field
```

Add `vod_id` to `PlayItem`:

```python
@dataclass(slots=True)
class PlayItem:
    title: str
    url: str
    path: str = ""
    index: int = 0
    size: int = 0
    vod_id: str = ""
```

Add resolver and cache fields to `OpenPlayerRequest`:

```python
@dataclass(slots=True)
class OpenPlayerRequest:
    vod: VodItem
    playlist: list[PlayItem]
    clicked_index: int
    source_mode: str = ""
    source_path: str = ""
    source_vod_id: str = ""
    source_clicked_vod_id: str = ""
    detail_resolver: Callable[[PlayItem], VodItem] | None = None
    resolved_vod_by_id: dict[str, VodItem] = field(default_factory=dict)
```

Update `_map_play_item()` in `src/atv_player/controllers/browse_controller.py`:

```python
def _map_play_item(payload: dict, index: int) -> PlayItem:
    return PlayItem(
        title=str(payload.get("title") or payload.get("name") or ""),
        url=str(payload.get("url") or ""),
        path=str(payload.get("path") or ""),
        index=index,
        size=int(payload.get("size") or 0),
        vod_id=str(payload.get("vod_id") or ""),
    )
```

Add helper methods to `BrowseController`:

```python
    def _first_play_url(self, vod: VodItem) -> str:
        if vod.items:
            return vod.items[0].url
        return vod.vod_play_url

    def resolve_folder_play_item(self, item: PlayItem) -> VodItem:
        payload = self._api_client.get_detail(item.vod_id)
        return _map_vod_item(payload["list"][0])
```

Update `build_playlist_from_folder()`:

```python
            playlist_item = PlayItem(
                title=item.vod_name,
                url=item.vod_play_url,
                path=item.path,
                index=index,
                size=0,
                vod_id=item.vod_id,
            )
```

Update `build_request_from_folder_item()`:

```python
    def build_request_from_folder_item(
        self,
        clicked_item: VodItem,
        folder_items: list[VodItem],
    ) -> OpenPlayerRequest:
        playlist, clicked_index = self.build_playlist_from_folder(folder_items, clicked_item.vod_id)
        clicked_playlist_item = playlist[clicked_index]
        resolved_vod = self.resolve_folder_play_item(clicked_playlist_item)
        clicked_playlist_item.url = self._first_play_url(resolved_vod)

        return OpenPlayerRequest(
            vod=resolved_vod,
            playlist=playlist,
            clicked_index=clicked_index,
            source_mode="folder",
            source_path=clicked_item.path.rsplit("/", 1)[0] or "/",
            source_vod_id=clicked_item.vod_id,
            source_clicked_vod_id=clicked_item.vod_id,
            detail_resolver=self.resolve_folder_play_item,
            resolved_vod_by_id={resolved_vod.vod_id: resolved_vod},
        )
```

- [ ] **Step 4: Run the focused browse-controller tests to verify they pass**

Run:

```bash
uv run pytest tests/test_browse_controller.py::test_build_playlist_from_folder_preserves_vod_ids_for_playable_files tests/test_browse_controller.py::test_build_request_from_folder_item_resolves_clicked_item_detail_before_playback -q
```

Expected: PASS with per-item `vod_id` preserved and the initially clicked folder item resolved through `get_detail()` before the player opens.

- [ ] **Step 5: Commit**

```bash
git add tests/test_browse_controller.py src/atv_player/models.py src/atv_player/controllers/browse_controller.py
git commit -m "feat: resolve clicked folder item detail before playback"
```

### Task 2: Add Session-Level Detail Resolver And Cache

**Files:**
- Modify: `src/atv_player/controllers/player_controller.py`
- Modify: `src/atv_player/ui/main_window.py`
- Modify: `tests/test_player_controller.py`
- Modify: `tests/test_app.py`
- Test: `tests/test_player_controller.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing player-controller and restore tests**

Update `tests/test_player_controller.py` imports:

```python
from atv_player.controllers.player_controller import PlayerController
from atv_player.models import HistoryRecord, PlayItem, VodItem
```

Keep `FakeApiClient` focused on saved history state:

```python
class FakeApiClient:
    def __init__(self) -> None:
        self.saved_payloads: list[dict] = []
        self.history: HistoryRecord | None = None
```

Add these tests after `test_player_controller_builds_history_payload`:

```python
def test_player_controller_create_session_preserves_detail_resolver_and_seed_cache() -> None:
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="movie-1", vod_name="Movie")
    playlist = [PlayItem(title="Episode 1", url="", vod_id="1$91483$1")]
    resolved_vod = VodItem(
        vod_id="1$91483$1",
        vod_name="Resolved Episode",
        vod_play_url="http://m/1.m3u8",
        items=[PlayItem(title="Episode 1", url="http://m/1.m3u8", vod_id="1$91483$1")],
    )

    def detail_resolver(item: PlayItem) -> VodItem:
        raise AssertionError("resolver should not be called when the cache is pre-seeded")

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        detail_resolver=detail_resolver,
        resolved_vod_by_id={"1$91483$1": resolved_vod},
    )

    assert session.detail_resolver is detail_resolver
    assert session.resolved_vod_by_id["1$91483$1"].vod_name == "Resolved Episode"


def test_player_controller_resolve_play_item_detail_uses_session_cache() -> None:
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="movie-1", vod_name="Movie")
    playlist = [PlayItem(title="Episode 1", url="", vod_id="1$91483$1")]
    calls: list[str] = []

    def detail_resolver(item: PlayItem) -> VodItem:
        calls.append(item.vod_id)
        return VodItem(
            vod_id=item.vod_id,
            vod_name="Resolved Episode",
            vod_play_url="http://m/1.m3u8",
            items=[PlayItem(title="Episode 1", url="http://m/1.m3u8", vod_id=item.vod_id)],
        )

    session = controller.create_session(vod, playlist, clicked_index=0, detail_resolver=detail_resolver)

    first = controller.resolve_play_item_detail(session, playlist[0])
    second = controller.resolve_play_item_detail(session, playlist[0])

    assert calls == ["1$91483$1"]
    assert first.vod_name == "Resolved Episode"
    assert second.vod_name == "Resolved Episode"
    assert playlist[0].url == "http://m/1.m3u8"
```

Update `tests/test_app.py` so `FakePlayerController.create_session()` accepts the new optional arguments:

```python
class FakePlayerController:
    def create_session(
        self,
        vod,
        playlist,
        clicked_index: int,
        detail_resolver=None,
        resolved_vod_by_id=None,
    ):
        return {
            "vod": vod,
            "playlist": playlist,
            "clicked_index": clicked_index,
            "detail_resolver": detail_resolver,
            "resolved_vod_by_id": resolved_vod_by_id or {},
        }
```

Add this restore test after `test_main_window_restore_last_player_opens_paused_from_config`:

```python
def test_main_window_restore_last_player_rebuilds_folder_request_with_detail_resolver(qtbot) -> None:
    class RestoreBrowseController:
        def __init__(self) -> None:
            self.load_calls: list[str] = []
            self.request_calls: list[str] = []

        def load_folder(self, path: str):
            self.load_calls.append(path)
            return [VodItem(vod_id="1$91483$1", vod_name="Episode 1", path="/TV/Ep1.mkv", type=2)], 1

        def build_request_from_folder_item(self, clicked, items):
            self.request_calls.append(clicked.vod_id)
            return OpenPlayerRequest(
                vod=VodItem(vod_id=clicked.vod_id, vod_name="Episode 1"),
                playlist=[PlayItem(title="Episode 1", url="", vod_id=clicked.vod_id)],
                clicked_index=0,
                source_mode="folder",
                source_path="/TV",
                source_vod_id=clicked.vod_id,
                source_clicked_vod_id=clicked.vod_id,
                detail_resolver=lambda item: VodItem(vod_id=item.vod_id, vod_name="Resolved Episode"),
                resolved_vod_by_id={},
            )

    config = AppConfig(
        last_active_window="player",
        last_playback_mode="folder",
        last_playback_path="/TV",
        last_playback_clicked_vod_id="1$91483$1",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    assert window.player_window.opened_session["detail_resolver"] is not None
```

- [ ] **Step 2: Run the focused session and app tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_controller.py::test_player_controller_create_session_preserves_detail_resolver_and_seed_cache tests/test_player_controller.py::test_player_controller_resolve_play_item_detail_uses_session_cache tests/test_app.py::test_main_window_restore_last_player_rebuilds_folder_request_with_detail_resolver -q
```

Expected: FAIL because `PlayerSession` does not yet carry resolver/cache fields, `PlayerController.create_session()` cannot accept them, and `MainWindow.open_player()` does not forward them.

- [ ] **Step 3: Write the minimal session-cache implementation**

Update the imports and `PlayerSession` in `src/atv_player/controllers/player_controller.py`:

```python
from collections.abc import Callable
from dataclasses import dataclass, field
from time import time

from atv_player.models import PlayItem, VodItem
```

```python
@dataclass(slots=True)
class PlayerSession:
    vod: VodItem
    playlist: list[PlayItem]
    start_index: int
    start_position_seconds: int
    speed: float
    opening_seconds: int = 0
    ending_seconds: int = 0
    detail_resolver: Callable[[PlayItem], VodItem] | None = None
    resolved_vod_by_id: dict[str, VodItem] = field(default_factory=dict)
```

Extend `create_session()` in `src/atv_player/controllers/player_controller.py`:

```python
    def create_session(
        self,
        vod: VodItem,
        playlist: list[PlayItem],
        clicked_index: int,
        detail_resolver: Callable[[PlayItem], VodItem] | None = None,
        resolved_vod_by_id: dict[str, VodItem] | None = None,
    ) -> PlayerSession:
        history = self._api_client.get_history(vod.vod_id)
        start_index = resolve_resume_index(history, playlist, clicked_index)
        position_seconds = int((history.position if history else 0) / 1000)
        speed = history.speed if history else 1.0
        return PlayerSession(
            vod=vod,
            playlist=playlist,
            start_index=start_index,
            start_position_seconds=position_seconds,
            speed=speed,
            opening_seconds=int((history.opening if history else 0) / 1000),
            ending_seconds=int((history.ending if history else 0) / 1000),
            detail_resolver=detail_resolver,
            resolved_vod_by_id=dict(resolved_vod_by_id or {}),
        )
```

Add cache-backed resolution to `PlayerController`:

```python
    def resolve_play_item_detail(self, session: PlayerSession, play_item: PlayItem) -> VodItem | None:
        if not play_item.vod_id or session.detail_resolver is None:
            return None
        if play_item.vod_id in session.resolved_vod_by_id:
            resolved_vod = session.resolved_vod_by_id[play_item.vod_id]
        else:
            resolved_vod = session.detail_resolver(play_item)
            session.resolved_vod_by_id[play_item.vod_id] = resolved_vod
        play_item.url = resolved_vod.items[0].url if resolved_vod.items else resolved_vod.vod_play_url
        return resolved_vod
```

Forward the new request fields in `src/atv_player/ui/main_window.py`:

```python
        session = self.player_controller.create_session(
            request.vod,
            request.playlist,
            request.clicked_index,
            detail_resolver=request.detail_resolver,
            resolved_vod_by_id=request.resolved_vod_by_id,
        )
```

Update the `RecordingPlayerWindow` helper in `tests/test_app.py` so the session object is inspectable:

```python
class RecordingPlayerWindow:
    def __init__(self, controller, config, save_config) -> None:
        self.opened: list[tuple[object, bool]] = []
        self.opened_session = None

    def open_session(self, session, start_paused: bool = False) -> None:
        self.opened.append((session, start_paused))
        self.opened_session = session
```

- [ ] **Step 4: Run the focused session and app tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_controller.py::test_player_controller_create_session_preserves_detail_resolver_and_seed_cache tests/test_player_controller.py::test_player_controller_resolve_play_item_detail_uses_session_cache tests/test_app.py::test_main_window_restore_last_player_rebuilds_folder_request_with_detail_resolver -q
```

Expected: PASS with resolver/cache state preserved into the session and folder-mode restore still producing a playable request.

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_controller.py tests/test_app.py src/atv_player/controllers/player_controller.py src/atv_player/ui/main_window.py
git commit -m "feat: add session cache for folder item detail"
```

### Task 3: Resolve Episodes On Demand In PlayerWindow And Keep Navigation Stable On Failure

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`
- Test: `tests/test_browse_controller.py`
- Test: `tests/test_player_controller.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing player-window tests**

Add these tests to `tests/test_player_window_ui.py` after the existing playback-advance tests:

```python
def test_player_window_play_next_resolves_target_episode_before_loading(qtbot) -> None:
    controller = RecordingPlayerController()
    resolved_vod = VodItem(
        vod_id="ep-2",
        vod_name="Resolved Episode 2",
        vod_content="resolved episode content",
        items=[PlayItem(title="Episode 2", url="http://resolved/2.m3u8", vod_id="ep-2")],
    )

    class FakeVideo(RecordingVideo):
        pass

    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = FakeVideo()
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(title="Episode 1", url="http://m/1.m3u8", vod_id="ep-1"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]
    session.detail_resolver = lambda item: resolved_vod
    window.open_session(session)
    window.video.load_calls.clear()

    window.play_next()

    assert window.current_index == 1
    assert window.video.load_calls == [("http://resolved/2.m3u8", 0)]
    assert "resolved episode content" in window.metadata_view.toPlainText()


def test_player_window_reuses_cached_detail_when_returning_to_same_episode(qtbot) -> None:
    controller = RecordingPlayerController()
    detail_calls: list[str] = []

    def detail_resolver(item: PlayItem) -> VodItem:
        detail_calls.append(item.vod_id)
        return VodItem(
            vod_id=item.vod_id,
            vod_name=f"Resolved {item.title}",
            items=[PlayItem(title=item.title, url=f"http://resolved/{item.vod_id}.m3u8", vod_id=item.vod_id)],
        )

    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(title="Episode 1", url="", vod_id="ep-1"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]
    session.detail_resolver = detail_resolver
    window.open_session(session)
    detail_calls.clear()
    window.video.load_calls.clear()

    window.play_next()
    window.play_previous()
    window.play_next()

    assert detail_calls == ["ep-2", "ep-1"]
    assert ("http://resolved/ep-2.m3u8", 0) in window.video.load_calls


def test_player_window_keeps_current_index_when_next_episode_detail_resolution_fails(qtbot) -> None:
    controller = RecordingPlayerController()

    def detail_resolver(item: PlayItem) -> VodItem:
        if item.vod_id == "ep-2":
            raise RuntimeError("detail failed")
        return VodItem(
            vod_id=item.vod_id,
            vod_name=item.title,
            items=[PlayItem(title=item.title, url=f"http://resolved/{item.vod_id}.m3u8", vod_id=item.vod_id)],
        )

    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(title="Episode 1", url="", vod_id="ep-1"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]
    session.detail_resolver = detail_resolver
    window.open_session(session)
    window.video.load_calls.clear()

    window.play_next()

    assert window.current_index == 0
    assert window.video.load_calls == []
    assert "播放失败: detail failed" in window.log_view.toPlainText()
```

- [ ] **Step 2: Run the focused player-window tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_play_next_resolves_target_episode_before_loading tests/test_player_window_ui.py::test_player_window_reuses_cached_detail_when_returning_to_same_episode tests/test_player_window_ui.py::test_player_window_keeps_current_index_when_next_episode_detail_resolution_fails -q
```

Expected: FAIL because `PlayerWindow` currently changes `current_index` before resolution, never refreshes from resolved detail, and has no path for cache-backed per-episode resolution.

- [ ] **Step 3: Write the minimal player-window resolution implementation**

Add these helpers to `src/atv_player/ui/player_window.py` near the other local rendering helpers:

```python
    def _apply_resolved_vod(self, resolved_vod: VodItem) -> None:
        if self.session is None:
            return
        self.session.vod = resolved_vod
        self._render_poster()
        self._render_metadata()

    def _resolve_current_play_item(self) -> None:
        if self.session is None:
            return
        current_item = self.session.playlist[self.current_index]
        resolved_vod = self.controller.resolve_play_item_detail(self.session, current_item)
        if resolved_vod is not None:
            self._apply_resolved_vod(resolved_vod)

    def _play_item_at_index(self, index: int, start_position_seconds: int = 0, pause: bool = False) -> None:
        if self.session is None:
            return
        previous_index = self.current_index
        self.current_index = index
        try:
            self.playlist.setCurrentRow(self.current_index)
            self._load_current_item(start_position_seconds=start_position_seconds, pause=pause)
        except Exception:
            self.current_index = previous_index
            self.playlist.setCurrentRow(previous_index)
            raise
```

Update `_load_current_item()`:

```python
    def _load_current_item(self, start_position_seconds: int = 0, pause: bool = False) -> None:
        if self.session is None:
            return
        current_item = self.session.playlist[self.current_index]
        self._resolve_current_play_item()
        current_item = self.session.playlist[self.current_index]
        self._append_log(f"当前: {current_item.title}")
        self._append_log(f"URL: {current_item.url}")
        effective_start_seconds = max(start_position_seconds, self.opening_spin.value())
        self.video.load(current_item.url, pause=pause, start_seconds=effective_start_seconds)
        self.video.set_speed(self.current_speed)
        self.video.set_volume(self.volume_slider.value())
        self._refresh_subtitle_state()
```

Update the navigation methods to resolve before committing the index:

```python
    def play_previous(self) -> None:
        if self.session is None or self.current_index <= 0:
            return
        self.report_progress()
        target_index = self.current_index - 1
        try:
            self._play_item_at_index(target_index)
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")

    def play_next(self) -> None:
        if self.session is None or self.current_index + 1 >= len(self.session.playlist):
            return
        self.report_progress()
        target_index = self.current_index + 1
        try:
            self._play_item_at_index(target_index)
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")

    def _play_clicked_item(self, item: QListWidgetItem) -> None:
        row = self.playlist.row(item)
        if row == self.current_index or self.session is None:
            return
        self.report_progress()
        try:
            self._play_item_at_index(row)
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")
```

Update `open_session()` so the first item uses the same safe path:

```python
        self.progress.setValue(0)
        self._reset_subtitle_combo()
        self._play_item_at_index(self.current_index, start_position_seconds=session.start_position_seconds, pause=start_paused)
```

- [ ] **Step 4: Run the focused player-window tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_play_next_resolves_target_episode_before_loading tests/test_player_window_ui.py::test_player_window_reuses_cached_detail_when_returning_to_same_episode tests/test_player_window_ui.py::test_player_window_keeps_current_index_when_next_episode_detail_resolution_fails -q
```

Expected: PASS with per-episode resolution happening before load, cache reuse avoiding duplicate detail calls, and failed navigation leaving the current episode unchanged.

- [ ] **Step 5: Run the regression suites**

Run:

```bash
uv run pytest tests/test_browse_controller.py tests/test_player_controller.py tests/test_player_window_ui.py tests/test_app.py -q
```

Expected: PASS with browse, app restore, player session, and player window flows all green after the folder-detail resolution changes.

- [ ] **Step 6: Commit**

```bash
git add tests/test_player_window_ui.py tests/test_browse_controller.py tests/test_player_controller.py tests/test_app.py src/atv_player/ui/player_window.py src/atv_player/controllers/player_controller.py src/atv_player/controllers/browse_controller.py src/atv_player/ui/main_window.py src/atv_player/models.py
git commit -m "feat: resolve folder playback items on demand"
```
