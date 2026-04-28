# Danmaku Global Preference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the player's danmaku on/off and line-count selection in global app settings and reuse that preference for later playback.

**Architecture:** Extend `AppConfig` and the single-row `app_config` table with semantic danmaku preference fields, then teach `PlayerWindow` to translate those fields into combo-box state and danmaku loading behavior. Keep persistence local to the existing `save_config` callback so player-side changes match the current preferred-parse workflow.

**Tech Stack:** Python 3.13, PySide6, SQLite, pytest, uv

---

### Task 1: Persist Danmaku Preference In AppConfig And SQLite

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing storage tests**

```python
def test_settings_repository_round_trip_persists_preferred_danmaku_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        preferred_danmaku_enabled=False,
        preferred_danmaku_line_count=4,
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.preferred_danmaku_enabled is False
    assert saved.preferred_danmaku_line_count == 4
    assert saved == config


def test_settings_repository_migrates_missing_preferred_danmaku_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE app_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                base_url TEXT NOT NULL,
                username TEXT NOT NULL,
                token TEXT NOT NULL,
                vod_token TEXT NOT NULL,
                last_path TEXT NOT NULL,
                last_active_window TEXT NOT NULL DEFAULT 'main',
                last_playback_source TEXT NOT NULL DEFAULT 'browse',
                last_playback_source_key TEXT NOT NULL DEFAULT '',
                last_playback_mode TEXT NOT NULL DEFAULT '',
                last_playback_path TEXT NOT NULL DEFAULT '',
                last_playback_vod_id TEXT NOT NULL DEFAULT '',
                last_playback_clicked_vod_id TEXT NOT NULL DEFAULT '',
                last_player_paused INTEGER NOT NULL DEFAULT 0,
                player_volume INTEGER NOT NULL DEFAULT 100,
                player_muted INTEGER NOT NULL DEFAULT 0,
                preferred_parse_key TEXT NOT NULL DEFAULT '',
                main_window_geometry BLOB,
                player_window_geometry BLOB,
                player_main_splitter_state BLOB,
                browse_content_splitter_state BLOB
            )
            """
        )
        conn.execute(
            """
            INSERT INTO app_config (
                id, base_url, username, token, vod_token, last_path,
                last_active_window, last_playback_source, last_playback_source_key,
                last_playback_mode, last_playback_path, last_playback_vod_id,
                last_playback_clicked_vod_id, last_player_paused, player_volume,
                player_muted, preferred_parse_key, main_window_geometry,
                player_window_geometry, player_main_splitter_state, browse_content_splitter_state
            )
            VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/', 'main', 'browse', '', '', '', '', '', 0, 100, 0, '', NULL, NULL, NULL, NULL)
            """
        )

    repo = SettingsRepository(db_path)
    saved = repo.load_config()

    assert saved.preferred_danmaku_enabled is True
    assert saved.preferred_danmaku_line_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_storage.py::test_settings_repository_round_trip_persists_preferred_danmaku_fields tests/test_storage.py::test_settings_repository_migrates_missing_preferred_danmaku_columns -v`

Expected: FAIL because `AppConfig` and `SettingsRepository` do not expose `preferred_danmaku_enabled` or `preferred_danmaku_line_count`.

- [ ] **Step 3: Write the minimal storage implementation**

```python
# src/atv_player/models.py
@dataclass(slots=True)
class AppConfig:
    base_url: str = "http://127.0.0.1:4567"
    username: str = ""
    token: str = ""
    vod_token: str = ""
    last_path: str = "/"
    last_active_window: str = "main"
    last_playback_source: str = "browse"
    last_playback_source_key: str = ""
    last_playback_mode: str = ""
    last_playback_path: str = ""
    last_playback_vod_id: str = ""
    last_playback_clicked_vod_id: str = ""
    last_player_paused: bool = False
    player_volume: int = 100
    player_muted: bool = False
    preferred_parse_key: str = ""
    preferred_danmaku_enabled: bool = True
    preferred_danmaku_line_count: int = 1
    main_window_geometry: bytes | None = None
    player_window_geometry: bytes | None = None
    player_main_splitter_state: bytes | None = None
    browse_content_splitter_state: bytes | None = None
```

```python
# src/atv_player/storage.py
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS app_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        base_url TEXT NOT NULL,
        username TEXT NOT NULL,
        token TEXT NOT NULL,
        vod_token TEXT NOT NULL,
        last_path TEXT NOT NULL,
        last_active_window TEXT NOT NULL DEFAULT 'main',
        last_playback_source TEXT NOT NULL DEFAULT 'browse',
        last_playback_source_key TEXT NOT NULL DEFAULT '',
        last_playback_mode TEXT NOT NULL DEFAULT '',
        last_playback_path TEXT NOT NULL DEFAULT '',
        last_playback_vod_id TEXT NOT NULL DEFAULT '',
        last_playback_clicked_vod_id TEXT NOT NULL DEFAULT '',
        last_player_paused INTEGER NOT NULL DEFAULT 0,
        player_volume INTEGER NOT NULL DEFAULT 100,
        player_muted INTEGER NOT NULL DEFAULT 0,
        preferred_parse_key TEXT NOT NULL DEFAULT '',
        preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1,
        preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1,
        main_window_geometry BLOB,
        player_window_geometry BLOB,
        player_main_splitter_state BLOB,
        browse_content_splitter_state BLOB
    )
    """
)
if "preferred_danmaku_enabled" not in columns:
    conn.execute(
        "ALTER TABLE app_config ADD COLUMN preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1"
    )
if "preferred_danmaku_line_count" not in columns:
    conn.execute(
        "ALTER TABLE app_config ADD COLUMN preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1"
    )
```

```python
# src/atv_player/storage.py
row = conn.execute(
    """
    SELECT
        base_url,
        username,
        token,
        vod_token,
        last_path,
        last_active_window,
        last_playback_source,
        last_playback_source_key,
        last_playback_mode,
        last_playback_path,
        last_playback_vod_id,
        last_playback_clicked_vod_id,
        last_player_paused,
        player_volume,
        player_muted,
        preferred_parse_key,
        preferred_danmaku_enabled,
        preferred_danmaku_line_count,
        main_window_geometry,
        player_window_geometry,
        player_main_splitter_state,
        browse_content_splitter_state
    FROM app_config
    WHERE id = 1
    """
).fetchone()
values = list(row)
values[12] = bool(values[12])
values[14] = bool(values[14])
values[16] = bool(values[16])
return AppConfig(*values)
```

```python
# src/atv_player/storage.py
conn.execute(
    """
    UPDATE app_config
    SET
        base_url = ?,
        username = ?,
        token = ?,
        vod_token = ?,
        last_path = ?,
        last_active_window = ?,
        last_playback_source = ?,
        last_playback_source_key = ?,
        last_playback_mode = ?,
        last_playback_path = ?,
        last_playback_vod_id = ?,
        last_playback_clicked_vod_id = ?,
        last_player_paused = ?,
        player_volume = ?,
        player_muted = ?,
        preferred_parse_key = ?,
        preferred_danmaku_enabled = ?,
        preferred_danmaku_line_count = ?,
        main_window_geometry = ?,
        player_window_geometry = ?,
        player_main_splitter_state = ?,
        browse_content_splitter_state = ?
    WHERE id = 1
    """,
    (
        config.base_url,
        config.username,
        config.token,
        config.vod_token,
        config.last_path,
        config.last_active_window,
        config.last_playback_source,
        config.last_playback_source_key,
        config.last_playback_mode,
        config.last_playback_path,
        config.last_playback_vod_id,
        config.last_playback_clicked_vod_id,
        int(config.last_player_paused),
        config.player_volume,
        int(config.player_muted),
        config.preferred_parse_key,
        int(config.preferred_danmaku_enabled),
        config.preferred_danmaku_line_count,
        config.main_window_geometry,
        config.player_window_geometry,
        config.player_main_splitter_state,
        config.browse_content_splitter_state,
    ),
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_storage.py::test_settings_repository_round_trip_persists_preferred_danmaku_fields tests/test_storage.py::test_settings_repository_migrates_missing_preferred_danmaku_columns -v`

Expected: PASS with 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/models.py src/atv_player/storage.py tests/test_storage.py
git commit -m "feat: persist danmaku preference in app config"
```

### Task 2: Apply Saved Danmaku Preference When Playback Starts

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing player-start tests**

```python
def test_player_window_uses_saved_off_danmaku_preference_on_open_session(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            return 70

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), config=AppConfig(preferred_danmaku_enabled=False))
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.video.loaded_danmaku_paths == []
    assert window.danmaku_combo.currentText() == "关闭"
```

```python
def test_player_window_uses_saved_danmaku_line_count_on_open_session(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 70

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(preferred_danmaku_enabled=True, preferred_danmaku_line_count=4),
    )
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert len(window.video.loaded_danmaku_paths) == 1
    assert window.danmaku_combo.currentText() == "4行"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_uses_saved_off_danmaku_preference_on_open_session tests/test_player_window_ui.py::test_player_window_uses_saved_danmaku_line_count_on_open_session -v`

Expected: FAIL because `PlayerWindow` always enables danmaku with `1` line when XML is present.

- [ ] **Step 3: Write the minimal player-start implementation**

```python
# src/atv_player/ui/player_window.py
def _preferred_danmaku_line_count(self) -> int:
    configured = 1 if self.config is None else int(getattr(self.config, "preferred_danmaku_line_count", 1))
    return max(1, min(configured, 5))


def _preferred_danmaku_enabled(self) -> bool:
    if self.config is None:
        return True
    return bool(getattr(self.config, "preferred_danmaku_enabled", True))


def _preferred_danmaku_combo_index(self) -> int:
    if not self._preferred_danmaku_enabled():
        return 1
    line_count = self._preferred_danmaku_line_count()
    return 0 if line_count == 1 else line_count + 1
```

```python
# src/atv_player/ui/player_window.py
def _configure_danmaku_for_current_item(self) -> None:
    self._danmaku_retry_timer.stop()
    xml_text = self._current_play_item_danmaku_xml()
    if not xml_text:
        if self.session is not None and self.session.playlist[self.current_index].danmaku_pending:
            self._reset_danmaku_combo()
            if not self._pending_danmaku_timer.isActive():
                self._pending_danmaku_timer.start()
            return
        self._pending_danmaku_timer.stop()
        self._reset_danmaku_combo()
        self._danmaku_retry_attempts = 0
        return
    self._pending_danmaku_timer.stop()
    preferred_index = self._preferred_danmaku_combo_index()
    self._reset_danmaku_combo(enabled=True, current_index=preferred_index)
    if preferred_index == 1:
        self._clear_active_danmaku()
        self._danmaku_retry_attempts = 0
        return
    line_count = self._preferred_danmaku_line_count()
    try:
        self._enable_danmaku(line_count)
        self._reset_danmaku_combo(enabled=True, current_index=preferred_index)
        self._danmaku_retry_attempts = 0
    except Exception as exc:
        if self._should_retry_danmaku_load(exc):
            self._schedule_danmaku_retry()
            return
        self._append_log(f"弹幕加载失败: {exc}")
        self._clear_active_danmaku()
        self._reset_danmaku_combo(enabled=True, current_index=1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_uses_saved_off_danmaku_preference_on_open_session tests/test_player_window_ui.py::test_player_window_uses_saved_danmaku_line_count_on_open_session -v`

Expected: PASS with 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: apply saved danmaku preference on playback start"
```

### Task 3: Persist Manual Danmaku Changes And Reuse Them After Async Resolution

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing persistence and async tests**

```python
def test_player_window_saves_preferred_danmaku_selection_when_user_changes_combo(qtbot) -> None:
    saved = {"called": 0}

    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self.removed_danmaku_track_ids: list[int] = []
            self._next_track_id = 70

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            if track_id is not None:
                self.removed_danmaku_track_ids.append(track_id)

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    config = AppConfig()
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("called", saved["called"] + 1),
    )
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.danmaku_combo.setCurrentIndex(5)

    assert config.preferred_danmaku_enabled is True
    assert config.preferred_danmaku_line_count == 4
    assert saved["called"] == 1
```

```python
def test_player_window_applies_saved_danmaku_line_count_after_async_resolution(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return False

        def supports_subtitle_ass_override(self) -> bool:
            return False

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="第1集", url="http://m/1.m3u8", danmaku_pending=True)],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(preferred_danmaku_enabled=True, preferred_danmaku_line_count=3),
    )
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    session.playlist[0].danmaku_xml = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'
    session.playlist[0].danmaku_pending = False

    qtbot.waitUntil(lambda: len(window.video.loaded_danmaku_paths) == 1)
    assert window.danmaku_combo.currentText() == "3行"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_saves_preferred_danmaku_selection_when_user_changes_combo tests/test_player_window_ui.py::test_player_window_applies_saved_danmaku_line_count_after_async_resolution -v`

Expected: FAIL because manual danmaku changes are not persisted and async completion still re-applies `1` line.

- [ ] **Step 3: Write the minimal persistence and async implementation**

```python
# src/atv_player/ui/player_window.py
def _save_preferred_danmaku_selection(self, index: int) -> None:
    if self.config is None or index < 0:
        return
    enabled = index != 1
    line_count = 1 if index in (0, 1, 2) else index - 1
    line_count = max(1, min(line_count, 5))
    if (
        self.config.preferred_danmaku_enabled == enabled
        and self.config.preferred_danmaku_line_count == line_count
    ):
        return
    self.config.preferred_danmaku_enabled = enabled
    self.config.preferred_danmaku_line_count = line_count
    self._save_config()
```

```python
# src/atv_player/ui/player_window.py
def _change_danmaku_selection(self, index: int) -> None:
    if index < 0 or not self._current_play_item_danmaku_xml():
        return
    self._save_preferred_danmaku_selection(index)
    if index == 1:
        self._clear_active_danmaku()
        return
    line_count = 1 if index in (0, 2) else index - 1
    try:
        self._enable_danmaku(line_count)
    except Exception as exc:
        self._append_log(f"弹幕切换失败: {exc}")
        self._clear_active_danmaku()
        self._reset_danmaku_combo(enabled=True, current_index=1)
```

```python
# src/atv_player/ui/player_window.py
def _refresh_pending_danmaku_for_current_item(self) -> None:
    if self.session is None:
        self._pending_danmaku_timer.stop()
        return
    current_item = self.session.playlist[self.current_index]
    if current_item.danmaku_xml:
        self._pending_danmaku_timer.stop()
        self._configure_danmaku_for_current_item()
        return
    if not current_item.danmaku_pending:
        self._pending_danmaku_timer.stop()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_saves_preferred_danmaku_selection_when_user_changes_combo tests/test_player_window_ui.py::test_player_window_applies_saved_danmaku_line_count_after_async_resolution -v`

Expected: PASS with 2 passed.

- [ ] **Step 5: Run the focused regression suite**

Run: `uv run pytest tests/test_storage.py tests/test_player_window_ui.py -v`

Expected: PASS with the updated storage and player-window suite green.

- [ ] **Step 6: Commit**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: remember danmaku preference globally"
```
