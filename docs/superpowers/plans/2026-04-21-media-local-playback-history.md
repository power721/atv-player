# Media Local Playback History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store Emby and Jellyfin playback history in local SQLite instead of backend `/history`, while keeping spider plugin local history working and showing all local sources in the merged playback-history tab.

**Architecture:** Add a general `LocalPlaybackHistoryRepository` backed by a new `media_playback_history` table, migrate spider-plugin local rows into it, and route spider-plugin, Emby, and Jellyfin playback through the same local loader/saver callbacks. Then update the merged history controller and main-window routing so remote, plugin, Emby, and Jellyfin histories all display and open correctly from one tab.

**Tech Stack:** Python, SQLite, PySide6, pytest, pytest-qt

---

## File Structure

- Create: `src/atv_player/local_playback_history.py`
  General repository for local media playback history plus migration from spider-plugin legacy rows.
- Modify: `src/atv_player/models.py`
  Extend `HistoryRecord` with generic source metadata used by local media sources.
- Modify: `src/atv_player/plugins/__init__.py`
  Switch spider-plugin history callbacks from `SpiderPluginRepository` playback table to the new local repository.
- Modify: `src/atv_player/controllers/emby_controller.py`
  Accept local playback-history callbacks and disable backend `/history`.
- Modify: `src/atv_player/controllers/jellyfin_controller.py`
  Accept local playback-history callbacks and disable backend `/history`.
- Modify: `src/atv_player/controllers/history_controller.py`
  Merge remote history with all local repository records and route delete/clear by source.
- Modify: `src/atv_player/ui/history_page.py`
  Reuse current source-aware history page behavior with `Emby`/`Jellyfin` labels.
- Modify: `src/atv_player/ui/main_window.py`
  Route Emby/Jellyfin history-row opening through their controllers.
- Modify: `src/atv_player/app.py`
  Instantiate and inject the new local playback-history repository.
- Modify: `tests/test_storage.py`
  Add repository tests for local media history and migration.
- Modify: `tests/test_emby_controller.py`
  Add Emby local history callback wiring tests.
- Modify: `tests/test_jellyfin_controller.py`
  Add Jellyfin local history callback wiring tests.
- Modify: `tests/test_player_controller.py`
  Add Emby/Jellyfin local-history restore/save tests.
- Modify: `tests/test_history_controller.py`
  Add Emby/Jellyfin merged-history and deletion coverage.
- Modify: `tests/test_app.py`
  Add merged-history open routing for Emby/Jellyfin and restore-path coverage if needed.

### Task 1: Extend `HistoryRecord` For Generic Local Media Sources

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Write the failing test**

```python
def test_local_playback_history_repository_round_trip_emby_source_metadata(tmp_path: Path) -> None:
    from atv_player.local_playback_history import LocalPlaybackHistoryRepository

    repo = LocalPlaybackHistoryRepository(tmp_path / "app.db")
    repo.save_history(
        "emby",
        "emby-1",
        {
            "vodName": "Emby Movie",
            "vodPic": "poster",
            "vodRemarks": "Episode 2",
            "episode": 1,
            "episodeUrl": "2.m3u8",
            "position": 45000,
            "opening": 0,
            "ending": 0,
            "speed": 1.25,
            "playlistIndex": 1,
            "createTime": 1713206400000,
        },
        source_name="Emby",
    )

    history = repo.get_history("emby", "emby-1")

    assert history is not None
    assert history.source_kind == "emby"
    assert history.source_key == ""
    assert history.source_name == "Emby"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage.py::test_local_playback_history_repository_round_trip_emby_source_metadata -q`
Expected: FAIL because `HistoryRecord` lacks `source_key` and `source_name`, and `LocalPlaybackHistoryRepository` does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
@dataclass(slots=True)
class HistoryRecord:
    ...
    source_kind: str = "remote"
    source_plugin_id: int = 0
    source_plugin_name: str = ""
    source_key: str = ""
    source_name: str = ""
```

- [ ] **Step 4: Run test to verify it still fails for the expected missing repository**

Run: `uv run pytest tests/test_storage.py::test_local_playback_history_repository_round_trip_emby_source_metadata -q`
Expected: FAIL because `LocalPlaybackHistoryRepository` is not defined yet, confirming the model change is in place.

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/models.py tests/test_storage.py
git commit -m "feat: add generic local history source metadata"
```

### Task 2: Create `LocalPlaybackHistoryRepository` And Migrate Legacy Plugin Rows

**Files:**
- Create: `src/atv_player/local_playback_history.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_local_playback_history_repository_round_trip_emby_source_metadata(tmp_path: Path) -> None:
    from atv_player.local_playback_history import LocalPlaybackHistoryRepository

    repo = LocalPlaybackHistoryRepository(tmp_path / "app.db")
    repo.save_history(
        "emby",
        "emby-1",
        {
            "vodName": "Emby Movie",
            "vodPic": "poster",
            "vodRemarks": "Episode 2",
            "episode": 1,
            "episodeUrl": "2.m3u8",
            "position": 45000,
            "opening": 0,
            "ending": 0,
            "speed": 1.25,
            "playlistIndex": 1,
            "createTime": 1713206400000,
        },
        source_name="Emby",
    )

    history = repo.get_history("emby", "emby-1")

    assert history is not None
    assert history.key == "emby-1"
    assert history.source_kind == "emby"
    assert history.source_name == "Emby"


def test_local_playback_history_repository_lists_and_deletes_jellyfin_records(tmp_path: Path) -> None:
    from atv_player.local_playback_history import LocalPlaybackHistoryRepository

    repo = LocalPlaybackHistoryRepository(tmp_path / "app.db")
    repo.save_history(
        "jellyfin",
        "jf-1",
        {
            "vodName": "Jellyfin Movie",
            "vodPic": "poster",
            "vodRemarks": "Episode 1",
            "episode": 0,
            "episodeUrl": "1.m3u8",
            "position": 10000,
            "opening": 0,
            "ending": 0,
            "speed": 1.0,
            "playlistIndex": 0,
            "createTime": 1713206400001,
        },
        source_name="Jellyfin",
    )

    records = repo.list_histories()
    repo.delete_history("jellyfin", "jf-1")

    assert [record.source_kind for record in records] == ["jellyfin"]
    assert repo.get_history("jellyfin", "jf-1") is None


def test_local_playback_history_repository_migrates_spider_plugin_legacy_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            '''
            CREATE TABLE spider_plugins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_value TEXT NOT NULL,
                display_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL,
                cached_file_path TEXT NOT NULL DEFAULT '',
                last_loaded_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                config_text TEXT NOT NULL DEFAULT ''
            )
            '''
        )
        conn.execute(
            '''
            CREATE TABLE spider_plugin_playback_history (
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
                playlist_index INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (plugin_id, vod_id)
            )
            '''
        )
        conn.execute(
            "INSERT INTO spider_plugins (id, source_type, source_value, display_name, enabled, sort_order, cached_file_path, last_loaded_at, last_error, config_text) VALUES (1, 'local', '/plugins/demo.py', '红果短剧', 1, 0, '', 0, '', '')"
        )
        conn.execute(
            "INSERT INTO spider_plugin_playback_history (plugin_id, vod_id, vod_name, vod_pic, vod_remarks, episode, episode_url, position, opening, ending, speed, playlist_index, updated_at) VALUES (1, 'detail-1', '红果短剧', 'poster', '第2集', 1, '2.m3u8', 45000, 0, 0, 1.0, 0, 1713206400000)"
        )

    from atv_player.local_playback_history import LocalPlaybackHistoryRepository

    repo = LocalPlaybackHistoryRepository(db_path)
    records = repo.list_histories()

    assert len(records) == 1
    assert records[0].source_kind == "spider_plugin"
    assert records[0].source_name == "红果短剧"
    assert records[0].key == "detail-1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_storage.py::test_local_playback_history_repository_round_trip_emby_source_metadata tests/test_storage.py::test_local_playback_history_repository_lists_and_deletes_jellyfin_records tests/test_storage.py::test_local_playback_history_repository_migrates_spider_plugin_legacy_rows -q`
Expected: FAIL because `LocalPlaybackHistoryRepository` and `media_playback_history` do not exist.

- [ ] **Step 3: Write minimal implementation**

```python
class LocalPlaybackHistoryRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS media_playback_history (
                    source_kind TEXT NOT NULL,
                    source_key TEXT NOT NULL DEFAULT '',
                    source_name TEXT NOT NULL DEFAULT '',
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
                    playlist_index INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (source_kind, source_key, vod_id)
                )
                """
            )
            self._migrate_spider_plugin_history(conn)

    def _migrate_spider_plugin_history(self, conn: sqlite3.Connection) -> None:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "spider_plugin_playback_history" not in tables or "spider_plugins" not in tables:
            return
        rows = conn.execute(
            """
            SELECT history.vod_id, history.vod_name, history.vod_pic, history.vod_remarks,
                   history.episode, history.episode_url, history.position, history.opening,
                   history.ending, history.speed, history.playlist_index, history.updated_at,
                   plugin.display_name
            FROM spider_plugin_playback_history AS history
            JOIN spider_plugins AS plugin ON plugin.id = history.plugin_id
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO media_playback_history (
                    source_kind, source_key, source_name, vod_id, vod_name, vod_pic,
                    vod_remarks, episode, episode_url, position, opening, ending,
                    speed, playlist_index, updated_at
                )
                VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "spider_plugin",
                    str(row[12] or ""),
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    int(row[4]),
                    row[5],
                    int(row[6]),
                    int(row[7]),
                    int(row[8]),
                    float(row[9]),
                    int(row[10]),
                    int(row[11]),
                ),
            )

    def get_history(self, source_kind: str, vod_id: str, source_key: str = "") -> HistoryRecord | None:
        ...

    def save_history(
        self,
        source_kind: str,
        vod_id: str,
        payload: dict[str, object],
        *,
        source_key: str = "",
        source_name: str = "",
    ) -> None:
        ...

    def list_histories(self) -> list[HistoryRecord]:
        ...

    def delete_history(self, source_kind: str, vod_id: str, source_key: str = "") -> None:
        ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_storage.py::test_local_playback_history_repository_round_trip_emby_source_metadata tests/test_storage.py::test_local_playback_history_repository_lists_and_deletes_jellyfin_records tests/test_storage.py::test_local_playback_history_repository_migrates_spider_plugin_legacy_rows -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/local_playback_history.py tests/test_storage.py src/atv_player/models.py
git commit -m "feat: add local media playback history repository"
```

### Task 3: Switch Spider Plugin History Callbacks To The New Repository

**Files:**
- Modify: `src/atv_player/plugins/__init__.py`
- Modify: `tests/test_spider_plugin_manager.py`

- [ ] **Step 1: Write the failing test**

```python
def test_manager_load_enabled_plugins_wires_local_repository_playback_history_callbacks(tmp_path: Path) -> None:
    from atv_player.local_playback_history import LocalPlaybackHistoryRepository

    plugin_repository = SpiderPluginRepository(tmp_path / "app.db")
    local_history_repository = LocalPlaybackHistoryRepository(tmp_path / "app.db")
    plugin = plugin_repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")
    local_history_repository.save_history(
        "spider_plugin",
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
            "playlistIndex": 1,
            "createTime": 1713206400000,
        },
        source_name="红果短剧",
    )
    manager = SpiderPluginManager(plugin_repository, HistoryLoader(), local_history_repository)

    definitions = manager.load_enabled_plugins()
    request = definitions[0].controller.build_request("detail-1")

    assert request.playback_history_loader is not None
    loaded = request.playback_history_loader()
    assert loaded is not None
    assert loaded.position == 45000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_spider_plugin_manager.py::test_manager_load_enabled_plugins_wires_local_repository_playback_history_callbacks -q`
Expected: FAIL because `SpiderPluginManager` does not accept the new repository dependency.

- [ ] **Step 3: Write minimal implementation**

```python
class SpiderPluginManager:
    def __init__(
        self,
        repository: SpiderPluginRepository,
        loader: SpiderPluginLoader,
        playback_history_repository=None,
    ) -> None:
        self._repository = repository
        self._loader = loader
        self._playback_history_repository = playback_history_repository

    def load_enabled_plugins(self, drive_detail_loader=None) -> list[SpiderPluginDefinition]:
        ...
        controller = SpiderPluginController(
            loaded.spider,
            plugin_name=title,
            search_enabled=loaded.search_enabled,
            drive_detail_loader=drive_detail_loader,
            playback_history_loader=lambda vod_id: self._playback_history_repository.get_history(
                "spider_plugin",
                vod_id,
                source_name=title,  # dropped in real code, kept out of test snippet
            ),
            playback_history_saver=lambda vod_id, payload: self._playback_history_repository.save_history(
                "spider_plugin",
                vod_id,
                payload,
                source_name=title,
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_spider_plugin_manager.py::test_manager_load_enabled_plugins_wires_local_repository_playback_history_callbacks -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/plugins/__init__.py tests/test_spider_plugin_manager.py
git commit -m "feat: route spider history through local media repository"
```

### Task 4: Wire Emby Local Playback History

**Files:**
- Modify: `src/atv_player/controllers/emby_controller.py`
- Modify: `tests/test_emby_controller.py`

- [ ] **Step 1: Write the failing test**

```python
def test_build_request_disables_remote_history_and_exposes_local_emby_history_hooks() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1-3281",
                "vod_name": "Season 1",
                "vod_pic": "poster.jpg",
                "vod_play_url": "Episode 1$1-3458#Episode 2$1-3459",
            }
        ]
    }
    load_calls: list[str] = []
    save_calls: list[tuple[str, dict[str, object]]] = []
    controller = EmbyController(
        api,
        playback_history_loader=lambda vod_id: load_calls.append(vod_id) or None,
        playback_history_saver=lambda vod_id, payload: save_calls.append((vod_id, payload)),
    )

    request = controller.build_request("1-3281")

    assert request.use_local_history is False
    assert request.playback_history_loader is not None
    assert request.playback_history_saver is not None

    request.playback_history_loader()
    request.playback_history_saver({"position": 45000})

    assert load_calls == ["1-3281"]
    assert save_calls == [("1-3281", {"position": 45000})]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_emby_controller.py::test_build_request_disables_remote_history_and_exposes_local_emby_history_hooks -q`
Expected: FAIL because `EmbyController` does not accept local-history callbacks and still leaves `use_local_history=True`.

- [ ] **Step 3: Write minimal implementation**

```python
class EmbyController:
    def __init__(
        self,
        api_client,
        playback_history_loader: Callable[[str], HistoryRecord | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._api_client = api_client
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
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_emby_controller.py::test_build_request_disables_remote_history_and_exposes_local_emby_history_hooks -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/controllers/emby_controller.py tests/test_emby_controller.py
git commit -m "feat: add emby local playback history hooks"
```

### Task 5: Wire Jellyfin Local Playback History

**Files:**
- Modify: `src/atv_player/controllers/jellyfin_controller.py`
- Modify: `tests/test_jellyfin_controller.py`

- [ ] **Step 1: Write the failing test**

```python
def test_build_request_disables_remote_history_and_exposes_local_jellyfin_history_hooks() -> None:
    from atv_player.controllers.jellyfin_controller import JellyfinController

    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1-3281",
                "vod_name": "Season 1",
                "vod_pic": "poster.jpg",
                "vod_play_url": "Episode 1$1-3458#Episode 2$1-3459",
            }
        ]
    }
    load_calls: list[str] = []
    save_calls: list[tuple[str, dict[str, object]]] = []
    controller = JellyfinController(
        api,
        playback_history_loader=lambda vod_id: load_calls.append(vod_id) or None,
        playback_history_saver=lambda vod_id, payload: save_calls.append((vod_id, payload)),
    )

    request = controller.build_request("1-3281")

    assert request.use_local_history is False
    assert request.playback_history_loader is not None
    assert request.playback_history_saver is not None

    request.playback_history_loader()
    request.playback_history_saver({"position": 45000})

    assert load_calls == ["1-3281"]
    assert save_calls == [("1-3281", {"position": 45000})]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jellyfin_controller.py::test_build_request_disables_remote_history_and_exposes_local_jellyfin_history_hooks -q`
Expected: FAIL because `JellyfinController` does not accept local-history callbacks and still enables backend `/history`.

- [ ] **Step 3: Write minimal implementation**

```python
class JellyfinController:
    def __init__(
        self,
        api_client,
        playback_history_loader: Callable[[str], HistoryRecord | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._api_client = api_client
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        ...
        return OpenPlayerRequest(
            ...,
            use_local_history=False,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jellyfin_controller.py::test_build_request_disables_remote_history_and_exposes_local_jellyfin_history_hooks -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/controllers/jellyfin_controller.py tests/test_jellyfin_controller.py
git commit -m "feat: add jellyfin local playback history hooks"
```

### Task 6: Prefer Local Emby/Jellyfin History In `PlayerController`

**Files:**
- Modify: `tests/test_player_controller.py`
- Modify: `src/atv_player/controllers/player_controller.py` only if tests expose a gap

- [ ] **Step 1: Write the failing tests**

```python
def test_player_controller_prefers_emby_local_history_loader_over_backend_history() -> None:
    api = FakeApiClient()
    api.history = HistoryRecord(
        id=1,
        key="emby-1",
        vod_name="API Movie",
        vod_pic="pic",
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
    session = controller.create_session(
        VodItem(vod_id="emby-1", vod_name="Emby Movie"),
        [PlayItem(title="Episode 1", url="1.m3u8"), PlayItem(title="Episode 2", url="2.m3u8")],
        clicked_index=0,
        use_local_history=False,
        playback_history_loader=lambda: HistoryRecord(
            id=0,
            key="emby-1",
            vod_name="Emby Movie",
            vod_pic="pic",
            vod_remarks="Episode 2",
            episode=1,
            episode_url="2.m3u8",
            position=45000,
            opening=5000,
            ending=10000,
            speed=1.25,
            create_time=2,
            source_kind="emby",
            source_name="Emby",
        ),
    )

    assert api.history_calls == []
    assert session.start_index == 1
    assert session.start_position_seconds == 45


def test_player_controller_reports_progress_to_jellyfin_local_saver_without_backend_history() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    saved_payloads: list[dict[str, object]] = []
    session = controller.create_session(
        VodItem(vod_id="jf-1", vod_name="Jellyfin Movie", vod_pic="poster"),
        [PlayItem(title="Episode 1", url="https://media.example/1.m3u8")],
        clicked_index=0,
        use_local_history=False,
        playback_history_saver=lambda payload: saved_payloads.append(payload),
    )

    controller.report_progress(
        session,
        current_index=0,
        position_seconds=45,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
        paused=False,
    )

    assert len(saved_payloads) == 1
    assert saved_payloads[0]["key"] == "jf-1"
    assert api.saved_payloads == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_player_controller.py::test_player_controller_prefers_emby_local_history_loader_over_backend_history tests/test_player_controller.py::test_player_controller_reports_progress_to_jellyfin_local_saver_without_backend_history -q`
Expected: FAIL until Emby/Jellyfin controllers and app wiring start supplying the new local callbacks consistently, or PASS immediately if `PlayerController` already supports the required flow.

- [ ] **Step 3: Write minimal implementation**

```python
# Only if needed. Prefer no production change if tests already pass.
history = playback_history_loader() if playback_history_loader is not None else None
if history is None and (use_local_history or restore_history):
    history = self._api_client.get_history(vod.vod_id)
...
if session.playback_history_saver is not None:
    session.playback_history_saver(payload)
if not session.use_local_history:
    return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_player_controller.py::test_player_controller_prefers_emby_local_history_loader_over_backend_history tests/test_player_controller.py::test_player_controller_reports_progress_to_jellyfin_local_saver_without_backend_history -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/controllers/player_controller.py tests/test_player_controller.py
git commit -m "test: lock emby and jellyfin local history behavior"
```

### Task 7: Merge Emby/Jellyfin Local Records Into History Controller And Open Routing

**Files:**
- Modify: `src/atv_player/controllers/history_controller.py`
- Modify: `src/atv_player/ui/main_window.py`
- Modify: `tests/test_history_controller.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_history_controller_merges_remote_plugin_emby_and_jellyfin_histories() -> None:
    api = FakeApiClient()
    repository = FakeRepository(
        histories=[
            HistoryRecord(
                id=0,
                key="emby-1",
                vod_name="Emby Movie",
                vod_pic="poster",
                vod_remarks="Episode 2",
                episode=1,
                episode_url="2.m3u8",
                position=45000,
                opening=0,
                ending=0,
                speed=1.0,
                create_time=300,
                source_kind="emby",
                source_name="Emby",
            ),
            HistoryRecord(
                id=0,
                key="jf-1",
                vod_name="Jellyfin Movie",
                vod_pic="poster",
                vod_remarks="Episode 1",
                episode=0,
                episode_url="1.m3u8",
                position=25000,
                opening=0,
                ending=0,
                speed=1.0,
                create_time=200,
                source_kind="jellyfin",
                source_name="Jellyfin",
            ),
        ]
    )
    controller = HistoryController(api, repository)

    records, total = controller.load_page(page=1, size=20)

    assert total == 3
    assert [record.source_kind for record in records] == ["emby", "jellyfin", "remote"]


def test_main_window_opens_emby_and_jellyfin_history_detail_by_source(qtbot, monkeypatch) -> None:
    emby_controller = AsyncRequestController(_make_telegram_request)
    jellyfin_controller = AsyncRequestController(_make_telegram_request)
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=emby_controller,
        jellyfin_controller=jellyfin_controller,
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="emby-1",
            vod_name="Emby Movie",
            vod_pic="",
            vod_remarks="Episode 1",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="emby",
            source_name="Emby",
        )
    )
    _wait_for_request_call(qtbot, emby_controller, "emby-1")

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="jf-1",
            vod_name="Jellyfin Movie",
            vod_pic="",
            vod_remarks="Episode 1",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="jellyfin",
            source_name="Jellyfin",
        )
    )
    _wait_for_request_call(qtbot, jellyfin_controller, "jf-1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_history_controller.py::test_history_controller_merges_remote_plugin_emby_and_jellyfin_histories tests/test_app.py::test_main_window_opens_emby_and_jellyfin_history_detail_by_source -q`
Expected: FAIL because `HistoryController` and `MainWindow` do not yet recognize Emby/Jellyfin local sources.

- [ ] **Step 3: Write minimal implementation**

```python
def _source_label(self, record: HistoryRecord) -> str:
    if record.source_kind == "spider_plugin":
        return record.source_plugin_name or record.source_name or "插件"
    if record.source_kind == "emby":
        return "Emby"
    if record.source_kind == "jellyfin":
        return "Jellyfin"
    return "远程"


def open_history_detail(self, record: HistoryRecord) -> None:
    if record.source_kind == "emby":
        self._start_open_request(lambda: self.emby_controller.build_request(record.key))
        return
    if record.source_kind == "jellyfin":
        self._start_open_request(lambda: self.jellyfin_controller.build_request(record.key))
        return
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_history_controller.py::test_history_controller_merges_remote_plugin_emby_and_jellyfin_histories tests/test_app.py::test_main_window_opens_emby_and_jellyfin_history_detail_by_source -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/controllers/history_controller.py src/atv_player/ui/main_window.py tests/test_history_controller.py tests/test_app.py
git commit -m "feat: merge emby and jellyfin local history"
```

### Task 8: Wire Repository Through `AppCoordinator`

**Files:**
- Modify: `src/atv_player/app.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write the failing test**

```python
def test_show_main_wires_local_playback_history_repository_into_media_controllers(monkeypatch, tmp_path) -> None:
    class RecordingMainWindow:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def show(self) -> None:
            return None

    repo = SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    config.token = "token"
    config.vod_token = "vod-token"
    repo.save_config(config)

    coordinator = AppCoordinator(repo)
    monkeypatch.setattr(app_module, "MainWindow", RecordingMainWindow)
    monkeypatch.setattr(app_module.AppCoordinator, "_load_capabilities", lambda self, client: {"emby": True, "jellyfin": True})

    window = coordinator._show_main()

    assert hasattr(coordinator, "_local_playback_history_repository")
    assert window.kwargs["emby_controller"]._playback_history_loader is not None
    assert window.kwargs["jellyfin_controller"]._playback_history_loader is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py::test_show_main_wires_local_playback_history_repository_into_media_controllers -q`
Expected: FAIL because `AppCoordinator` does not create or inject `LocalPlaybackHistoryRepository`.

- [ ] **Step 3: Write minimal implementation**

```python
from atv_player.local_playback_history import LocalPlaybackHistoryRepository

class AppCoordinator(QObject):
    def __init__(self, repo: SettingsRepository) -> None:
        ...
        if hasattr(repo, "database_path"):
            ...
            self._local_playback_history_repository = LocalPlaybackHistoryRepository(repo.database_path)
            self._plugin_manager = SpiderPluginManager(
                self._plugin_repository,
                self._plugin_loader,
                self._local_playback_history_repository,
            )
        else:
            ...
            self._local_playback_history_repository = None

    def _show_main(self):
        ...
        emby_controller = EmbyController(
            self._api_client,
            playback_history_loader=lambda vod_id: self._local_playback_history_repository.get_history("emby", vod_id),
            playback_history_saver=lambda vod_id, payload: self._local_playback_history_repository.save_history(
                "emby",
                vod_id,
                payload,
                source_name="Emby",
            ),
        )
        jellyfin_controller = JellyfinController(
            self._api_client,
            playback_history_loader=lambda vod_id: self._local_playback_history_repository.get_history("jellyfin", vod_id),
            playback_history_saver=lambda vod_id, payload: self._local_playback_history_repository.save_history(
                "jellyfin",
                vod_id,
                payload,
                source_name="Jellyfin",
            ),
        )
        history_controller = HistoryController(self._api_client, self._local_playback_history_repository)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py::test_show_main_wires_local_playback_history_repository_into_media_controllers -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/app.py tests/test_app.py
git commit -m "feat: wire local playback history repository into app"
```

### Task 9: Run Focused Verification

**Files:**
- Test: `tests/test_storage.py`
- Test: `tests/test_spider_plugin_manager.py`
- Test: `tests/test_emby_controller.py`
- Test: `tests/test_jellyfin_controller.py`
- Test: `tests/test_player_controller.py`
- Test: `tests/test_history_controller.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Run repository and controller verification**

Run: `uv run pytest tests/test_storage.py tests/test_spider_plugin_manager.py tests/test_emby_controller.py tests/test_jellyfin_controller.py tests/test_player_controller.py::test_player_controller_prefers_emby_local_history_loader_over_backend_history tests/test_player_controller.py::test_player_controller_reports_progress_to_jellyfin_local_saver_without_backend_history -q`
Expected: PASS

- [ ] **Step 2: Run merged history and routing verification**

Run: `uv run pytest tests/test_history_controller.py tests/test_app.py::test_main_window_opens_remote_history_detail_asynchronously tests/test_app.py::test_main_window_opens_plugin_history_detail_asynchronously tests/test_app.py::test_main_window_shows_error_when_plugin_history_source_is_missing tests/test_app.py::test_main_window_opens_emby_and_jellyfin_history_detail_by_source tests/test_app.py::test_show_main_wires_local_playback_history_repository_into_media_controllers -q`
Expected: PASS

- [ ] **Step 3: Run restore-path verification**

Run: `uv run pytest tests/test_app.py::test_main_window_restore_last_player_routes_emby_detail_to_emby_controller tests/test_app.py::test_main_window_restore_last_player_routes_plugin_detail_to_plugin_controller_with_playback_history_loader -q`
Expected: PASS

- [ ] **Step 4: Run the full targeted regression set**

Run: `uv run pytest tests/test_storage.py tests/test_spider_plugin_manager.py tests/test_emby_controller.py tests/test_jellyfin_controller.py tests/test_player_controller.py tests/test_history_controller.py tests/test_app.py -q`
Expected: PASS, or if `pytest-qt` batch segfaults recur, rerun the UI-heavy subsets sequentially and record the passing outputs explicitly.

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/local_playback_history.py src/atv_player/models.py src/atv_player/plugins/__init__.py src/atv_player/controllers/emby_controller.py src/atv_player/controllers/jellyfin_controller.py src/atv_player/controllers/player_controller.py src/atv_player/controllers/history_controller.py src/atv_player/ui/main_window.py src/atv_player/app.py tests/test_storage.py tests/test_spider_plugin_manager.py tests/test_emby_controller.py tests/test_jellyfin_controller.py tests/test_player_controller.py tests/test_history_controller.py tests/test_app.py
git commit -m "feat: store media playback history locally"
```
