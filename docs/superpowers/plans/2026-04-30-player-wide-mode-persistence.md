# Player Wide Mode Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the player window's wide-mode preference so restarting the app restores wide mode for any newly opened player window when the last player window was wide.

**Architecture:** Add one explicit boolean preference, `player_wide_mode`, to `AppConfig` and SQLite storage instead of inferring wide mode from splitter state. Keep `player_main_splitter_state` responsible only for the visible-sidebar layout, and make `PlayerWindow` read/write the new preference during initialization and wide-mode toggles.

**Tech Stack:** Python 3, PySide6, SQLite, pytest, pytest-qt

---

## File Structure

- `src/atv_player/models.py`
  Owns the `AppConfig` dataclass fields and defaults. Add the new `player_wide_mode: bool = False` preference here.
- `src/atv_player/storage.py`
  Owns the `app_config` table schema, column migrations, load/save SQL, and bool conversions. Add the new column and round-trip logic here.
- `src/atv_player/ui/player_window.py`
  Owns player window initialization, wide-mode toggling, splitter restore behavior, and config persistence hooks. Read the stored preference at startup and save it when toggled.
- `tests/test_storage.py`
  Owns storage round-trip and migration regression coverage. Add focused tests for new-column persistence and old-schema migration.
- `tests/test_player_window_ui.py`
  Owns player-window UI behavior and persistence regressions. Add focused tests for restoring wide mode from config and persisting toggle changes.

### Task 1: Add Storage Regression Tests

**Files:**
- Modify: `tests/test_storage.py:201-260`
- Modify: `tests/test_storage.py:492-560`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing round-trip persistence test**

```python
def test_settings_repository_round_trip_persists_player_wide_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        player_wide_mode=True,
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.player_wide_mode is True
    assert saved == config
```

- [ ] **Step 2: Run the round-trip test to verify it fails**

Run: `uv run pytest tests/test_storage.py::test_settings_repository_round_trip_persists_player_wide_mode -q`

Expected: FAIL with `TypeError` for an unexpected `AppConfig` keyword argument or with an assertion failure because `player_wide_mode` is not persisted.

- [ ] **Step 3: Write the failing migration test for old databases**

```python
def test_settings_repository_migrates_missing_player_wide_mode_column(tmp_path: Path) -> None:
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
        conn.execute(
            """
            INSERT INTO app_config (
                id, base_url, username, token, vod_token, last_path,
                last_active_window, last_playback_mode, last_playback_path,
                last_playback_vod_id, last_playback_clicked_vod_id,
                last_player_paused, player_volume, player_muted,
                preferred_parse_key, preferred_danmaku_enabled,
                preferred_danmaku_line_count, main_window_geometry,
                player_window_geometry, player_main_splitter_state,
                browse_content_splitter_state
            )
            VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/TV', 'player', 'detail', '/TV', 'vod-1', 'vod-1', 0, 100, 0, '', 1, 1, NULL, NULL, NULL, NULL)
            """
        )

    repo = SettingsRepository(db_path)
    saved = repo.load_config()

    assert saved.player_wide_mode is False
```

- [ ] **Step 4: Run the migration test to verify it fails**

Run: `uv run pytest tests/test_storage.py::test_settings_repository_migrates_missing_player_wide_mode_column -q`

Expected: FAIL because the repository does not yet add or read a `player_wide_mode` column.

- [ ] **Step 5: Commit the failing storage tests**

```bash
git add tests/test_storage.py
git commit -m "test: cover player wide mode storage"
```

### Task 2: Add Player Window Regression Tests

**Files:**
- Modify: `tests/test_player_window_ui.py:7460-7545`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing startup-restore test**

```python
def test_player_window_starts_in_wide_mode_when_config_requests_it(qtbot) -> None:
    config = AppConfig(player_wide_mode=True)
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.show()

    assert window.wide_button.isChecked() is True
    assert window.sidebar_container.isHidden() is True
    assert window.main_splitter.sizes()[1] == 0
```

- [ ] **Step 2: Run the startup-restore test to verify it fails**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_starts_in_wide_mode_when_config_requests_it -q`

Expected: FAIL because `PlayerWindow` currently ignores `config.player_wide_mode` during initialization.

- [ ] **Step 3: Write the failing toggle-persistence test**

```python
def test_player_window_toggling_wide_mode_updates_config_and_saves(qtbot) -> None:
    saved = {"count": 0}
    config = AppConfig(player_wide_mode=False)
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)
    window.show()

    window.wide_button.click()

    assert config.player_wide_mode is True
    assert saved["count"] >= 1

    window.wide_button.click()

    assert config.player_wide_mode is False
    assert saved["count"] >= 2
```

- [ ] **Step 4: Run the toggle-persistence test to verify it fails**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_toggling_wide_mode_updates_config_and_saves -q`

Expected: FAIL because the toggle handler does not yet write `player_wide_mode` or save immediately.

- [ ] **Step 5: Commit the failing player-window tests**

```bash
git add tests/test_player_window_ui.py
git commit -m "test: cover player wide mode restore"
```

### Task 3: Implement Config And SQLite Support

**Files:**
- Modify: `src/atv_player/models.py:12-34`
- Modify: `src/atv_player/storage.py:25-149`
- Modify: `src/atv_player/storage.py:156-188`
- Modify: `src/atv_player/storage.py:194-241`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Add the new config field in `AppConfig`**

```python
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
    player_wide_mode: bool = False
    preferred_parse_key: str = ""
    preferred_danmaku_enabled: bool = True
    preferred_danmaku_line_count: int = 1
    main_window_geometry: bytes | None = None
    player_window_geometry: bytes | None = None
    player_main_splitter_state: bytes | None = None
    browse_content_splitter_state: bytes | None = None
```

- [ ] **Step 2: Add schema, migration, load, and save support in `SettingsRepository`**

```python
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
    player_wide_mode INTEGER NOT NULL DEFAULT 0,
    preferred_parse_key TEXT NOT NULL DEFAULT '',
    preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1,
    preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1,
    main_window_geometry BLOB,
    player_window_geometry BLOB,
    player_main_splitter_state BLOB,
    browse_content_splitter_state BLOB
)

if "player_wide_mode" not in columns:
    conn.execute(
        "ALTER TABLE app_config ADD COLUMN player_wide_mode INTEGER NOT NULL DEFAULT 0"
    )

INSERT INTO app_config (
    id,
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
    player_wide_mode,
    preferred_parse_key,
    preferred_danmaku_enabled,
    preferred_danmaku_line_count,
    main_window_geometry,
    player_window_geometry,
    player_main_splitter_state,
    browse_content_splitter_state
)
VALUES (1, 'http://127.0.0.1:4567', '', '', '', '/', 'main', 'browse', '', '', '', '', '', 0, 100, 0, 0, '', 1, 1, NULL, NULL, NULL, NULL)
ON CONFLICT(id) DO NOTHING
```

```python
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
    player_wide_mode,
    preferred_parse_key,
    preferred_danmaku_enabled,
    preferred_danmaku_line_count,
    main_window_geometry,
    player_window_geometry,
    player_main_splitter_state,
    browse_content_splitter_state
FROM app_config
WHERE id = 1
```

```python
values = list(row)
values[12] = bool(values[12])
values[14] = bool(values[14])
values[15] = bool(values[15])
values[17] = bool(values[17])
return AppConfig(*values)
```

```python
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
    player_wide_mode = ?,
    preferred_parse_key = ?,
    preferred_danmaku_enabled = ?,
    preferred_danmaku_line_count = ?,
    main_window_geometry = ?,
    player_window_geometry = ?,
    player_main_splitter_state = ?,
    browse_content_splitter_state = ?
WHERE id = 1
```

```python
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
    int(config.player_wide_mode),
    config.preferred_parse_key,
    int(config.preferred_danmaku_enabled),
    config.preferred_danmaku_line_count,
    config.main_window_geometry,
    config.player_window_geometry,
    config.player_main_splitter_state,
    config.browse_content_splitter_state,
)
```

- [ ] **Step 3: Run the storage tests to verify they pass**

Run: `uv run pytest tests/test_storage.py::test_settings_repository_round_trip_persists_player_wide_mode tests/test_storage.py::test_settings_repository_migrates_missing_player_wide_mode_column -q`

Expected: PASS with both storage tests green.

- [ ] **Step 4: Commit the config and storage implementation**

```bash
git add src/atv_player/models.py src/atv_player/storage.py tests/test_storage.py
git commit -m "feat: persist player wide mode preference"
```

### Task 4: Implement Player Window Restore And Toggle Persistence

**Files:**
- Modify: `src/atv_player/ui/player_window.py:346-348`
- Modify: `src/atv_player/ui/player_window.py:524-540`
- Modify: `src/atv_player/ui/player_window.py:1527-1534`
- Modify: `src/atv_player/ui/player_window.py:3173-3178`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Initialize the wide button from config before initial visibility is applied**

```python
self.wide_button = self._create_icon_button("grid.svg", "宽屏", "W")
self.fullscreen_button = self._create_icon_button("maximize.svg", "全屏", "Enter")
self.wide_button.setCheckable(True)
if self.config is not None:
    self.wide_button.setChecked(bool(self.config.player_wide_mode))
```

```python
self._restore_main_splitter_state()
self._sidebar_sizes = self.main_splitter.sizes()
if self.wide_button.isChecked():
    self.main_splitter.setSizes([1, 0])
```

- [ ] **Step 2: Persist the preference inside `_toggle_wide_mode()`**

```python
def _toggle_wide_mode(self) -> None:
    is_wide_mode = self.wide_button.isChecked()
    if self.config is not None and self.config.player_wide_mode != is_wide_mode:
        self.config.player_wide_mode = is_wide_mode
        self._save_config()
    if is_wide_mode:
        self._remember_sidebar_sizes()
        self._apply_visibility_state()
        self.main_splitter.setSizes([1, 0])
        return
    self._apply_visibility_state()
    self.main_splitter.setSizes(self._restoreable_sidebar_sizes())
```

- [ ] **Step 3: Run the focused player-window tests to verify they pass**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_starts_in_wide_mode_when_config_requests_it tests/test_player_window_ui.py::test_player_window_toggling_wide_mode_updates_config_and_saves tests/test_player_window_ui.py::test_player_window_persists_pre_wide_splitter_state_when_saved_in_wide_mode tests/test_player_window_ui.py::test_player_window_restores_sidebar_after_toggling_wide_mode_from_fullscreen -q`

Expected: PASS with startup restore, config persistence, splitter persistence, and fullscreen regression coverage all green.

- [ ] **Step 4: Commit the player-window implementation**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: restore player wide mode on startup"
```

### Task 5: Run Broader Verification

**Files:**
- Modify: none
- Test: `tests/test_storage.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Run the relevant broader regression suite**

Run: `uv run pytest tests/test_storage.py tests/test_player_window_ui.py -q`

Expected: PASS with all storage and player-window tests green.

- [ ] **Step 2: Inspect the final diff before handoff**

Run: `git diff --stat HEAD~2..HEAD`

Expected: output limited to `src/atv_player/models.py`, `src/atv_player/storage.py`, `src/atv_player/ui/player_window.py`, `tests/test_storage.py`, and `tests/test_player_window_ui.py`.

- [ ] **Step 3: Commit any final verification-only adjustments if needed**

```bash
git add src/atv_player/models.py src/atv_player/storage.py src/atv_player/ui/player_window.py tests/test_storage.py tests/test_player_window_ui.py
git commit -m "test: verify player wide mode persistence"
```

If no post-verification adjustments were needed, skip this commit.
