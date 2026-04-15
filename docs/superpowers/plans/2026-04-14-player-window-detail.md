# Player Window Detail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the player window sidebar show title-level metadata as the main detail view while keeping playback status and failures in a separate log section.

**Architecture:** Keep the existing player sidebar and fullscreen behavior, but replace the single detail text area with a small composite container that owns two read-only text views: one for title metadata and one for playback logs. Preserve metadata on `VodItem`, map it in the browse/detail controller, and render it from `PlayerSession.vod` so playback UI stays decoupled from backend payload shape.

**Tech Stack:** Python 3.13, PySide6 widgets, pytest, pytest-qt

---

## File Structure

- Modify: `src/atv_player/models.py`
  Add the title-level metadata fields the player needs: `vod_year`, `vod_area`, `vod_lang`, `vod_director`, `vod_actor`.
- Modify: `src/atv_player/controllers/browse_controller.py`
  Map the new metadata fields from backend payloads and preserve any already-known metadata when building folder-item playback requests.
- Modify: `src/atv_player/ui/player_window.py`
  Replace the single detail editor with a details container that owns a metadata view and a log view, add metadata formatting helpers, and route runtime messages to the log view.
- Modify: `tests/test_browse_controller.py`
  Cover the detail payload mapping and folder-item metadata preservation.
- Modify: `tests/test_player_window_ui.py`
  Cover the new metadata/log layout, title metadata rendering, missing-field tolerance, and log routing behavior.

### Task 1: Map Player Metadata Into `VodItem`

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/controllers/browse_controller.py`
- Test: `tests/test_browse_controller.py`

- [ ] **Step 1: Write the failing controller tests**

Add these tests to `tests/test_browse_controller.py`:

```python
def test_build_request_from_detail_maps_title_metadata_fields() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "detail-1",
                "vod_name": "九寨沟",
                "type_name": "纪录片",
                "vod_year": "2006",
                "vod_area": "中国大陆",
                "vod_lang": "无对白",
                "vod_remarks": "6.2",
                "vod_director": "Masa Nishimura",
                "vod_actor": "未知",
                "vod_content": "九寨沟风景名胜区位于四川省阿坝藏族羌族自治州南坪县境内。",
                "dbid": 19971621,
                "items": [
                    {"title": "正片", "url": "http://m/1.m3u8"},
                ],
            }
        ]
    }
    controller = BrowseController(api)

    request = controller.build_request_from_detail("detail-1")

    assert request.vod.vod_name == "九寨沟"
    assert request.vod.type_name == "纪录片"
    assert request.vod.vod_year == "2006"
    assert request.vod.vod_area == "中国大陆"
    assert request.vod.vod_lang == "无对白"
    assert request.vod.vod_remarks == "6.2"
    assert request.vod.vod_director == "Masa Nishimura"
    assert request.vod.vod_actor == "未知"
    assert request.vod.vod_content == "九寨沟风景名胜区位于四川省阿坝藏族羌族自治州南坪县境内。"
    assert request.vod.dbid == 19971621


def test_build_request_from_folder_item_preserves_available_metadata() -> None:
    controller = BrowseController(FakeApiClient())
    clicked_item = VodItem(
        vod_id="v1",
        vod_name="九寨沟",
        vod_pic="poster.jpg",
        path="/纪录片/九寨沟.mp4",
        type=2,
        type_name="纪录片",
        vod_year="2006",
        vod_area="中国大陆",
        vod_lang="无对白",
        vod_remarks="6.2",
        vod_director="Masa Nishimura",
        vod_actor="未知",
        vod_content="九寨沟风景名胜区位于四川省阿坝藏族羌族自治州南坪县境内。",
        dbid=19971621,
        vod_play_url="http://m/1.m3u8",
    )

    request = controller.build_request_from_folder_item(clicked_item, [clicked_item])

    assert request.vod.type_name == "纪录片"
    assert request.vod.vod_year == "2006"
    assert request.vod.vod_area == "中国大陆"
    assert request.vod.vod_lang == "无对白"
    assert request.vod.vod_remarks == "6.2"
    assert request.vod.vod_director == "Masa Nishimura"
    assert request.vod.vod_actor == "未知"
    assert request.vod.vod_content.startswith("九寨沟风景名胜区位于")
    assert request.vod.dbid == 19971621
```

- [ ] **Step 2: Run the controller tests to verify they fail**

Run:

```bash
uv run pytest tests/test_browse_controller.py::test_build_request_from_detail_maps_title_metadata_fields tests/test_browse_controller.py::test_build_request_from_folder_item_preserves_available_metadata -v
```

Expected: FAIL because `VodItem` does not yet define the new metadata fields and `build_request_from_folder_item()` does not preserve them.

- [ ] **Step 3: Write the minimal model and controller implementation**

Update `src/atv_player/models.py` so `VodItem` carries the metadata fields:

```python
@dataclass(slots=True)
class VodItem:
    vod_id: str
    vod_name: str
    path: str = ""
    vod_pic: str = ""
    vod_tag: str = ""
    vod_time: str = ""
    vod_remarks: str = ""
    vod_play_from: str = ""
    vod_play_url: str = ""
    type_name: str = ""
    vod_content: str = ""
    vod_year: str = ""
    vod_area: str = ""
    vod_lang: str = ""
    vod_director: str = ""
    vod_actor: str = ""
    dbid: int = 0
    type: int = 0
    items: list[PlayItem] = field(default_factory=list)
```

Update `_map_vod_item()` in `src/atv_player/controllers/browse_controller.py`:

```python
def _map_vod_item(payload: dict) -> VodItem:
    items = [
        _map_play_item(item, index)
        for index, item in enumerate(payload.get("items") or [])
    ]
    return VodItem(
        vod_id=str(payload.get("vod_id") or ""),
        vod_name=str(payload.get("vod_name") or ""),
        path=str(payload.get("path") or ""),
        vod_pic=str(payload.get("vod_pic") or ""),
        vod_tag=str(payload.get("vod_tag") or ""),
        vod_time=str(payload.get("vod_time") or ""),
        vod_remarks=str(payload.get("vod_remarks") or ""),
        vod_play_from=str(payload.get("vod_play_from") or ""),
        vod_play_url=str(payload.get("vod_play_url") or ""),
        type_name=str(payload.get("type_name") or ""),
        vod_content=str(payload.get("vod_content") or ""),
        vod_year=str(payload.get("vod_year") or ""),
        vod_area=str(payload.get("vod_area") or ""),
        vod_lang=str(payload.get("vod_lang") or ""),
        vod_director=str(payload.get("vod_director") or ""),
        vod_actor=str(payload.get("vod_actor") or ""),
        dbid=int(payload.get("dbid") or 0),
        type=int(payload.get("type") or 0),
        items=items,
    )
```

Update `build_request_from_folder_item()` in `src/atv_player/controllers/browse_controller.py` so existing metadata is preserved:

```python
def build_request_from_folder_item(
    self,
    clicked_item: VodItem,
    folder_items: list[VodItem],
) -> OpenPlayerRequest:
    playlist, clicked_index = self.build_playlist_from_folder(folder_items, clicked_item.vod_id)
    vod = VodItem(
        vod_id=clicked_item.vod_id,
        vod_name=clicked_item.vod_name,
        vod_pic=clicked_item.vod_pic,
        path=clicked_item.path,
        vod_remarks=clicked_item.vod_remarks,
        type_name=clicked_item.type_name,
        vod_content=clicked_item.vod_content,
        vod_year=clicked_item.vod_year,
        vod_area=clicked_item.vod_area,
        vod_lang=clicked_item.vod_lang,
        vod_director=clicked_item.vod_director,
        vod_actor=clicked_item.vod_actor,
        dbid=clicked_item.dbid,
        type=clicked_item.type,
    )
    return OpenPlayerRequest(
        vod=vod,
        playlist=playlist,
        clicked_index=clicked_index,
        source_mode="folder",
        source_path=clicked_item.path.rsplit("/", 1)[0] or "/",
        source_vod_id=clicked_item.vod_id,
        source_clicked_vod_id=clicked_item.vod_id,
    )
```

- [ ] **Step 4: Run the controller tests to verify they pass**

Run:

```bash
uv run pytest tests/test_browse_controller.py::test_build_request_from_detail_maps_title_metadata_fields tests/test_browse_controller.py::test_build_request_from_folder_item_preserves_available_metadata -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_browse_controller.py src/atv_player/models.py src/atv_player/controllers/browse_controller.py
git commit -m "feat: map player title metadata"
```

### Task 2: Split The Player Detail Pane Into Metadata And Log Views

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing player-window layout and rendering tests**

Add these tests to `tests/test_player_window_ui.py`:

```python
def test_player_window_uses_detail_container_with_metadata_and_log_views(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.details is not None
    assert window.metadata_view.isReadOnly() is True
    assert window.log_view.isReadOnly() is True
    assert window.details.layout().indexOf(window.metadata_view) != -1
    assert window.details.layout().indexOf(window.log_view) != -1


def test_player_window_renders_title_metadata_in_expected_order(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="九寨沟",
            type_name="纪录片",
            vod_year="2006",
            vod_area="中国大陆",
            vod_lang="无对白",
            vod_remarks="6.2",
            vod_director="Masa Nishimura",
            vod_actor="未知",
            vod_content="九寨沟风景名胜区位于四川省阿坝藏族羌族自治州南坪县境内。",
            dbid=19971621,
        ),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.metadata_view.toPlainText() == (
        "名称: 九寨沟\n"
        "类型: 纪录片\n"
        "年代: 2006\n"
        "地区: 中国大陆\n"
        "语言: 无对白\n"
        "评分: 6.2\n"
        "导演: Masa Nishimura\n"
        "演员: 未知\n"
        "豆瓣ID: 19971621\n"
        "\n"
        "简介:\n"
        "九寨沟风景名胜区位于四川省阿坝藏族羌族自治州南坪县境内。"
    )
```

- [ ] **Step 2: Run the player-window tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_uses_detail_container_with_metadata_and_log_views tests/test_player_window_ui.py::test_player_window_renders_title_metadata_in_expected_order -v
```

Expected: FAIL because `PlayerWindow` still exposes a single `QTextEdit` named `details` and has no `metadata_view` or `log_view`.

- [ ] **Step 3: Write the minimal player-window UI and formatting implementation**

Replace the single detail editor in `src/atv_player/ui/player_window.py` with a container that owns two read-only text widgets:

```python
self.metadata_view = QTextEdit()
self.metadata_view.setReadOnly(True)
self.log_view = QTextEdit()
self.log_view.setReadOnly(True)

self.details = QWidget()
details_layout = QVBoxLayout(self.details)
details_layout.setContentsMargins(0, 0, 0, 0)
details_layout.setSpacing(6)
details_layout.addWidget(QLabel("影片详情"))
details_layout.addWidget(self.metadata_view, 3)
details_layout.addWidget(QLabel("播放日志"))
details_layout.addWidget(self.log_view, 1)
```

Keep the existing sidebar wiring intact by continuing to add `self.details` to `self.sidebar_splitter`.

Add a formatting helper to `PlayerWindow`:

```python
def _format_metadata_text(self, vod) -> str:
    rows = [
        ("名称", vod.vod_name),
        ("类型", vod.type_name),
        ("年代", vod.vod_year),
        ("地区", vod.vod_area),
        ("语言", vod.vod_lang),
        ("评分", vod.vod_remarks),
        ("导演", vod.vod_director),
        ("演员", vod.vod_actor),
        ("豆瓣ID", str(vod.dbid) if vod.dbid else ""),
    ]
    lines = [f"{label}: {value}".rstrip() for label, value in rows]
    lines.append("")
    lines.append("简介:")
    lines.append(vod.vod_content)
    return "\n".join(lines)


def _render_metadata(self) -> None:
    if self.session is None:
        self.metadata_view.clear()
        return
    self.metadata_view.setPlainText(self._format_metadata_text(self.session.vod))
```

Call `_render_metadata()` from `open_session()` immediately after `self.session = session` and before playback starts:

```python
def open_session(self, session, start_paused: bool = False) -> None:
    self.session = session
    self._render_metadata()
    self.current_index = session.start_index
    self.current_speed = session.speed
    speed_text = self._speed_text(session.speed)
    speed_index = self.speed_combo.findText(speed_text)
    if speed_index >= 0:
        self.speed_combo.setCurrentIndex(speed_index)
    self.is_playing = not start_paused
    self._set_last_player_paused(start_paused)
    self._update_play_button_icon()
```

- [ ] **Step 4: Run the player-window tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_uses_detail_container_with_metadata_and_log_views tests/test_player_window_ui.py::test_player_window_renders_title_metadata_in_expected_order -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "feat: add player metadata detail view"
```

### Task 3: Route Runtime Messages Into The Log View And Cover Regressions

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing log-routing and session-refresh tests**

Add these tests to `tests/test_player_window_ui.py`:

```python
def test_player_window_appends_runtime_failures_to_log_view_without_overwriting_metadata(qtbot) -> None:
    class FailingVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            raise RuntimeError("boom")

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="九寨沟", type_name="纪录片", vod_content="简介文本"),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FailingVideo()

    window.open_session(session)

    assert "名称: 九寨沟" in window.metadata_view.toPlainText()
    assert "播放失败: boom" in window.log_view.toPlainText()
    assert "播放失败: boom" not in window.metadata_view.toPlainText()


def test_player_window_opening_new_session_refreshes_metadata_and_clears_old_logs(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    first_session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="九寨沟", type_name="纪录片", vod_content="第一条简介"),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    second_session = PlayerSession(
        vod=VodItem(vod_id="movie-2", vod_name="黄龙", type_name="纪录片", vod_content="第二条简介"),
        playlist=[PlayItem(title="正片", url="http://m/2.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(first_session)
    window._append_log("播放失败: boom")
    window.open_session(second_session)

    assert "名称: 黄龙" in window.metadata_view.toPlainText()
    assert "第一条简介" not in window.metadata_view.toPlainText()
    assert "播放失败: boom" not in window.log_view.toPlainText()
```

Also update the existing failure assertions so they check `window.log_view.toPlainText()` instead of the old single detail widget text:

```python
assert "恢复播放失败" in window.log_view.toPlainText()
```

- [ ] **Step 2: Run the player-window tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_appends_runtime_failures_to_log_view_without_overwriting_metadata tests/test_player_window_ui.py::test_player_window_opening_new_session_refreshes_metadata_and_clears_old_logs tests/test_player_window_ui.py::test_player_window_reports_failure_after_seek_retries_are_exhausted -v
```

Expected: FAIL because runtime messages still write into the old detail widget code path and opening a new session does not reset a dedicated log view.

- [ ] **Step 3: Write the minimal log-routing implementation**

Add logging helpers to `src/atv_player/ui/player_window.py`:

```python
def _reset_log(self) -> None:
    self.log_view.clear()


def _append_log(self, message: str) -> None:
    if not message:
        return
    if self.log_view.toPlainText():
        self.log_view.append(message)
        return
    self.log_view.setPlainText(message)
```

Reset the log on each new session and add the current item context when loading:

```python
def open_session(self, session, start_paused: bool = False) -> None:
    self.session = session
    self._render_metadata()
    self._reset_log()
    self.current_index = session.start_index
    self.current_speed = session.speed
    speed_text = self._speed_text(session.speed)
    speed_index = self.speed_combo.findText(speed_text)
    if speed_index >= 0:
        self.speed_combo.setCurrentIndex(speed_index)
    self.is_playing = not start_paused


def _load_current_item(self, start_position_seconds: int = 0, pause: bool = False) -> None:
    if self.session is None:
        return
    current_item = self.session.playlist[self.current_index]
    self._append_log(f"当前: {current_item.title}")
    self._append_log(f"URL: {current_item.url}")
    try:
        self.video.load(current_item.url, pause=pause, start_seconds=start_position_seconds)
        self.video.set_speed(self.current_speed)
        self.video.set_volume(self.volume_slider.value())
    except Exception as exc:
        self._append_log(f"播放失败: {exc}")
```

Replace every existing `self.details.append` call with `_append_log()`, for example:

```python
self._append_log("恢复播放失败: 媒体尚未进入可跳转状态")
self._append_log(f"恢复播放失败: {exc}")
self._append_log(f"进度上报失败: {exc}")
self._append_log(f"跳转失败: {exc}")
self._append_log(f"静音失败: {exc}")
self._append_log(f"倍速设置失败: {exc}")
self._append_log(f"音量设置失败: {exc}")
```

Do not change `_apply_visibility_state()` beyond continuing to hide or show `self.details` as the detail container.

- [ ] **Step 4: Run the focused suite to verify it passes**

Run:

```bash
uv run pytest tests/test_browse_controller.py tests/test_player_window_ui.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_browse_controller.py tests/test_player_window_ui.py src/atv_player/models.py src/atv_player/controllers/browse_controller.py src/atv_player/ui/player_window.py
git commit -m "feat: separate player metadata and logs"
```
