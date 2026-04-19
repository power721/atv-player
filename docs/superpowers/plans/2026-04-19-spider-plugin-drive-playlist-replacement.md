# Spider Plugin Drive Playlist Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a spider-plugin route item is a supported drive-share link such as `查看$https://pan.quark.cn/s/14a405a9bb0d`, clicking it should replace the active route's placeholder playlist with the flattened backend `items` list and start playback from the first expanded item.

**Architecture:** Extend the existing deferred `playback_loader` flow so loaders can optionally return a route-playlist replacement instead of only mutating one `PlayItem`. The player window will apply that replacement to the active route in the current session, rerender the sidebar, and continue playback from index `0`. Spider-plugin drive resolution will map backend `items` directly into replacement `PlayItem`s in backend order, while other plugin routes keep the current single-item loading behavior.

**Tech Stack:** Python, PySide6, pytest, existing spider-plugin controller flow, `PlayerWindow`, request/session dataclasses

---

## File Structure

- Modify: `src/atv_player/models.py`
  Add a small result dataclass for playback-loader route replacement and update callback types.
- Modify: `src/atv_player/controllers/player_controller.py`
  Carry the upgraded playback-loader type through `PlayerSession`.
- Modify: `src/atv_player/ui/player_window.py`
  Apply replacement playlists returned by the active route's playback loader and rerender the current route.
- Modify: `src/atv_player/plugins/controller.py`
  Remove debug prints, map backend drive `items` into flattened `PlayItem`s, and return a route-replacement result for drive routes.
- Modify: `tests/test_spider_plugin_controller.py`
  Cover the returned replacement playlist for `quark`/`baidu` drive routes and preserve normal routes.
- Modify: `tests/test_player_window_ui.py`
  Cover replacing the active route playlist in-session and refreshing the visible sidebar items.

### Task 1: Add A Playback-Loader Result Type For Route Replacement

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/controllers/player_controller.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing player-window test for in-session route replacement**

Add this test to `tests/test_player_window_ui.py` near the other playback-loader tests:

```python
def test_player_window_replaces_active_route_playlist_when_playback_loader_returns_replacement(qtbot) -> None:
    controller = FakePlayerController()
    replacement = [
        PlayItem(title="S1 - 1", url="http://m/1.mp4", play_source="quark"),
        PlayItem(title="S1 - 2", url="http://m/2.mp4", play_source="quark"),
    ]

    def load_item(item: PlayItem):
        assert item.title == "查看"
        return PlaybackLoadResult(replacement_playlist=replacement, replacement_start_index=0)

    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘剧集"),
        playlist=[PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="quark")],
        playlists=[
            [PlayItem(title="第1集", url="http://line/1.m3u8", play_source="播放源 1")],
            [PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="quark")],
        ],
        playlist_index=1,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        playback_loader=load_item,
    )

    window = PlayerWindow(controller, config=None, save_config=lambda: None)
    qtbot.addWidget(window)

    window.open_session(session)

    assert window.session is not None
    assert window.session.playlist_index == 1
    assert [item.title for item in window.session.playlist] == ["S1 - 1", "S1 - 2"]
    assert [item.title for item in window.session.playlists[1]] == ["S1 - 1", "S1 - 2"]
    assert window.current_index == 0
    assert window.playlist.count() == 2
    assert window.playlist.item(0).text() == "S1 - 1"
```

- [ ] **Step 2: Run the player-window test to verify it fails**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_replaces_active_route_playlist_when_playback_loader_returns_replacement -q
```

Expected: FAIL because `PlaybackLoadResult` does not exist and `PlayerWindow` currently ignores any playlist-replacement concept.

- [ ] **Step 3: Add a playback-loader result type and propagate the callback signature**

Update `src/atv_player/models.py`:

```python
@dataclass(slots=True)
class PlaybackLoadResult:
    replacement_playlist: list[PlayItem] = field(default_factory=list)
    replacement_start_index: int = 0


@dataclass(slots=True)
class OpenPlayerRequest:
    vod: VodItem
    playlist: list[PlayItem]
    clicked_index: int
    playlists: list[list[PlayItem]] = field(default_factory=list)
    playlist_index: int = 0
    source_kind: str = "browse"
    source_key: str = ""
    source_mode: str = ""
    source_path: str = ""
    source_vod_id: str = ""
    source_clicked_vod_id: str = ""
    detail_resolver: Callable[[PlayItem], VodItem | None] | None = None
    resolved_vod_by_id: dict[str, VodItem] = field(default_factory=dict)
    use_local_history: bool = True
    restore_history: bool = False
    playback_loader: Callable[[PlayItem], PlaybackLoadResult | None] | None = None
    playback_progress_reporter: Callable[[PlayItem, int, bool], None] | None = None
    playback_stopper: Callable[[PlayItem], None] | None = None
    playback_history_loader: Callable[[], HistoryRecord | None] | None = None
    playback_history_saver: Callable[[dict[str, object]], None] | None = None
```

Update `src/atv_player/controllers/player_controller.py`:

```python
from atv_player.models import HistoryRecord, PlaybackLoadResult, PlayItem, VodItem


@dataclass(slots=True)
class PlayerSession:
    vod: VodItem
    playlist: list[PlayItem]
    start_index: int
    start_position_seconds: int
    speed: float
    playlists: list[list[PlayItem]] = field(default_factory=list)
    playlist_index: int = 0
    opening_seconds: int = 0
    ending_seconds: int = 0
    detail_resolver: Callable[[PlayItem], VodItem | None] | None = None
    resolved_vod_by_id: dict[str, VodItem] = field(default_factory=dict)
    use_local_history: bool = True
    playback_loader: Callable[[PlayItem], PlaybackLoadResult | None] | None = None
    playback_progress_reporter: Callable[[PlayItem, int, bool], None] | None = None
    playback_stopper: Callable[[PlayItem], None] | None = None
    playback_history_saver: Callable[[dict[str, object]], None] | None = None
```

- [ ] **Step 4: Apply route replacements in the player window**

Update `src/atv_player/ui/player_window.py` inside `_prepare_current_play_item()`:

```python
        if self.session.playback_loader is not None:
            load_result = self.session.playback_loader(current_item)
            if load_result is not None and load_result.replacement_playlist:
                replacement = list(load_result.replacement_playlist)
                self.session.playlists[self.session.playlist_index] = replacement
                self.session.playlist = replacement
                self.current_index = max(
                    0,
                    min(load_result.replacement_start_index, len(replacement) - 1),
                )
                self._render_playlist_group_combo()
                self._render_playlist_items()
                current_item = self.session.playlist[self.current_index]
```

Keep the rest of the playback flow unchanged so normal loaders that return `None` still work.

- [ ] **Step 5: Run the player-window test to verify it passes**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_replaces_active_route_playlist_when_playback_loader_returns_replacement -q
```

Expected: PASS

- [ ] **Step 6: Commit the playback-loader contract change**

Run:

```bash
git add tests/test_player_window_ui.py src/atv_player/models.py src/atv_player/controllers/player_controller.py src/atv_player/ui/player_window.py
git commit -m "feat: allow playback loaders to replace route playlists"
```

### Task 2: Return Flattened Drive Replacement Playlists From Spider Plugins

**Files:**
- Modify: `src/atv_player/plugins/controller.py`
- Test: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Write the failing spider-plugin tests for route replacement**

Add these tests to `tests/test_spider_plugin_controller.py`:

```python
def test_controller_returns_replacement_playlist_for_quark_drive_route() -> None:
    spider = DriveLinkSpider()

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.quark.cn/s/f518510ef92a"
        return {
            "list": [
                {
                    "vod_id": "1$94954$1",
                    "vod_name": "夸克资源",
                    "items": [
                        {"title": "S1 - 1", "url": "http://m/1.mp4", "path": "/S1/1.mp4", "size": 11},
                        {"title": "S1 - 2", "url": "http://m/2.mp4", "path": "/S1/2.mp4", "size": 12},
                    ],
                }
            ]
        }

    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
    )

    request = controller.build_request("/detail/drive")
    result = request.playback_loader(request.playlists[0][0])

    assert result is not None
    assert [item.title for item in result.replacement_playlist] == ["S1 - 1", "S1 - 2"]
    assert [item.url for item in result.replacement_playlist] == ["http://m/1.mp4", "http://m/2.mp4"]
    assert [item.play_source for item in result.replacement_playlist] == ["网盘线", "网盘线"]
    assert result.replacement_start_index == 0


def test_controller_returns_replacement_playlist_for_baidu_drive_route() -> None:
    class BaiduDriveSpider(FakeSpider):
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "百度网盘剧集",
                        "vod_play_from": "百度线",
                        "vod_play_url": "查看$https://pan.baidu.com/s/1demo?pwd=test",
                    }
                ]
            }

    controller = SpiderPluginController(
        BaiduDriveSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=lambda link: {
            "list": [
                {
                    "vod_id": "detail-1",
                    "vod_name": "百度资源",
                    "items": [
                        {"title": "第1集", "url": "http://b/1.mp4"},
                        {"title": "第2集", "url": "http://b/2.mp4"},
                    ],
                }
            ]
        },
    )

    request = controller.build_request("/detail/baidu")
    result = request.playback_loader(request.playlist[0])

    assert result is not None
    assert [item.title for item in result.replacement_playlist] == ["第1集", "第2集"]
    assert [item.url for item in result.replacement_playlist] == ["http://b/1.mp4", "http://b/2.mp4"]
```

- [ ] **Step 2: Run the spider-plugin tests to verify they fail**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py -q
```

Expected: FAIL because drive-link resolution still mutates a single clicked item instead of returning a replacement playlist.

- [ ] **Step 3: Return `PlaybackLoadResult` for supported drive routes**

Update `src/atv_player/plugins/controller.py`:

```python
from atv_player.models import DoubanCategory, OpenPlayerRequest, PlayItem, PlaybackLoadResult, VodItem
```

Remove the current debug print calls from `build_request()`, `_resolve_play_item()`, and the normal `playerContent()` path.

Then add a helper:

```python
    def _build_drive_replacement_playlist(self, detail: VodItem, play_source: str) -> list[PlayItem]:
        if detail.items:
            return [
                PlayItem(
                    title=item.title,
                    url=item.url,
                    path=item.path,
                    index=index,
                    size=item.size,
                    vod_id=item.vod_id,
                    headers=dict(item.headers),
                    play_source=play_source,
                )
                for index, item in enumerate(detail.items)
                if item.url
            ]
        playlist = build_detail_playlist(detail)
        return [
            PlayItem(
                title=item.title,
                url=item.url,
                path=item.path,
                index=index,
                size=item.size,
                vod_id=item.vod_id,
                headers=dict(item.headers),
                play_source=play_source,
            )
            for index, item in enumerate(playlist)
            if item.url and not _looks_like_drive_share_link(item.url)
        ]
```

Then change the drive branch inside `_resolve_play_item()`:

```python
            replacement = self._build_drive_replacement_playlist(detail, item.play_source)
            if not replacement:
                raise ValueError(f"没有可播放的项目: {detail.vod_name or item.title}")
            return PlaybackLoadResult(
                replacement_playlist=replacement,
                replacement_start_index=0,
            )
```

Keep the normal non-drive branch mutating `item.url` and returning `None`.

- [ ] **Step 4: Run the spider-plugin tests to verify they pass**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py -q
```

Expected: PASS

- [ ] **Step 5: Commit the spider-plugin replacement work**

Run:

```bash
git add tests/test_spider_plugin_controller.py src/atv_player/plugins/controller.py
git commit -m "feat: expand spider drive routes into episode playlists"
```

### Task 3: Verify Player Behavior And Targeted Regressions

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_api_client.py`
- Test: `tests/test_app.py`
- Test: `tests/test_telegram_search_controller.py`
- Test: `tests/test_spider_plugin_manager.py`

- [ ] **Step 1: Add a focused player-window regression for preserving other routes**

Add this test to `tests/test_player_window_ui.py`:

```python
def test_player_window_route_replacement_keeps_other_route_groups_unchanged(qtbot) -> None:
    controller = FakePlayerController()
    first_group = [PlayItem(title="第1集", url="http://line/1.m3u8", play_source="播放源 1")]
    drive_group = [PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="quark")]

    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘剧集"),
        playlist=drive_group,
        playlists=[first_group, drive_group],
        playlist_index=1,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        playback_loader=lambda item: PlaybackLoadResult(
            replacement_playlist=[PlayItem(title="S1 - 1", url="http://m/1.mp4", play_source="quark")],
            replacement_start_index=0,
        ),
    )

    window = PlayerWindow(controller, config=None, save_config=lambda: None)
    qtbot.addWidget(window)

    window.open_session(session)

    assert [item.title for item in window.session.playlists[0]] == ["第1集"]
    assert [item.title for item in window.session.playlists[1]] == ["S1 - 1"]
```

- [ ] **Step 2: Run the player-window and related regression suites**

Run:

```bash
uv run pytest tests/test_player_window_ui.py tests/test_api_client.py tests/test_app.py tests/test_telegram_search_controller.py tests/test_spider_plugin_manager.py -q
```

Expected: PASS

- [ ] **Step 3: Run the full spider-drive related regression suite**

Run:

```bash
uv run pytest tests/test_player_window_ui.py tests/test_spider_plugin_controller.py tests/test_api_client.py tests/test_app.py tests/test_telegram_search_controller.py tests/test_spider_plugin_manager.py -q
```

Expected: PASS

- [ ] **Step 4: Commit the final regression coverage**

Run:

```bash
git add tests/test_player_window_ui.py
git commit -m "test: cover spider drive route playlist replacement"
```
