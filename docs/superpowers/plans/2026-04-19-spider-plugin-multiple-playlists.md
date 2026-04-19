# Spider Plugin Multiple Playlists Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve spider-plugin routes as separate playlists and let the player switch between them without breaking existing single-playlist sources.

**Architecture:** Keep `playlist` as the active list for compatibility, and add grouped `playlists` plus `playlist_index` to request/session models. Normalize legacy single-playlist requests in the player-controller path, then update the spider controller to emit grouped playlists and the player window to render a route selector that swaps the active list.

**Tech Stack:** Python, PySide6, pytest, existing player/controller/request dataclasses

---

## File Structure

- Modify: `src/atv_player/models.py`
  Add grouped playlist fields to `OpenPlayerRequest` while keeping `playlist` as the active list.
- Modify: `src/atv_player/controllers/player_controller.py`
  Extend `PlayerSession`, normalize grouped playlists in `create_session()`, and keep history/reporting scoped to the active list.
- Modify: `src/atv_player/ui/main_window.py`
  Pass grouped playlist fields from `OpenPlayerRequest` into `PlayerController.create_session()`.
- Modify: `src/atv_player/plugins/controller.py`
  Build grouped spider playlists by route and attach the first group as the active playlist.
- Modify: `src/atv_player/ui/player_window.py`
  Add the route selector, render the active group, and switch playback within the selected group.
- Modify: `tests/test_spider_plugin_controller.py`
  Cover grouped spider playlist construction and active-playlist request wiring.
- Modify: `tests/test_player_controller.py`
  Cover grouped playlist normalization and active-group session state.
- Modify: `tests/test_player_window_ui.py`
  Cover route selector rendering, route switching, and group-bounded next/previous navigation.

### Task 1: Normalize Grouped Playlists In Request And Session Flow

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/controllers/player_controller.py`
- Modify: `src/atv_player/ui/main_window.py`
- Test: `tests/test_player_controller.py`

- [ ] **Step 1: Write the failing normalization tests**

Add these tests to `tests/test_player_controller.py`:

```python
def test_player_controller_normalizes_single_playlist_into_one_group() -> None:
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="movie-1", vod_name="Movie")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8"), PlayItem(title="Episode 2", url="2.m3u8")]

    session = controller.create_session(vod, playlist, clicked_index=1)

    assert len(session.playlists) == 1
    assert session.playlist_index == 0
    assert [item.title for item in session.playlists[0]] == ["Episode 1", "Episode 2"]
    assert session.playlist is session.playlists[0]
    assert session.start_index == 1


def test_player_controller_uses_selected_group_as_active_playlist() -> None:
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="plugin-1", vod_name="Plugin Movie")
    first_group = [PlayItem(title="第1集", url="http://m/1.m3u8", play_source="备用线")]
    second_group = [
        PlayItem(title="第1集", url="http://b/1.m3u8", play_source="极速线"),
        PlayItem(title="第2集", url="http://b/2.m3u8", play_source="极速线"),
    ]

    session = controller.create_session(
        vod,
        playlist=second_group,
        clicked_index=1,
        playlists=[first_group, second_group],
        playlist_index=1,
    )

    assert len(session.playlists) == 2
    assert session.playlist_index == 1
    assert session.playlist is second_group
    assert [item.title for item in session.playlist] == ["第1集", "第2集"]
    assert session.start_index == 1
```

- [ ] **Step 2: Run the player-controller tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_controller.py -q
```

Expected: FAIL because `PlayerSession` and `PlayerController.create_session()` do not yet expose `playlists` or `playlist_index`.

- [ ] **Step 3: Add grouped playlist fields to the models**

Update `src/atv_player/models.py`:

```python
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
    playback_loader: Callable[[PlayItem], None] | None = None
    playback_progress_reporter: Callable[[PlayItem, int, bool], None] | None = None
    playback_stopper: Callable[[PlayItem], None] | None = None
    playback_history_loader: Callable[[], HistoryRecord | None] | None = None
    playback_history_saver: Callable[[dict[str, object]], None] | None = None
```

- [ ] **Step 4: Add grouped playlist normalization to the player controller**

Update `src/atv_player/controllers/player_controller.py`:

```python
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
    playback_loader: Callable[[PlayItem], None] | None = None
    playback_progress_reporter: Callable[[PlayItem, int, bool], None] | None = None
    playback_stopper: Callable[[PlayItem], None] | None = None
    playback_history_saver: Callable[[dict[str, object]], None] | None = None


class PlayerController:
    def _normalize_playlists(
        self,
        playlist: list[PlayItem],
        playlists: list[list[PlayItem]] | None,
        playlist_index: int,
    ) -> tuple[list[list[PlayItem]], int, list[PlayItem]]:
        normalized = [group for group in (playlists or []) if group]
        if not normalized:
            normalized = [playlist]
        playlist_index = max(0, min(playlist_index, len(normalized) - 1))
        active_playlist = normalized[playlist_index]
        return normalized, playlist_index, active_playlist

    def create_session(
        self,
        vod: VodItem,
        playlist: list[PlayItem],
        clicked_index: int,
        playlists: list[list[PlayItem]] | None = None,
        playlist_index: int = 0,
        detail_resolver: Callable[[PlayItem], VodItem | None] | None = None,
        resolved_vod_by_id: dict[str, VodItem] | None = None,
        use_local_history: bool = True,
        restore_history: bool = False,
        playback_loader: Callable[[PlayItem], None] | None = None,
        playback_progress_reporter: Callable[[PlayItem, int, bool], None] | None = None,
        playback_stopper: Callable[[PlayItem], None] | None = None,
        playback_history_loader: Callable[[], HistoryRecord | None] | None = None,
        playback_history_saver: Callable[[dict[str, object]], None] | None = None,
    ) -> PlayerSession:
        normalized_playlists, playlist_index, active_playlist = self._normalize_playlists(
            playlist,
            playlists,
            playlist_index,
        )
        history = playback_history_loader() if playback_history_loader is not None else None
        if history is None and (use_local_history or restore_history):
            history = self._api_client.get_history(vod.vod_id)
        start_index = resolve_resume_index(history, active_playlist, clicked_index)
        matched_history = history is not None and start_index == history.episode
        if matched_history and history is not None:
            position_seconds = int(history.position / 1000)
            speed = history.speed
        else:
            position_seconds = 0
            speed = 1.0
        return PlayerSession(
            vod=vod,
            playlist=active_playlist,
            playlists=normalized_playlists,
            playlist_index=playlist_index,
            start_index=start_index,
            start_position_seconds=position_seconds,
            speed=speed,
            opening_seconds=int((history.opening if history else 0) / 1000),
            ending_seconds=int((history.ending if history else 0) / 1000),
            detail_resolver=detail_resolver,
            resolved_vod_by_id=dict(resolved_vod_by_id or {}),
            use_local_history=use_local_history,
            playback_loader=playback_loader,
            playback_progress_reporter=playback_progress_reporter,
            playback_stopper=playback_stopper,
            playback_history_saver=playback_history_saver,
        )
```

- [ ] **Step 5: Pass grouped playlist fields from the request into session creation**

Update `src/atv_player/ui/main_window.py`:

```python
    def _create_player_session(self, request):
        return self.player_controller.create_session(
            request.vod,
            request.playlist,
            request.clicked_index,
            playlists=request.playlists,
            playlist_index=request.playlist_index,
            detail_resolver=request.detail_resolver,
            resolved_vod_by_id=request.resolved_vod_by_id,
            use_local_history=request.use_local_history,
            restore_history=request.restore_history,
            playback_loader=request.playback_loader,
            playback_progress_reporter=request.playback_progress_reporter,
            playback_stopper=request.playback_stopper,
            playback_history_loader=request.playback_history_loader,
            playback_history_saver=request.playback_history_saver,
        )
```

- [ ] **Step 6: Run the player-controller tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_controller.py -q
```

Expected: PASS

- [ ] **Step 7: Commit the normalization work**

Run:

```bash
git add tests/test_player_controller.py src/atv_player/models.py src/atv_player/controllers/player_controller.py src/atv_player/ui/main_window.py
git commit -m "feat: add grouped playlist session model"
```

### Task 2: Return Grouped Playlists From The Spider Plugin Controller

**Files:**
- Modify: `src/atv_player/plugins/controller.py`
- Test: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Write the failing spider grouped-playlist tests**

Replace the flat-playlist expectations in `tests/test_spider_plugin_controller.py` with:

```python
def test_controller_build_request_exposes_grouped_route_playlists() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    request = controller.build_request("/detail/1")

    assert request.use_local_history is False
    assert request.playlist_index == 0
    assert len(request.playlists) == 2
    assert [item.title for item in request.playlists[0]] == ["第1集", "第2集"]
    assert [item.title for item in request.playlists[1]] == ["第3集"]
    assert request.playlist is request.playlists[0]

    first = request.playlists[0][0]
    second = request.playlists[0][1]
    third = request.playlists[1][0]

    assert first.play_source == "备用线"
    assert first.index == 0
    assert first.vod_id == "/play/1"
    assert second.url == "https://media.example/2.m3u8"
    assert third.play_source == "极速线"
    assert third.index == 0


def test_controller_build_request_defers_player_content_until_episode_load() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.headers == {"Referer": "https://site.example"}
```

- [ ] **Step 2: Run the spider-controller tests to verify they fail**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py -q
```

Expected: FAIL because `_build_playlist()` still returns one flat list and `OpenPlayerRequest` does not yet receive grouped playlist values from the spider controller.

- [ ] **Step 3: Build grouped route playlists in the spider controller**

Update `src/atv_player/plugins/controller.py`:

```python
    def _route_name(self, routes: list[str], group_index: int) -> str:
        route = routes[group_index] if group_index < len(routes) else ""
        route = route.strip()
        return route or f"线路 {group_index + 1}"

    def _build_playlist(self, detail: VodItem) -> list[list[PlayItem]]:
        routes = [item.strip() for item in (detail.vod_play_from or "").split("$$$")]
        groups = (detail.vod_play_url or "").split("$$$")
        playlists: list[list[PlayItem]] = []
        for group_index, group in enumerate(groups):
            route = self._route_name(routes, group_index)
            playlist: list[PlayItem] = []
            for raw_chunk in group.split("#"):
                chunk = raw_chunk.strip()
                if not chunk:
                    continue
                title, separator, value = chunk.partition("$")
                if not separator:
                    title = chunk
                    value = chunk
                clean_value = value.strip()
                playlist.append(
                    PlayItem(
                        title=title.strip() or clean_value or f"选集 {len(playlist) + 1}",
                        url=clean_value if _looks_like_media_url(clean_value) else "",
                        vod_id="" if _looks_like_media_url(clean_value) else clean_value,
                        index=len(playlist),
                        play_source=route,
                    )
                )
            if playlist:
                playlists.append(playlist)
        return playlists
```

- [ ] **Step 4: Attach grouped playlists to the spider request**

Update `src/atv_player/plugins/controller.py` inside `build_request()`:

```python
        playlists = self._build_playlist(detail)
        if not playlists:
            raise ValueError(f"没有可播放的项目: {detail.vod_name}")
        playlist = playlists[0]
        history_loader = None
        history_saver = None
        if self._playback_history_loader is not None:
            history_loader = lambda source_vod_id=detail.vod_id: self._playback_history_loader(source_vod_id)
        if self._playback_history_saver is not None:
            history_saver = lambda payload, source_vod_id=detail.vod_id: self._playback_history_saver(
                source_vod_id,
                payload,
            )
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            playlists=playlists,
            playlist_index=0,
            clicked_index=0,
            source_kind="plugin",
            source_mode="detail",
            source_vod_id=detail.vod_id,
            use_local_history=False,
            playback_loader=self._resolve_play_item,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )
```

- [ ] **Step 5: Run the spider-controller tests to verify they pass**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py -q
```

Expected: PASS

- [ ] **Step 6: Commit the spider grouped-playlist work**

Run:

```bash
git add tests/test_spider_plugin_controller.py src/atv_player/plugins/controller.py
git commit -m "feat: preserve spider routes as grouped playlists"
```

### Task 3: Add Route Switching To The Player Window

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing player-window grouped-playlist tests**

Add these tests to `tests/test_player_window_ui.py`:

```python
def test_player_window_hides_route_selector_for_single_group(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert isinstance(window.playlist_group_combo, QComboBox)
    assert window.playlist_group_combo.isHidden() is True


def test_player_window_renders_route_selector_and_switches_active_group(qtbot) -> None:
    controller = FakePlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    video = RecordingVideo()
    window.video = video
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="红果短剧"),
        playlist=[
            PlayItem(title="第1集", url="http://a/1.m3u8", play_source="备用线"),
            PlayItem(title="第2集", url="http://a/2.m3u8", play_source="备用线"),
        ],
        playlists=[
            [
                PlayItem(title="第1集", url="http://a/1.m3u8", play_source="备用线"),
                PlayItem(title="第2集", url="http://a/2.m3u8", play_source="备用线"),
            ],
            [
                PlayItem(title="第1集", url="http://b/1.m3u8", play_source="极速线"),
                PlayItem(title="第2集", url="http://b/2.m3u8", play_source="极速线"),
            ],
        ],
        playlist_index=0,
        start_index=1,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    assert window.playlist_group_combo.isHidden() is False
    assert [window.playlist_group_combo.itemText(i) for i in range(window.playlist_group_combo.count())] == ["备用线", "极速线"]
    assert [window.playlist.item(i).text() for i in range(window.playlist.count())] == ["第1集", "第2集"]
    assert window.playlist.currentRow() == 1

    window.playlist_group_combo.setCurrentIndex(1)

    assert window.session is not None
    assert window.session.playlist_index == 1
    assert [item.title for item in window.session.playlist] == ["第1集", "第2集"]
    assert window.playlist.currentRow() == 1
    assert video.load_calls[-1][0] == "http://b/2.m3u8"


def test_player_window_next_and_previous_stay_within_active_group(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    video = RecordingVideo()
    window.video = video
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="红果短剧"),
        playlist=[
            PlayItem(title="第1集", url="http://b/1.m3u8", play_source="极速线"),
            PlayItem(title="第2集", url="http://b/2.m3u8", play_source="极速线"),
        ],
        playlists=[
            [PlayItem(title="第1集", url="http://a/1.m3u8", play_source="备用线")],
            [
                PlayItem(title="第1集", url="http://b/1.m3u8", play_source="极速线"),
                PlayItem(title="第2集", url="http://b/2.m3u8", play_source="极速线"),
            ],
        ],
        playlist_index=1,
        start_index=1,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)
    window.play_next()
    assert window.current_index == 1

    window.play_previous()
    assert window.current_index == 0
    assert video.load_calls[-1][0] == "http://b/1.m3u8"
```

- [ ] **Step 2: Run the player-window tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py -q
```

Expected: FAIL because the player window has no route selector or grouped-playlist rendering logic.

- [ ] **Step 3: Add the route selector widgets and helper methods**

Update `src/atv_player/ui/player_window.py`:

```python
        self.playlist_group_combo = QComboBox()
        self.playlist_group_combo.setHidden(True)

        sidebar_layout = QVBoxLayout()
        sidebar_layout.addWidget(self.sidebar_actions_widget)
        sidebar_layout.addWidget(self.playlist_group_combo)
        sidebar_layout.addWidget(self.sidebar_splitter)
```

Add helper methods:

```python
    def _session_playlists(self) -> list[list[PlayItem]]:
        if self.session is None:
            return []
        if self.session.playlists:
            return self.session.playlists
        return [self.session.playlist]

    def _playlist_group_label(self, playlist: list[PlayItem], playlist_index: int) -> str:
        if playlist and playlist[0].play_source:
            return playlist[0].play_source
        return f"线路 {playlist_index + 1}"

    def _render_playlist_group_combo(self) -> None:
        playlists = self._session_playlists()
        self.playlist_group_combo.blockSignals(True)
        self.playlist_group_combo.clear()
        for index, playlist in enumerate(playlists):
            self.playlist_group_combo.addItem(self._playlist_group_label(playlist, index))
        has_multiple_groups = len(playlists) > 1
        self.playlist_group_combo.setHidden(not has_multiple_groups)
        if has_multiple_groups and self.session is not None:
            self.playlist_group_combo.setCurrentIndex(self.session.playlist_index)
        self.playlist_group_combo.blockSignals(False)

    def _render_playlist_items(self) -> None:
        self.playlist.clear()
        if self.session is None:
            return
        for item in self.session.playlist:
            self.playlist.addItem(QListWidgetItem(item.title))
        self.playlist.setCurrentRow(self.current_index)
```

- [ ] **Step 4: Wire session opening and route switching**

Update `src/atv_player/ui/player_window.py`:

```python
        self.playlist_group_combo.currentIndexChanged.connect(self._change_playlist_group)
```

Replace the playlist rendering block in `open_session()` with:

```python
        if not session.playlists:
            session.playlists = [session.playlist]
            session.playlist_index = 0
        self._render_playlist_group_combo()
        self._render_playlist_items()
```

Add route-switch logic:

```python
    def _change_playlist_group(self, playlist_index: int) -> None:
        if self.session is None:
            return
        if not (0 <= playlist_index < len(self.session.playlists)):
            return
        if playlist_index == self.session.playlist_index:
            return
        target_playlist = self.session.playlists[playlist_index]
        if not target_playlist:
            return
        target_row = min(self.current_index, len(target_playlist) - 1)
        self._invalidate_play_item_resolution()
        self.session.playlist_index = playlist_index
        self.session.playlist = target_playlist
        self.current_index = target_row
        self._render_playlist_group_combo()
        self._render_playlist_items()
        self._load_current_item(previous_index=target_row)
        self._refresh_window_title()
```

- [ ] **Step 5: Run the player-window tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_window_ui.py -q
```

Expected: PASS

- [ ] **Step 6: Run the focused grouped-playlist test suite**

Run:

```bash
uv run pytest tests/test_player_controller.py tests/test_spider_plugin_controller.py tests/test_player_window_ui.py -q
```

Expected: PASS

- [ ] **Step 7: Commit the player-window grouped-playlist work**

Run:

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "feat: add player route switching for grouped playlists"
```
