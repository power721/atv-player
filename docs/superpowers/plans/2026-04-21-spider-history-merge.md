# Spider History Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep spider plugin playback progress local-only in SQLite and merge spider-plugin local history with remote history in the playback-history tab.

**Architecture:** Extend `HistoryRecord` with source metadata, expose plugin-local history listing/deletion from `SpiderPluginRepository`, and aggregate remote plus plugin-local records inside `HistoryController`. Update `HistoryPage` and `MainWindow` to operate on full history records so open/delete/clear route to the correct source without crossing boundaries.

**Tech Stack:** Python, PySide6, SQLite, pytest, pytest-qt

---

## File Structure

- Modify: `src/atv_player/models.py`
  Add history source metadata to `HistoryRecord`.
- Modify: `src/atv_player/plugins/repository.py`
  Add plugin-local history listing and single-record deletion helpers for merged history.
- Modify: `src/atv_player/controllers/history_controller.py`
  Aggregate remote and plugin-local histories, sort, paginate, and dispatch mutations by source.
- Modify: `src/atv_player/ui/history_page.py`
  Show source column and pass full `HistoryRecord` objects for open/delete/clear actions.
- Modify: `src/atv_player/ui/main_window.py`
  Route history open requests by source, including spider plugin records.
- Modify: `tests/test_storage.py`
  Cover repository listing and deletion helpers for plugin-local history.
- Modify: `tests/test_history_controller.py`
  Cover merged loading and source-aware deletion/clear logic.
- Modify: `tests/test_browse_page_ui.py`
  Cover history-page source rendering and record-based actions.
- Modify: `tests/test_app.py`
  Cover main-window opening of remote and plugin history rows.
- Modify: `tests/test_player_controller.py`
  Lock in plugin local saver behavior without backend history writes.

### Task 1: Extend `HistoryRecord` With Source Metadata

**Files:**
- Modify: `src/atv_player/models.py`
- Test: `tests/test_history_controller.py`

- [ ] **Step 1: Write the failing test**

```python
def test_history_controller_maps_backend_payload() -> None:
    controller = HistoryController(FakeApiClient(), FakeRepository())

    records, total = controller.load_page(page=1, size=20)

    assert total == 1
    assert records[0].id == 9
    assert records[0].vod_name == "Movie"
    assert records[0].episode == 1
    assert records[0].source_kind == "remote"
    assert records[0].source_plugin_id == 0
    assert records[0].source_plugin_name == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_history_controller.py::test_history_controller_maps_backend_payload -q`
Expected: FAIL because `HistoryRecord` has no `source_kind`, `source_plugin_id`, or `source_plugin_name`.

- [ ] **Step 3: Write minimal implementation**

```python
@dataclass(slots=True)
class HistoryRecord:
    id: int
    key: str
    vod_name: str
    vod_pic: str
    vod_remarks: str
    episode: int
    episode_url: str
    position: int
    opening: int
    ending: int
    speed: float
    create_time: int
    playlist_index: int = 0
    source_kind: str = "remote"
    source_plugin_id: int = 0
    source_plugin_name: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_history_controller.py::test_history_controller_maps_backend_payload -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/models.py tests/test_history_controller.py
git commit -m "feat: add history source metadata"
```

### Task 2: Add Plugin-Local History Listing And Delete Helpers

**Files:**
- Modify: `src/atv_player/plugins/repository.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_spider_plugin_repository_lists_playback_histories_with_plugin_metadata(tmp_path: Path) -> None:
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
            "playlistIndex": 1,
            "createTime": 1713206400000,
        },
    )

    records = repo.list_playback_histories()

    assert len(records) == 1
    assert records[0].key == "detail-1"
    assert records[0].source_kind == "spider_plugin"
    assert records[0].source_plugin_id == plugin.id
    assert records[0].source_plugin_name == "红果短剧"


def test_spider_plugin_repository_deletes_single_playback_history(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SpiderPluginRepository(db_path)
    plugin = repo.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")
    repo.save_playback_history(
        plugin.id,
        "detail-1",
        {
            "vodName": "红果短剧",
            "vodPic": "poster-1",
            "vodRemarks": "第1集",
            "episode": 0,
            "episodeUrl": "https://media.example/1.m3u8",
            "position": 15000,
            "opening": 0,
            "ending": 0,
            "speed": 1.0,
            "playlistIndex": 0,
            "createTime": 1713206400000,
        },
    )

    repo.delete_playback_history(plugin.id, "detail-1")

    assert repo.get_playback_history(plugin.id, "detail-1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_storage.py::test_spider_plugin_repository_lists_playback_histories_with_plugin_metadata tests/test_storage.py::test_spider_plugin_repository_deletes_single_playback_history -q`
Expected: FAIL because `SpiderPluginRepository` has no `list_playback_histories()` or `delete_playback_history()`.

- [ ] **Step 3: Write minimal implementation**

```python
def list_playback_histories(self) -> list[HistoryRecord]:
    with self._connect() as conn:
        rows = conn.execute(
            """
            SELECT history.vod_id, history.vod_name, history.vod_pic, history.vod_remarks,
                   history.episode, history.episode_url, history.position, history.opening,
                   history.ending, history.speed, history.playlist_index, history.updated_at,
                   plugin.id, plugin.display_name
            FROM spider_plugin_playback_history AS history
            JOIN spider_plugins AS plugin ON plugin.id = history.plugin_id
            """
        ).fetchall()
    return [
        HistoryRecord(
            id=0,
            key=row[0],
            vod_name=row[1],
            vod_pic=row[2],
            vod_remarks=row[3],
            episode=int(row[4]),
            episode_url=row[5],
            position=int(row[6]),
            opening=int(row[7]),
            ending=int(row[8]),
            speed=float(row[9]),
            create_time=int(row[11]),
            playlist_index=int(row[10]),
            source_kind="spider_plugin",
            source_plugin_id=int(row[12]),
            source_plugin_name=str(row[13] or ""),
        )
        for row in rows
    ]


def delete_playback_history(self, plugin_id: int, vod_id: str) -> None:
    with self._connect() as conn:
        conn.execute(
            "DELETE FROM spider_plugin_playback_history WHERE plugin_id = ? AND vod_id = ?",
            (plugin_id, vod_id),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_storage.py::test_spider_plugin_repository_lists_playback_histories_with_plugin_metadata tests/test_storage.py::test_spider_plugin_repository_deletes_single_playback_history -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/plugins/repository.py tests/test_storage.py
git commit -m "feat: expose spider plugin playback histories"
```

### Task 3: Aggregate Remote And Plugin Histories In `HistoryController`

**Files:**
- Modify: `src/atv_player/controllers/history_controller.py`
- Modify: `tests/test_history_controller.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_history_controller_merges_remote_and_plugin_histories_in_descending_time_order() -> None:
    api = FakeApiClient()
    repository = FakeRepository(
        histories=[
            HistoryRecord(
                id=0,
                key="plugin-1",
                vod_name="Plugin Movie",
                vod_pic="plugin-pic",
                vod_remarks="第2集",
                episode=1,
                episode_url="plugin-2.m3u8",
                position=45000,
                opening=0,
                ending=0,
                speed=1.0,
                create_time=200,
                source_kind="spider_plugin",
                source_plugin_id=7,
                source_plugin_name="红果短剧",
            )
        ]
    )
    controller = HistoryController(api, repository)

    records, total = controller.load_page(page=1, size=20)

    assert total == 2
    assert [record.key for record in records] == ["plugin-1", "movie-1"]
    assert [record.source_kind for record in records] == ["spider_plugin", "remote"]


def test_history_controller_deletes_one_or_many_by_source() -> None:
    api = FakeApiClient()
    repository = FakeRepository()
    controller = HistoryController(api, repository)
    remote = HistoryRecord(
        id=9,
        key="movie-1",
        vod_name="Movie",
        vod_pic="pic",
        vod_remarks="Episode 2",
        episode=1,
        episode_url="2.m3u8",
        position=90000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=123456,
        source_kind="remote",
    )
    plugin = HistoryRecord(
        id=0,
        key="detail-1",
        vod_name="Plugin Movie",
        vod_pic="poster",
        vod_remarks="第1集",
        episode=0,
        episode_url="1.m3u8",
        position=15000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=123457,
        source_kind="spider_plugin",
        source_plugin_id=3,
        source_plugin_name="红果短剧",
    )

    controller.delete_one(remote)
    controller.delete_many([remote, plugin])

    assert api.deleted_one == [9]
    assert api.deleted_many == [[9]]
    assert repository.deleted == [(3, "detail-1")]


def test_history_controller_clear_page_deletes_current_records_by_source() -> None:
    api = FakeApiClient()
    repository = FakeRepository()
    controller = HistoryController(api, repository)
    remote = HistoryRecord(
        id=11,
        key="movie-2",
        vod_name="Movie 2",
        vod_pic="",
        vod_remarks="Episode 1",
        episode=0,
        episode_url="1.m3u8",
        position=3000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=999,
        source_kind="remote",
    )
    plugin = HistoryRecord(
        id=0,
        key="detail-2",
        vod_name="Plugin Movie",
        vod_pic="",
        vod_remarks="第3集",
        episode=2,
        episode_url="3.m3u8",
        position=6000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1000,
        source_kind="spider_plugin",
        source_plugin_id=4,
        source_plugin_name="插件二",
    )

    controller.clear_page([remote, plugin])

    assert api.deleted_many == [[11]]
    assert repository.deleted == [(4, "detail-2")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_history_controller.py::test_history_controller_merges_remote_and_plugin_histories_in_descending_time_order tests/test_history_controller.py::test_history_controller_deletes_one_or_many_by_source tests/test_history_controller.py::test_history_controller_clear_page_deletes_current_records_by_source -q`
Expected: FAIL because `HistoryController` still only accepts the API client and only deletes backend ids.

- [ ] **Step 3: Write minimal implementation**

```python
class HistoryController:
    def __init__(self, api_client, plugin_repository=None) -> None:
        self._api_client = api_client
        self._plugin_repository = plugin_repository

    def _load_remote_records(self) -> list[HistoryRecord]:
        payload = self._api_client.list_history(page=1, size=10000)
        return [
            HistoryRecord(
                id=item["id"],
                key=item["key"],
                vod_name=item["vodName"],
                vod_pic=item.get("vodPic", ""),
                vod_remarks=item.get("vodRemarks", ""),
                episode=item.get("episode", 0),
                episode_url=item.get("episodeUrl", ""),
                position=item.get("position", 0),
                opening=item.get("opening", 0),
                ending=item.get("ending", 0),
                speed=item.get("speed", 1.0),
                create_time=item["createTime"],
                source_kind="remote",
            )
            for item in payload["content"]
        ]

    def load_page(self, page: int, size: int) -> tuple[list[HistoryRecord], int]:
        records = self._load_remote_records()
        if self._plugin_repository is not None:
            records.extend(self._plugin_repository.list_playback_histories())
        records.sort(key=lambda item: item.create_time, reverse=True)
        total = len(records)
        start = max(page - 1, 0) * size
        end = start + size
        return records[start:end], total

    def delete_one(self, record: HistoryRecord) -> None:
        self.delete_many([record])

    def delete_many(self, records: list[HistoryRecord]) -> None:
        remote_ids = [record.id for record in records if record.source_kind == "remote"]
        if len(remote_ids) == 1:
            self._api_client.delete_history(remote_ids[0])
        elif remote_ids:
            self._api_client.delete_histories(remote_ids)
        if self._plugin_repository is None:
            return
        for record in records:
            if record.source_kind == "spider_plugin":
                self._plugin_repository.delete_playback_history(record.source_plugin_id, record.key)

    def clear_page(self, records: list[HistoryRecord]) -> None:
        self.delete_many(records)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_history_controller.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/controllers/history_controller.py tests/test_history_controller.py
git commit -m "feat: merge remote and spider plugin histories"
```

### Task 4: Update `HistoryPage` To Use Full `HistoryRecord` Objects

**Files:**
- Modify: `src/atv_player/ui/history_page.py`
- Modify: `tests/test_browse_page_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_history_page_formats_episode_progress_time_and_source(qtbot) -> None:
    class Controller:
        def load_page(self, page: int, size: int):
            return [
                HistoryRecord(
                    id=1,
                    key="movie-1",
                    vod_name="Movie",
                    vod_pic="pic",
                    vod_remarks="Episode 2",
                    episode=1,
                    episode_url="2.m3u8",
                    position=90000,
                    opening=0,
                    ending=0,
                    speed=1.0,
                    create_time=1713168000000,
                    source_kind="spider_plugin",
                    source_plugin_id=7,
                    source_plugin_name="红果短剧",
                )
            ], 1

    page = HistoryPage(Controller())
    qtbot.addWidget(page)

    page.load_history()
    qtbot.waitUntil(lambda: page.table.rowCount() == 1, timeout=1000)

    assert page.table.columnCount() == 6
    assert page.table.horizontalHeaderItem(5).text() == "来源"
    assert page.table.item(0, 5).text() == "红果短剧"


def test_history_page_delete_selected_passes_records_to_controller(qtbot) -> None:
    class Controller:
        def __init__(self) -> None:
            self.deleted_records: list[list[HistoryRecord]] = []

        def load_page(self, page: int, size: int):
            return [], 0

        def delete_many(self, records: list[HistoryRecord]) -> None:
            self.deleted_records.append(records)

    controller = Controller()
    page = HistoryPage(controller)
    qtbot.addWidget(page)
    page.records = [
        HistoryRecord(
            id=0,
            key="detail-1",
            vod_name="Movie",
            vod_pic="",
            vod_remarks="Ep",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="spider_plugin",
            source_plugin_id=3,
            source_plugin_name="红果短剧",
        )
    ]
    page.total_items = 1
    page.table.setColumnCount(6)
    page.table.setRowCount(1)
    page.table.setItem(0, 0, QTableWidgetItem("Movie"))
    page.table.selectRow(0)

    page.delete_selected()

    qtbot.waitUntil(lambda: len(controller.deleted_records) == 1, timeout=1000)
    assert controller.deleted_records[0][0].key == "detail-1"


def test_history_page_clear_all_passes_current_page_records_to_controller(qtbot) -> None:
    class Controller:
        def __init__(self) -> None:
            self.cleared_records: list[list[HistoryRecord]] = []

        def load_page(self, page: int, size: int):
            return [], 0

        def clear_page(self, records: list[HistoryRecord]) -> None:
            self.cleared_records.append(records)

    controller = Controller()
    page = HistoryPage(controller)
    qtbot.addWidget(page)
    page.records = [
        HistoryRecord(
            id=9,
            key="movie-1",
            vod_name="Movie",
            vod_pic="",
            vod_remarks="Ep",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="remote",
        )
    ]

    page.clear_all()

    qtbot.waitUntil(lambda: len(controller.cleared_records) == 1, timeout=1000)
    assert controller.cleared_records[0][0].key == "movie-1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_browse_page_ui.py::test_history_page_formats_episode_progress_time_and_source tests/test_browse_page_ui.py::test_history_page_delete_selected_passes_records_to_controller tests/test_browse_page_ui.py::test_history_page_clear_all_passes_current_page_records_to_controller -q`
Expected: FAIL because `HistoryPage` still has five columns, emits only `key`, and passes ids to the controller.

- [ ] **Step 3: Write minimal implementation**

```python
class HistoryPage(QWidget):
    open_detail_requested = Signal(object)

    def __init__(self, controller) -> None:
        ...
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["标题", "集数", "当前播放", "进度", "时间", "来源"])
        ...

    def delete_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows:
            return
        records = [self.records[row] for row in rows]
        next_page = self.current_page - 1 if len(records) == len(self.records) and self.current_page > 1 else self.current_page

        def run() -> None:
            self.controller.delete_many(records)
            ...

    def clear_all(self) -> None:
        records = list(self.records)
        ...
        def run() -> None:
            self.controller.clear_page(records)
            ...

    def _open_selected(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self.records)):
            return
        self.open_detail_requested.emit(self.records[row])

    def _source_label(self, record: HistoryRecord) -> str:
        if record.source_kind == "spider_plugin":
            return record.source_plugin_name or "插件"
        return "远程"

    def _handle_load_succeeded(...):
        ...
        self.table.setItem(row, 5, QTableWidgetItem(self._source_label(record)))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_browse_page_ui.py::test_history_page_formats_episode_progress_time_and_source tests/test_browse_page_ui.py::test_history_page_delete_selected_passes_records_to_controller tests/test_browse_page_ui.py::test_history_page_clear_all_passes_current_page_records_to_controller -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/ui/history_page.py tests/test_browse_page_ui.py
git commit -m "feat: show merged history sources in history page"
```

### Task 5: Route History Opens Through Browse Or Plugin Controllers

**Files:**
- Modify: `src/atv_player/ui/main_window.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_main_window_opens_remote_history_detail_asynchronously(qtbot, monkeypatch) -> None:
    browse_controller = AsyncHistoryBrowseController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=browse_controller,
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[OpenPlayerRequest] = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "show_error", lambda message: None)

    window.open_history_detail(
        HistoryRecord(
            id=9,
            key="history-vod-1",
            vod_name="History Movie",
            vod_pic="",
            vod_remarks="Ep",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="remote",
        )
    )
    _wait_for_history_detail_call(qtbot, browse_controller, "history-vod-1")


def test_main_window_opens_plugin_history_detail_asynchronously(qtbot, monkeypatch) -> None:
    controller = AsyncPluginController(_make_telegram_request)
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[{"id": 7, "title": "红果短剧", "controller": controller, "search_enabled": False}],
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[OpenPlayerRequest] = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "show_error", lambda message: None)

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="detail-1",
            vod_name="Plugin Movie",
            vod_pic="",
            vod_remarks="第1集",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="spider_plugin",
            source_plugin_id=7,
            source_plugin_name="红果短剧",
        )
    )
    _wait_for_request_call(qtbot, controller, "detail-1")


def test_main_window_shows_error_when_plugin_history_source_is_missing(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[],
    )
    qtbot.addWidget(window)
    window.show()

    errors: list[str] = []
    monkeypatch.setattr(window, "show_error", lambda message: errors.append(message))

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="detail-1",
            vod_name="Plugin Movie",
            vod_pic="",
            vod_remarks="第1集",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="spider_plugin",
            source_plugin_id=999,
            source_plugin_name="失效插件",
        )
    )

    qtbot.waitUntil(lambda: errors == ["没有可播放的项目: 失效插件"], timeout=1000)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_app.py::test_main_window_opens_remote_history_detail_asynchronously tests/test_app.py::test_main_window_opens_plugin_history_detail_asynchronously tests/test_app.py::test_main_window_shows_error_when_plugin_history_source_is_missing -q`
Expected: FAIL because `MainWindow.open_history_detail()` still accepts a string key only.

- [ ] **Step 3: Write minimal implementation**

```python
def _find_plugin_controller(self, plugin_id: int):
    for definition in self._plugin_definitions:
        if _plugin_value(definition, "id") == plugin_id:
            return _plugin_value(definition, "controller")
    return None


def open_history_detail(self, record: HistoryRecord) -> None:
    if record.source_kind == "spider_plugin":
        controller = self._find_plugin_controller(record.source_plugin_id)
        if controller is None:
            self.show_error(f"没有可播放的项目: {record.source_plugin_name or record.key}")
            return
        self._start_open_request(lambda: controller.build_request(record.key))
        return
    self._start_open_request(lambda: self.browse_controller.build_request_from_detail(record.key))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_app.py::test_main_window_opens_remote_history_detail_asynchronously tests/test_app.py::test_main_window_opens_plugin_history_detail_asynchronously tests/test_app.py::test_main_window_shows_error_when_plugin_history_source_is_missing -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/ui/main_window.py tests/test_app.py
git commit -m "feat: route merged history opens by source"
```

### Task 6: Lock Plugin Playback Progress To Local-Only Save Path

**Files:**
- Modify: `tests/test_player_controller.py`
- Modify: `src/atv_player/controllers/player_controller.py` if needed

- [ ] **Step 1: Write the failing test**

```python
def test_player_controller_reports_progress_to_plugin_local_saver_without_backend_history() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="plugin-1", vod_name="Plugin Movie", vod_pic="poster")
    playlist = [PlayItem(title="第1集", url="https://media.example/1.m3u8")]
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
        position_seconds=45,
        speed=1.25,
        opening_seconds=5,
        ending_seconds=10,
        paused=False,
    )

    assert len(saved_payloads) == 1
    assert saved_payloads[0]["key"] == "plugin-1"
    assert api.saved_payloads == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_player_controller.py::test_player_controller_reports_progress_to_plugin_local_saver_without_backend_history -q`
Expected: FAIL if plugin saver ordering or `use_local_history` guard regresses.

- [ ] **Step 3: Write minimal implementation**

```python
if session.playback_history_saver is not None:
    session.playback_history_saver(payload)
if not session.use_local_history:
    return
self._api_client.save_history(payload)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_player_controller.py::test_player_controller_reports_progress_to_plugin_local_saver_without_backend_history -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/controllers/player_controller.py tests/test_player_controller.py
git commit -m "test: lock plugin playback history to local saver"
```

### Task 7: Run Focused Verification

**Files:**
- Test: `tests/test_storage.py`
- Test: `tests/test_history_controller.py`
- Test: `tests/test_browse_page_ui.py`
- Test: `tests/test_app.py`
- Test: `tests/test_player_controller.py`

- [ ] **Step 1: Run repository and controller tests**

Run: `uv run pytest tests/test_storage.py::test_spider_plugin_repository_lists_playback_histories_with_plugin_metadata tests/test_storage.py::test_spider_plugin_repository_deletes_single_playback_history tests/test_history_controller.py -q`
Expected: PASS

- [ ] **Step 2: Run history page tests**

Run: `uv run pytest tests/test_browse_page_ui.py::test_history_page_formats_episode_progress_time_and_source tests/test_browse_page_ui.py::test_history_page_delete_selected_passes_records_to_controller tests/test_browse_page_ui.py::test_history_page_clear_all_passes_current_page_records_to_controller tests/test_browse_page_ui.py::test_history_page_delete_reloads_previous_page_when_last_page_becomes_empty tests/test_browse_page_ui.py::test_history_page_refresh_reuses_current_page_state -q`
Expected: PASS

- [ ] **Step 3: Run main-window and player tests**

Run: `uv run pytest tests/test_app.py::test_main_window_opens_remote_history_detail_asynchronously tests/test_app.py::test_main_window_opens_plugin_history_detail_asynchronously tests/test_app.py::test_main_window_shows_error_when_plugin_history_source_is_missing tests/test_player_controller.py::test_player_controller_reports_progress_to_plugin_local_saver_without_backend_history -q`
Expected: PASS

- [ ] **Step 4: Run the full targeted regression set**

Run: `uv run pytest tests/test_storage.py tests/test_history_controller.py tests/test_browse_page_ui.py tests/test_app.py tests/test_player_controller.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_storage.py tests/test_history_controller.py tests/test_browse_page_ui.py tests/test_app.py tests/test_player_controller.py src/atv_player/models.py src/atv_player/plugins/repository.py src/atv_player/controllers/history_controller.py src/atv_player/ui/history_page.py src/atv_player/ui/main_window.py src/atv_player/controllers/player_controller.py
git commit -m "feat: merge spider plugin history into playback tab"
```
