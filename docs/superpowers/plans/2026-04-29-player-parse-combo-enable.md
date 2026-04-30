# Player Parse Combo Enable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Disable the player parse combo by default and only enable it for the current spider playback item after `playerContent()` returns `parse=1`.

**Architecture:** Store parse availability on each `PlayItem` so the playback loader remains the single source of truth for deferred spider playback. Refresh the parse combo from the current item during session open, item load, loader success, and loader failure so the control behaves contextually without changing parser resolution semantics.

**Tech Stack:** Python 3.13, PySide6, pytest, existing spider plugin playback loader and player window UI.

---

## File Map

- `src/atv_player/models.py`
  Responsibility: extend `PlayItem` with a persistent per-item parse-required flag.
- `src/atv_player/plugins/controller.py`
  Responsibility: set the parse-required flag when `playerContent()` indicates parse resolution.
- `src/atv_player/ui/player_window.py`
  Responsibility: keep the parse combo populated, but drive `setEnabled()` from the current play item state across playback transitions.
- `tests/test_spider_plugin_controller.py`
  Responsibility: lock parse-required state for `parse=1` and `parse=0` spider playback.
- `tests/test_player_window_ui.py`
  Responsibility: lock parse combo default-disabled behavior and current-item-based enabling.

### Task 1: Track Parse Requirement On Play Items

**Files:**
- Modify: `tests/test_spider_plugin_controller.py`
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/plugins/controller.py`

- [ ] **Step 1: Write the failing controller tests**

Add one assertion to the existing parse-resolution test and one new direct-play test:

```python
def test_controller_resolves_parse_required_player_content_via_parser_service() -> None:
    parser_calls: list[tuple[str, str, str]] = []

    class FakeParserService:
        def resolve(self, flag: str, url: str, preferred_key: str = ""):
            parser_calls.append((flag, url, preferred_key))
            return type(
                "Result",
                (),
                {
                    "parser_key": "jx2",
                    "parser_label": "jx2",
                    "url": "https://media.example/resolved.m3u8",
                    "headers": {"Referer": "https://page.example"},
                },
            )()

    controller = SpiderPluginController(
        ParseRequiredSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        playback_parser_service=FakeParserService(),
        preferred_parse_key_loader=lambda: "jx1",
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert parser_calls == [("备用线", "https://page.example/play/1", "jx1")]
    assert first.parse_required is True
    assert first.url == "https://media.example/resolved.m3u8"
    assert first.headers == {"Referer": "https://page.example"}


def test_controller_keeps_direct_play_items_parse_disabled() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.parse_required is False
    assert first.url == "https://stream.example/play/1.m3u8"
```

- [ ] **Step 2: Run the focused controller tests and verify they fail**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py::test_controller_resolves_parse_required_player_content_via_parser_service tests/test_spider_plugin_controller.py::test_controller_keeps_direct_play_items_parse_disabled -v
```

Expected: FAIL with `AttributeError` or assertion failure because `PlayItem` does not yet expose `parse_required`.

- [ ] **Step 3: Write the minimal implementation**

Extend `PlayItem` and mark the flag in the spider playback loader:

```python
@dataclass(slots=True)
class PlayItem:
    title: str
    url: str
    path: str = ""
    index: int = 0
    size: int = 0
    duration_seconds: int = 0
    vod_id: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    play_source: str = ""
    media_title: str = ""
    parse_required: bool = False
    danmaku_title_only: bool = False
    ...
```

```python
payload = self._spider.playerContent(item.play_source, item.vod_id, []) or {}
parse_required = int(payload.get("parse") or 0) == 1
item.parse_required = parse_required
url = str(payload.get("url") or "").strip()

if parse_required:
    if self._playback_parser_service is None:
        raise ValueError("当前插件未配置内置解析")
    result = self._playback_parser_service.resolve(
        item.play_source,
        url,
        preferred_key="" if self._preferred_parse_key_loader is None else self._preferred_parse_key_loader(),
    )
    item.url = result.url
    item.headers = dict(result.headers)
    ...
```

- [ ] **Step 4: Re-run the focused controller tests and verify they pass**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py::test_controller_resolves_parse_required_player_content_via_parser_service tests/test_spider_plugin_controller.py::test_controller_keeps_direct_play_items_parse_disabled -v
```

Expected: PASS for both tests.

- [ ] **Step 5: Commit the controller/model slice**

```bash
git add tests/test_spider_plugin_controller.py src/atv_player/models.py src/atv_player/plugins/controller.py
git commit -m "feat: track parse-required spider playback items"
```

### Task 2: Disable The Parse Combo By Default

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Modify: `src/atv_player/ui/player_window.py`

- [ ] **Step 1: Write the failing player-window tests**

Update the existing parse-combo test and add two current-item state tests:

```python
def test_player_window_exposes_parse_combo_with_builtin_entries(qtbot) -> None:
    class FakeParserService:
        def parsers(self):
            return [
                type("Parser", (), {"key": "fish", "label": "fish"})(),
                type("Parser", (), {"key": "jx1", "label": "jx1"})(),
                type("Parser", (), {"key": "jx2", "label": "jx2"})(),
                type("Parser", (), {"key": "mg1", "label": "mg1"})(),
                type("Parser", (), {"key": "tx1", "label": "tx1"})(),
            ]

    window = PlayerWindow(FakePlayerController(), config=AppConfig(), playback_parser_service=FakeParserService())
    qtbot.addWidget(window)

    assert window.parse_combo.count() == 6
    assert window.parse_combo.itemText(0) == "解析"
    assert [window.parse_combo.itemText(index) for index in range(1, window.parse_combo.count())] == [
        "fish",
        "jx1",
        "jx2",
        "mg1",
        "tx1",
    ]
    assert window.parse_combo.isEnabled() is False


def test_player_window_enables_parse_combo_for_current_parse_required_item(qtbot) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig(), playback_parser_service=type(
        "FakeParserService",
        (),
        {"parsers": lambda self: [type("Parser", (), {"key": "jx1", "label": "jx1"})()]},
    )())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="https://media.example/1.m3u8", parse_required=True)],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    assert window.parse_combo.isEnabled() is True


def test_player_window_disables_parse_combo_when_switching_to_non_parse_item(qtbot) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig(), playback_parser_service=type(
        "FakeParserService",
        (),
        {"parsers": lambda self: [type("Parser", (), {"key": "jx1", "label": "jx1"})()]},
    )())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(title="Episode 1", url="https://media.example/1.m3u8", parse_required=True),
            PlayItem(title="Episode 2", url="https://media.example/2.m3u8", parse_required=False),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)
    assert window.parse_combo.isEnabled() is True

    window._play_item_at_index(1)

    assert window.parse_combo.isEnabled() is False
```

- [ ] **Step 2: Run the focused player-window tests and verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_exposes_parse_combo_with_builtin_entries tests/test_player_window_ui.py::test_player_window_enables_parse_combo_for_current_parse_required_item tests/test_player_window_ui.py::test_player_window_disables_parse_combo_when_switching_to_non_parse_item -v
```

Expected: FAIL because the combo is currently always enabled once populated.

- [ ] **Step 3: Write the minimal player-window implementation**

Add a dedicated refresh helper and call it from session open, item load preparation, loader success, and loader failure:

```python
def _current_item_requires_parse(self) -> bool:
    if self.session is None:
        return False
    if not (0 <= self.current_index < len(self.session.playlist)):
        return False
    return bool(getattr(self.session.playlist[self.current_index], "parse_required", False))


def _refresh_parse_combo_enabled_state(self) -> None:
    self.parse_combo.setEnabled(self._current_item_requires_parse())
```

```python
def _populate_parse_combo(self) -> None:
    self.parse_combo.blockSignals(True)
    self.parse_combo.clear()
    self.parse_combo.addItem("解析", "")
    if self._playback_parser_service is not None:
        for parser in self._playback_parser_service.parsers():
            self.parse_combo.addItem(parser.label, parser.key)
    preferred_parse_key = "" if self.config is None else getattr(self.config, "preferred_parse_key", "")
    preferred_index = self.parse_combo.findData(preferred_parse_key)
    self.parse_combo.setCurrentIndex(preferred_index if preferred_index >= 0 else 0)
    self.parse_combo.setEnabled(False)
    self.parse_combo.blockSignals(False)
```

```python
def open_session(self, session, start_paused: bool = False) -> None:
    ...
    self._reset_audio_combo()
    self._refresh_parse_combo_enabled_state()
    if session.initial_log_message:
        self._append_log(session.initial_log_message)
    ...
```

```python
def _load_current_item(...):
    ...
    self._reset_danmaku_combo()
    self._refresh_parse_combo_enabled_state()
    if not self._prepare_current_play_item(...):
        return
    self._refresh_parse_combo_enabled_state()
    self._start_current_item_playback(...)
```

```python
def _handle_playback_loader_succeeded(self, request_id: int, load_result: PlaybackLoadResult | None) -> None:
    ...
    self._apply_playback_loader_result(load_result)
    self._refresh_parse_combo_enabled_state()
    current_item = self.session.playlist[self.current_index]
    ...
```

```python
def _handle_playback_loader_failed(self, request_id: int, message: str) -> None:
    ...
    self._restore_current_index(pending_loader.previous_index)
    self._refresh_parse_combo_enabled_state()
    self._append_log(f"播放失败: {message}")
```

- [ ] **Step 4: Re-run the focused player-window tests and verify they pass**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_exposes_parse_combo_with_builtin_entries tests/test_player_window_ui.py::test_player_window_enables_parse_combo_for_current_parse_required_item tests/test_player_window_ui.py::test_player_window_disables_parse_combo_when_switching_to_non_parse_item -v
```

Expected: PASS for all three tests.

- [ ] **Step 5: Commit the player-window slice**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "feat: gate parse combo by current play item"
```

### Task 3: Preserve Parser Preference Interaction

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Modify: `src/atv_player/ui/player_window.py` if needed

- [ ] **Step 1: Write the failing parser-selection test update**

Adjust the existing preference test so it enables the combo the same way real playback would:

```python
def test_player_window_saves_preferred_parse_key_when_user_selects_parser(qtbot) -> None:
    saved = {"called": 0}
    config = AppConfig()

    class FakeParserService:
        def parsers(self):
            return [
                type("Parser", (), {"key": "fish", "label": "fish"})(),
                type("Parser", (), {"key": "jx1", "label": "jx1"})(),
            ]

    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("called", saved["called"] + 1),
        playback_parser_service=FakeParserService(),
    )
    qtbot.addWidget(window)
    window.parse_combo.setEnabled(True)

    window.parse_combo.setCurrentIndex(2)

    assert config.preferred_parse_key == "jx1"
    assert saved["called"] == 1
```

- [ ] **Step 2: Run the focused preference test and verify it fails only if the gating logic broke interaction**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_saves_preferred_parse_key_when_user_selects_parser -v
```

Expected: PASS if the combo still emits when enabled. If it fails, the failure should show that the new gating logic blocked valid enabled interaction.

- [ ] **Step 3: Write the minimal fix if interaction regressed**

Keep `_change_parse_selection()` unchanged except for any guard needed to allow replay only when the enabled combo is acting on the current item:

```python
def _change_parse_selection(self, index: int) -> None:
    if self.config is None:
        return
    parser_key = str(self.parse_combo.itemData(index) or "")
    if getattr(self.config, "preferred_parse_key", "") == parser_key:
        return
    self.config.preferred_parse_key = parser_key
    self._save_config()
    if (
        self.session is not None
        and self.session.playback_loader is not None
        and 0 <= self.current_index < len(self.session.playlist)
        and not self.session.playlist[self.current_index].url
    ):
        self._replay_current_item()
```

- [ ] **Step 4: Run the targeted regression suite**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py::test_controller_resolves_parse_required_player_content_via_parser_service tests/test_spider_plugin_controller.py::test_controller_keeps_direct_play_items_parse_disabled tests/test_player_window_ui.py::test_player_window_exposes_parse_combo_with_builtin_entries tests/test_player_window_ui.py::test_player_window_enables_parse_combo_for_current_parse_required_item tests/test_player_window_ui.py::test_player_window_disables_parse_combo_when_switching_to_non_parse_item tests/test_player_window_ui.py::test_player_window_saves_preferred_parse_key_when_user_selects_parser -v
```

Expected: PASS for all six tests.

- [ ] **Step 5: Commit the regression lock**

```bash
git add tests/test_spider_plugin_controller.py tests/test_player_window_ui.py src/atv_player/models.py src/atv_player/plugins/controller.py src/atv_player/ui/player_window.py
git commit -m "test: lock parse combo enable behavior"
```

## Self-Review

- Spec coverage: the plan covers the new `PlayItem` state, spider `playerContent()` marking, parse combo default-disabled behavior, current-item-based enable/disable refresh, and preserved parser preference interaction.
- Placeholder scan: no `TODO`, `TBD`, or generic “write tests” steps remain; each test and command is concrete.
- Type consistency: the plan uses one field name, `parse_required`, across model, controller, UI, and tests.
