# Spider Plugin Local Playback Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add local playback-progress persistence for spider-plugin playback so plugin videos reopen at the previous episode and timestamp after restart without calling the backend `/history` API.

**Architecture:** Keep persistence inside the spider-plugin layer by extending `SpiderPluginRepository` with a plugin playback-history table. Thread plugin-specific history hooks through `OpenPlayerRequest` into `PlayerController`, and leave all non-plugin history behavior unchanged. Reuse `HistoryRecord` and `resolve_resume_index()` so plugin resume semantics match the existing player flow.

**Tech Stack:** Python, sqlite3, PySide6, pytest

---

## File Map

- `src/atv_player/models.py`
  Adds optional request/session hooks for plugin-local playback history.
- `src/atv_player/controllers/player_controller.py`
  Loads plugin-local history during session creation and saves plugin-local progress during reporting.
- `src/atv_player/plugins/repository.py`
  Stores plugin playback-progress rows in `app.db` and deletes them with their plugin.
- `src/atv_player/plugins/controller.py`
  Attaches plugin-local history callbacks to spider-plugin player requests.
- `src/atv_player/plugins/__init__.py`
  Binds repository-backed loader/saver closures when constructing `SpiderPluginController`.
- `tests/test_storage.py`
  Verifies repository persistence and deletion behavior for plugin playback history.
- `tests/test_player_controller.py`
  Verifies `PlayerController` prefers plugin-local history and saves to the plugin-local callback without using the API.
- `tests/test_spider_plugin_controller.py`
  Verifies plugin requests expose the new history hooks while keeping `use_local_history=False`.
- `tests/test_spider_plugin_manager.py`
  Verifies manager-built plugin controllers wire repository-backed history callbacks.
- `tests/test_app.py`
  Verifies plugin restore rebuilds a request that carries plugin-local history into player-session creation.
- `tests/test_main_window_ui.py`
  Updates fake player-controller signatures so the UI tests keep matching `MainWindow`’s player-session call contract.

### Task 1: Add Spider Plugin Playback-History Persistence

**Files:**
- Modify: `src/atv_player/plugins/repository.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing repository tests**

Add these tests near the existing `SpiderPluginRepository` coverage in `tests/test_storage.py`:

```python
def test_spider_plugin_repository_round_trip_playback_history(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SpiderPluginRepository(db_path)
    plugin = repo.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")

    repo.save_playback_history(
        plugin.id,
        "detail-1",
        {
            "vodName": "红果短剧",
            "vodPic": "poster-1",
            "vodRemarks": "第2集",
            "episode": 1,
            "episodeUrl": "https://media.example/2.m3u8",
            "position": 45000,
            "opening": 5000,
            "ending": 10000,
            "speed": 1.25,
            "createTime": 1713206400000,
        },
    )

    history = repo.get_playback_history(plugin.id, "detail-1")

    assert history is not None
    assert history.key == "detail-1"
    assert history.vod_name == "红果短剧"
    assert history.episode == 1
    assert history.position == 45000
    assert history.speed == 1.25


def test_spider_plugin_repository_updates_existing_playback_history_and_deletes_with_plugin(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SpiderPluginRepository(db_path)
    plugin = repo.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")

    repo.save_playback_history(
        plugin.id,
        "detail-1",
        {
            "vodName": "旧标题",
            "vodPic": "poster-old",
            "vodRemarks": "第1集",
            "episode": 0,
            "episodeUrl": "https://media.example/1.m3u8",
            "position": 15000,
            "opening": 0,
            "ending": 0,
            "speed": 1.0,
            "createTime": 1713206400000,
        },
    )
    repo.save_playback_history(
        plugin.id,
        "detail-1",
        {
            "vodName": "新标题",
            "vodPic": "poster-new",
            "vodRemarks": "第3集",
            "episode": 2,
            "episodeUrl": "https://media.example/3.m3u8",
            "position": 90000,
            "opening": 8000,
            "ending": 16000,
            "speed": 1.5,
            "createTime": 1713206500000,
        },
    )

    updated = repo.get_playback_history(plugin.id, "detail-1")
    assert updated is not None
    assert updated.vod_name == "新标题"
    assert updated.episode == 2
    assert updated.position == 90000
    assert updated.speed == 1.5

    repo.delete_plugin(plugin.id)

    assert repo.get_playback_history(plugin.id, "detail-1") is None
```

- [ ] **Step 2: Run the repository tests to verify they fail**

Run:

```bash
uv run pytest tests/test_storage.py::test_spider_plugin_repository_round_trip_playback_history tests/test_storage.py::test_spider_plugin_repository_updates_existing_playback_history_and_deletes_with_plugin -q
```

Expected: FAIL with `AttributeError` because `SpiderPluginRepository` does not yet define `save_playback_history()` or `get_playback_history()`.

- [ ] **Step 3: Implement the playback-history table and repository methods**

Update `src/atv_player/plugins/repository.py` with a new table and repository API:

```python
from atv_player.models import HistoryRecord, SpiderPluginConfig, SpiderPluginLogEntry


class SpiderPluginRepository:
    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS spider_plugin_playback_history (
                    plugin_id INTEGER NOT NULL,
                    vod_id TEXT NOT NULL,
                    vod_name TEXT NOT NULL DEFAULT '',
                    vod_pic TEXT NOT NULL DEFAULT '',
                    vod_remarks TEXT NOT NULL DEFAULT '',
                    episode INTEGER NOT NULL DEFAULT 0,
                    episode_url TEXT NOT NULL DEFAULT '',
                    position INTEGER NOT NULL DEFAULT 0,
                    opening INTEGER NOT NULL DEFAULT 0,
                    ending INTEGER NOT NULL DEFAULT 0,
                    speed REAL NOT NULL DEFAULT 1.0,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (plugin_id, vod_id)
                )
                """
            )

    def delete_plugin(self, plugin_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM spider_plugin_playback_history WHERE plugin_id = ?", (plugin_id,))
            conn.execute("DELETE FROM spider_plugin_logs WHERE plugin_id = ?", (plugin_id,))
            conn.execute("DELETE FROM spider_plugins WHERE id = ?", (plugin_id,))

    def get_playback_history(self, plugin_id: int, vod_id: str) -> HistoryRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT plugin_id, vod_id, vod_name, vod_pic, vod_remarks,
                       episode, episode_url, position, opening, ending, speed, updated_at
                FROM spider_plugin_playback_history
                WHERE plugin_id = ? AND vod_id = ?
                """,
                (plugin_id, vod_id),
            ).fetchone()
        if row is None:
            return None
        return HistoryRecord(
            id=0,
            key=row[1],
            vod_name=row[2],
            vod_pic=row[3],
            vod_remarks=row[4],
            episode=row[5],
            episode_url=row[6],
            position=row[7],
            opening=row[8],
            ending=row[9],
            speed=row[10],
            create_time=row[11],
        )

    def save_playback_history(self, plugin_id: int, vod_id: str, payload: dict[str, object]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO spider_plugin_playback_history (
                    plugin_id, vod_id, vod_name, vod_pic, vod_remarks,
                    episode, episode_url, position, opening, ending, speed, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plugin_id, vod_id) DO UPDATE SET
                    vod_name = excluded.vod_name,
                    vod_pic = excluded.vod_pic,
                    vod_remarks = excluded.vod_remarks,
                    episode = excluded.episode,
                    episode_url = excluded.episode_url,
                    position = excluded.position,
                    opening = excluded.opening,
                    ending = excluded.ending,
                    speed = excluded.speed,
                    updated_at = excluded.updated_at
                """,
                (
                    plugin_id,
                    vod_id,
                    str(payload.get("vodName", "")),
                    str(payload.get("vodPic", "")),
                    str(payload.get("vodRemarks", "")),
                    int(payload.get("episode", 0)),
                    str(payload.get("episodeUrl", "")),
                    int(payload.get("position", 0)),
                    int(payload.get("opening", 0)),
                    int(payload.get("ending", 0)),
                    float(payload.get("speed", 1.0)),
                    int(payload.get("createTime", 0)),
                ),
            )
```

- [ ] **Step 4: Run the repository tests to verify they pass**

Run:

```bash
uv run pytest tests/test_storage.py::test_spider_plugin_repository_round_trip_playback_history tests/test_storage.py::test_spider_plugin_repository_updates_existing_playback_history_and_deletes_with_plugin -q
```

Expected: PASS

- [ ] **Step 5: Commit the repository work**

Run:

```bash
git add tests/test_storage.py src/atv_player/plugins/repository.py
git commit -m "feat: persist spider plugin playback history"
```

### Task 2: Thread Plugin-Local History Through Player Requests and Sessions

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/controllers/player_controller.py`
- Test: `tests/test_player_controller.py`

- [ ] **Step 1: Write the failing session-restore test**

Add this test to `tests/test_player_controller.py`:

```python
def test_player_controller_prefers_plugin_local_history_loader() -> None:
    api = FakeApiClient()
    api.history = HistoryRecord(
        id=1,
        key="movie-1",
        vod_name="API Movie",
        vod_pic="api-pic",
        vod_remarks="Episode 1",
        episode=0,
        episode_url="1.m3u8",
        position=1000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1,
    )
    controller = PlayerController(api)
    vod = VodItem(vod_id="movie-1", vod_name="Plugin Movie", vod_pic="plugin-pic")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8"), PlayItem(title="Episode 2", url="2.m3u8")]

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        use_local_history=False,
        playback_history_loader=lambda: HistoryRecord(
            id=0,
            key="plugin:movie-1",
            vod_name="Plugin Movie",
            vod_pic="plugin-pic",
            vod_remarks="Episode 2",
            episode=1,
            episode_url="2.m3u8",
            position=45000,
            opening=5000,
            ending=10000,
            speed=1.25,
            create_time=2,
        ),
    )

    assert api.history_calls == []
    assert session.start_index == 1
    assert session.start_position_seconds == 45
    assert session.speed == 1.25
    assert session.opening_seconds == 5
    assert session.ending_seconds == 10
```

- [ ] **Step 2: Run the restore test to verify it fails**

Run:

```bash
uv run pytest tests/test_player_controller.py::test_player_controller_prefers_plugin_local_history_loader -q
```

Expected: FAIL with `TypeError` because `create_session()` does not yet accept `playback_history_loader`.

- [ ] **Step 3: Add request/session hook types and session-loader support**

Update `src/atv_player/models.py` and `src/atv_player/controllers/player_controller.py`:

```python
# src/atv_player/models.py
class OpenPlayerRequest:
    ...
    playback_history_loader: Callable[[], HistoryRecord | None] | None = None
    playback_history_saver: Callable[[dict[str, object]], None] | None = None


# src/atv_player/controllers/player_controller.py
class PlayerSession:
    ...
    playback_history_saver: Callable[[dict[str, object]], None] | None = None


def create_session(
    ...,
    playback_history_loader: Callable[[], HistoryRecord | None] | None = None,
    playback_history_saver: Callable[[dict[str, object]], None] | None = None,
):
    history = playback_history_loader() if playback_history_loader is not None else None
    if history is None and (use_local_history or restore_history):
        history = self._api_client.get_history(vod.vod_id)
    ...
    return PlayerSession(
        ...,
        playback_history_saver=playback_history_saver,
    )
```

- [ ] **Step 4: Run the restore test to verify it passes**

Run:

```bash
uv run pytest tests/test_player_controller.py::test_player_controller_prefers_plugin_local_history_loader -q
```

Expected: PASS

- [ ] **Step 5: Write the failing plugin-progress-save test**

Add this test to `tests/test_player_controller.py`:

```python
def test_player_controller_reports_progress_to_plugin_local_saver_without_api_history() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="plugin-vod-1", vod_name="Plugin Movie", vod_pic="poster-plugin")
    playlist = [PlayItem(title="Episode 1", url="https://media.example/1.m3u8", vod_id="ep-1")]
    saved_payloads: list[dict[str, object]] = []

    session = controller.create_session(
        vod,
        playlist,
        clicked_index=0,
        use_local_history=False,
        playback_history_saver=lambda payload: saved_payloads.append(payload),
    )

    controller.report_progress(
        session,
        current_index=0,
        position_seconds=90,
        speed=1.5,
        opening_seconds=15,
        ending_seconds=30,
        paused=False,
    )

    assert api.saved_payloads == []
    assert saved_payloads == [
        {
            "cid": 0,
            "key": "plugin-vod-1",
            "vodName": "Plugin Movie",
            "vodPic": "poster-plugin",
            "vodRemarks": "Episode 1",
            "episode": 0,
            "episodeUrl": "https://media.example/1.m3u8",
            "position": 90000,
            "opening": 15000,
            "ending": 30000,
            "speed": 1.5,
            "createTime": saved_payloads[0]["createTime"],
        }
    ]
    assert isinstance(saved_payloads[0]["createTime"], int)
```

- [ ] **Step 6: Run the save test to verify it fails**

Run:

```bash
uv run pytest tests/test_player_controller.py::test_player_controller_reports_progress_to_plugin_local_saver_without_api_history -q
```

Expected: FAIL because `report_progress()` does not yet call a plugin-local saver.

- [ ] **Step 7: Save plugin-local progress before the API-history branch**

Update `src/atv_player/controllers/player_controller.py`:

```python
def report_progress(...):
    ...
    payload = {
        "cid": 0,
        "key": session.vod.vod_id,
        "vodName": session.vod.vod_name,
        "vodPic": session.vod.vod_pic,
        "vodRemarks": current_item.title,
        "episode": current_index,
        "episodeUrl": current_item.url,
        "position": position_ms,
        "opening": opening_seconds * 1000,
        "ending": ending_seconds * 1000,
        "speed": speed,
        "createTime": int(time() * 1000),
    }
    if session.playback_history_saver is not None:
        session.playback_history_saver(payload)
    if not session.use_local_history:
        return
    self._api_client.save_history(payload)
```

- [ ] **Step 8: Run the focused player-controller tests**

Run:

```bash
uv run pytest tests/test_player_controller.py::test_player_controller_prefers_plugin_local_history_loader tests/test_player_controller.py::test_player_controller_reports_progress_to_plugin_local_saver_without_api_history tests/test_player_controller.py::test_player_controller_reports_progress_via_session_hook_without_saving_history -q
```

Expected: PASS

- [ ] **Step 9: Commit the player-controller plumbing**

Run:

```bash
git add tests/test_player_controller.py src/atv_player/models.py src/atv_player/controllers/player_controller.py
git commit -m "feat: add plugin local playback history hooks"
```

### Task 3: Wire Spider Plugin Requests to Repository-Backed History Callbacks

**Files:**
- Modify: `src/atv_player/plugins/controller.py`
- Modify: `src/atv_player/plugins/__init__.py`
- Test: `tests/test_spider_plugin_controller.py`
- Test: `tests/test_spider_plugin_manager.py`

- [ ] **Step 1: Write the failing controller-hook test**

Add this test to `tests/test_spider_plugin_controller.py`:

```python
def test_controller_build_request_attaches_local_playback_history_callbacks() -> None:
    load_calls: list[str] = []
    save_calls: list[tuple[str, dict[str, object]]] = []
    controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        playback_history_loader=lambda vod_id: load_calls.append(vod_id) or None,
        playback_history_saver=lambda vod_id, payload: save_calls.append((vod_id, payload)),
    )

    request = controller.build_request("/detail/1")

    assert request.use_local_history is False
    assert request.playback_history_loader is not None
    assert request.playback_history_saver is not None

    request.playback_history_loader()
    request.playback_history_saver({"position": 45000})

    assert load_calls == ["/detail/1"]
    assert save_calls == [("/detail/1", {"position": 45000})]
```

- [ ] **Step 2: Run the controller-hook test to verify it fails**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py::test_controller_build_request_attaches_local_playback_history_callbacks -q
```

Expected: FAIL because `SpiderPluginController` does not yet accept or attach playback-history callbacks.

- [ ] **Step 3: Attach bound loader/saver closures inside `SpiderPluginController.build_request()`**

Update `src/atv_player/plugins/controller.py`:

```python
class SpiderPluginController:
    def __init__(
        self,
        spider,
        plugin_name: str,
        search_enabled: bool,
        playback_history_loader: Callable[[str], HistoryRecord | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        ...
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        ...
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
            ...,
            use_local_history=False,
            playback_loader=self._resolve_play_item,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )
```

- [ ] **Step 4: Run the controller-hook test to verify it passes**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py::test_controller_build_request_attaches_local_playback_history_callbacks -q
```

Expected: PASS

- [ ] **Step 5: Write the failing manager-wiring test**

Add this test to `tests/test_spider_plugin_manager.py`:

```python
class FakeSpider:
    def init(self, extend: str = "") -> None:
        return None

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "红果短剧",
                    "vod_play_url": "第1集$https://media.example/1.m3u8",
                }
            ]
        }


class HistoryLoader(FakeLoader):
    def load(self, config: SpiderPluginConfig, force_refresh: bool = False) -> LoadedSpiderPlugin:
        loaded = super().load(config, force_refresh=force_refresh)
        return LoadedSpiderPlugin(
            config=loaded.config,
            spider=FakeSpider(),
            plugin_name="红果短剧",
            search_enabled=False,
        )


def test_manager_load_enabled_plugins_wires_repository_playback_history_callbacks(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")
    repository.save_playback_history(
        plugin.id,
        "detail-1",
        {
            "vodName": "红果短剧",
            "vodPic": "poster",
            "vodRemarks": "第1集",
            "episode": 0,
            "episodeUrl": "https://media.example/1.m3u8",
            "position": 45000,
            "opening": 0,
            "ending": 0,
            "speed": 1.0,
            "createTime": 1713206400000,
        },
    )
    manager = SpiderPluginManager(repository, HistoryLoader())

    definitions = manager.load_enabled_plugins()
    request = definitions[0].controller.build_request("detail-1")

    assert request.playback_history_loader is not None
    assert request.playback_history_loader().position == 45000

    assert request.playback_history_saver is not None
    request.playback_history_saver(
        {
            "vodName": "红果短剧",
            "vodPic": "poster",
            "vodRemarks": "第1集",
            "episode": 0,
            "episodeUrl": "https://media.example/1.m3u8",
            "position": 90000,
            "opening": 0,
            "ending": 0,
            "speed": 1.0,
            "createTime": 1713206500000,
        }
    )
    assert repository.get_playback_history(plugin.id, "detail-1").position == 90000
```

- [ ] **Step 6: Run the manager-wiring test to verify it fails**

Run:

```bash
uv run pytest tests/test_spider_plugin_manager.py::test_manager_load_enabled_plugins_wires_repository_playback_history_callbacks -q
```

Expected: FAIL because `SpiderPluginManager.load_enabled_plugins()` does not yet pass repository-backed callbacks into `SpiderPluginController`.

- [ ] **Step 7: Bind repository-backed closures in `SpiderPluginManager.load_enabled_plugins()`**

Update `src/atv_player/plugins/__init__.py`:

```python
controller = SpiderPluginController(
    loaded.spider,
    plugin_name=title,
    search_enabled=loaded.search_enabled,
    playback_history_loader=lambda vod_id, plugin_id=plugin.id: self._repository.get_playback_history(
        plugin_id,
        vod_id,
    ),
    playback_history_saver=lambda vod_id, payload, plugin_id=plugin.id: self._repository.save_playback_history(
        plugin_id,
        vod_id,
        payload,
    ),
)
```

- [ ] **Step 8: Run the focused plugin tests**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py::test_controller_build_request_attaches_local_playback_history_callbacks tests/test_spider_plugin_manager.py::test_manager_load_enabled_plugins_wires_repository_playback_history_callbacks -q
```

Expected: PASS

- [ ] **Step 9: Commit the plugin wiring**

Run:

```bash
git add tests/test_spider_plugin_controller.py tests/test_spider_plugin_manager.py src/atv_player/plugins/controller.py src/atv_player/plugins/__init__.py
git commit -m "feat: wire spider plugin local playback resume"
```

### Task 4: Pass Plugin-Local History Through Main-Window Restore and Update Test Doubles

**Files:**
- Modify: `src/atv_player/ui/main_window.py`
- Test: `tests/test_app.py`
- Test: `tests/test_main_window_ui.py`

- [ ] **Step 1: Write the failing app restore test**

Add this test to `tests/test_app.py` near the other restore-path coverage:

```python
def test_main_window_restore_last_player_routes_plugin_detail_to_plugin_controller_with_playback_history_loader(qtbot, monkeypatch) -> None:
    class RestoreBrowseController:
        def build_request_from_detail(self, vod_id: str):
            raise AssertionError(f"browse restore should not be used for {vod_id}")

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.opened: list[tuple[object, bool]] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    class RestorePluginController:
        def load_categories(self):
            return []

        def load_items(self, category_id: str, page: int):
            return [], 0

        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="插件电影"),
                playlist=[PlayItem(title="第2集", url="https://media.example/2.m3u8")],
                clicked_index=0,
                source_kind="plugin",
                source_mode="detail",
                source_vod_id=vod_id,
                use_local_history=False,
                playback_history_loader=lambda: HistoryRecord(
                    id=0,
                    key=vod_id,
                    vod_name="插件电影",
                    vod_pic="poster",
                    vod_remarks="第2集",
                    episode=0,
                    episode_url="https://media.example/2.m3u8",
                    position=45000,
                    opening=0,
                    ending=0,
                    speed=1.0,
                    create_time=1713206400000,
                ),
            )

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_source="plugin",
        last_playback_source_key="plugin-1",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": RestorePluginController(), "search_enabled": False}],
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    session = window.player_window.opened[0][0]
    assert session["vod"].vod_name == "插件电影"
    assert session["use_local_history"] is False
    assert session["playback_history_loader"] is not None
    assert session["playback_history_loader"]().position == 45000
    assert window.player_window.opened[0][1] is True
```

- [ ] **Step 2: Run the app restore test to verify it fails**

Run:

```bash
uv run pytest tests/test_app.py::test_main_window_restore_last_player_routes_plugin_detail_to_plugin_controller_with_playback_history_loader -q
```

Expected: FAIL because `MainWindow._create_player_session()` and the fake player controllers do not yet accept/pass `playback_history_loader` and `playback_history_saver`.

- [ ] **Step 3: Pass the new request hooks through `MainWindow` and update fake player controllers**

Update `src/atv_player/ui/main_window.py`, `tests/test_app.py`, and `tests/test_main_window_ui.py`:

```python
# src/atv_player/ui/main_window.py
def _create_player_session(self, request):
    return self.player_controller.create_session(
        request.vod,
        request.playlist,
        request.clicked_index,
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


# tests/test_app.py and tests/test_main_window_ui.py
class FakePlayerController:
    def create_session(
        self,
        vod,
        playlist,
        clicked_index: int,
        detail_resolver=None,
        resolved_vod_by_id=None,
        use_local_history=True,
        restore_history=False,
        playback_loader=None,
        playback_progress_reporter=None,
        playback_stopper=None,
        playback_history_loader=None,
        playback_history_saver=None,
    ):
        return {
            "vod": vod,
            "playlist": playlist,
            "clicked_index": clicked_index,
            "use_local_history": use_local_history,
            "restore_history": restore_history,
            "playback_history_loader": playback_history_loader,
            "playback_history_saver": playback_history_saver,
        }
```

- [ ] **Step 4: Run the focused app and UI tests**

Run:

```bash
uv run pytest tests/test_app.py::test_main_window_restore_last_player_routes_plugin_detail_to_plugin_controller_with_playback_history_loader tests/test_main_window_ui.py::test_main_window_inserts_dynamic_spider_tabs_before_browse -q
```

Expected: PASS

- [ ] **Step 5: Run the end-to-end regression slice**

Run:

```bash
uv run pytest tests/test_storage.py tests/test_player_controller.py tests/test_spider_plugin_controller.py tests/test_spider_plugin_manager.py tests/test_app.py -q
```

Expected: PASS

- [ ] **Step 6: Commit the main-window pass-through and test-double updates**

Run:

```bash
git add tests/test_app.py tests/test_main_window_ui.py src/atv_player/ui/main_window.py
git commit -m "test: cover spider plugin local resume flow"
```

## Self-Review

- Spec coverage:
  - local persistence in `app.db`: Task 1
  - plugin-only hooks on requests/sessions: Task 2
  - spider-plugin request and manager wiring: Task 3
  - restart restore path consuming plugin-local history: Task 4
  - no backend `/history` usage for plugins: Tasks 2, 3, 4
- Placeholder scan:
  - no `TODO`, `TBD`, or “similar to” references remain
  - every code-edit step includes concrete code
  - every verification step includes an exact command and expected outcome
- Type consistency:
  - `playback_history_loader` is always `Callable[[], HistoryRecord | None] | None` once it reaches `OpenPlayerRequest` / `MainWindow` / `PlayerController`
  - `playback_history_saver` is always `Callable[[dict[str, object]], None] | None` once it reaches `OpenPlayerRequest` / `PlayerSession` / `PlayerController`

