# Paused Player Restore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve paused playback state across app restart so restoring the last player session resumes the same item and position without auto-playing when the prior session was paused.

**Architecture:** Keep the feature local to desktop state. Extend `AppConfig` and `SettingsRepository` with an explicit `last_player_paused` flag, thread that state through `MainWindow.open_player()` and `PlayerWindow.open_session()`, and prove behavior with focused storage, main-window, and player-window tests written before implementation.

**Tech Stack:** Python 3.14, PySide6, SQLite, pytest, pytest-qt

---

## File Structure

- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/storage.py`
- Modify: `src/atv_player/ui/main_window.py`
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_storage.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_player_window_ui.py`

## Task 1: Lock In Persisted Paused State In Config Storage

**Files:**
- Modify: `tests/test_storage.py`
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing storage tests**

```python
def test_settings_repository_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        last_path="/Movies",
        last_active_window="player",
        last_playback_mode="folder",
        last_playback_path="/Movies",
        last_playback_vod_id="vod-1",
        last_playback_clicked_vod_id="vod-2",
        last_player_paused=True,
        main_window_geometry=None,
        player_window_geometry=None,
        player_main_splitter_state=b"split-main",
    )

    repo.save_config(config)

    assert repo.load_config() == config


def test_settings_repository_migrates_missing_last_player_paused_column(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            '''
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
                main_window_geometry BLOB,
                player_window_geometry BLOB,
                player_main_splitter_state BLOB
            )
            '''
        )
        conn.execute(
            """
            INSERT INTO app_config (
                id, base_url, username, token, vod_token, last_path,
                last_active_window, last_playback_mode, last_playback_path,
                last_playback_vod_id, last_playback_clicked_vod_id,
                main_window_geometry, player_window_geometry, player_main_splitter_state
            ) VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/TV', 'player', 'detail', '/TV', 'vod-1', 'vod-1', NULL, NULL, NULL)
            """
        )

    repo = SettingsRepository(db_path)
    saved = repo.load_config()

    assert saved.last_player_paused is False
```

- [ ] **Step 2: Run the storage tests to verify they fail**

Run: `uv run pytest tests/test_storage.py -q`
Expected: FAIL because `AppConfig` and `SettingsRepository` do not define or persist `last_player_paused`.

- [ ] **Step 3: Add the minimal model and repository implementation**

```python
@dataclass(slots=True)
class AppConfig:
    base_url: str = "http://127.0.0.1:4567"
    username: str = ""
    token: str = ""
    vod_token: str = ""
    last_path: str = "/"
    last_active_window: str = "main"
    last_playback_mode: str = ""
    last_playback_path: str = ""
    last_playback_vod_id: str = ""
    last_playback_clicked_vod_id: str = ""
    last_player_paused: bool = False
    main_window_geometry: bytes | None = None
    player_window_geometry: bytes | None = None
    player_main_splitter_state: bytes | None = None
```

```python
CREATE TABLE IF NOT EXISTS app_config (
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
    main_window_geometry BLOB,
    player_window_geometry BLOB,
    player_main_splitter_state BLOB
)
```

```python
if "last_player_paused" not in columns:
    conn.execute(
        "ALTER TABLE app_config ADD COLUMN last_player_paused INTEGER NOT NULL DEFAULT 0"
    )
```

```python
SELECT
    base_url,
    username,
    token,
    vod_token,
    last_path,
    last_active_window,
    last_playback_mode,
    last_playback_path,
    last_playback_vod_id,
    last_playback_clicked_vod_id,
    last_player_paused,
    main_window_geometry,
    player_window_geometry,
    player_main_splitter_state
FROM app_config
WHERE id = 1
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
    last_playback_mode = ?,
    last_playback_path = ?,
    last_playback_vod_id = ?,
    last_playback_clicked_vod_id = ?,
    last_player_paused = ?,
    main_window_geometry = ?,
    player_window_geometry = ?,
    player_main_splitter_state = ?
WHERE id = 1
```

- [ ] **Step 4: Run the storage tests to verify they pass**

Run: `uv run pytest tests/test_storage.py -q`
Expected: PASS

- [ ] **Step 5: Commit the storage slice**

```bash
git add tests/test_storage.py src/atv_player/models.py src/atv_player/storage.py docs/superpowers/plans/2026-04-14-paused-player-restore.md
git commit -m "feat: persist paused player restore state"
```

## Task 2: Teach PlayerWindow To Open And Persist Paused Sessions

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing player window tests**

```python
def test_player_window_can_open_session_paused(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=1), start_paused=True)

    assert window.is_playing is False
    assert window.video.load_calls == [("http://m/2.m3u8", True, 0)]
    assert window.play_button.toolTip() == "播放/暂停 (Space)"


def test_player_window_toggle_playback_persists_last_player_paused(qtbot) -> None:
    config = AppConfig(last_player_paused=False)
    saved = {"count": 0}
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: saved.__setitem__("count", saved["count"] + 1))
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.toggle_playback()
    assert config.last_player_paused is True

    window.toggle_playback()
    assert config.last_player_paused is False
    assert saved["count"] >= 2


def test_player_window_return_to_main_persists_paused_restore_state(qtbot) -> None:
    config = AppConfig(last_active_window="player", last_player_paused=False)
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)

    class FakeVideo:
        def pause(self) -> None:
            return None

    window.session = object()
    window.video = FakeVideo()
    window._return_to_main()

    assert config.last_player_paused is True


def test_player_window_quit_application_preserves_current_paused_state(qtbot, monkeypatch) -> None:
    config = AppConfig(last_active_window="player", last_player_paused=False)
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.is_playing = False

    monkeypatch.setattr(QApplication, "quit", lambda *args, **kwargs: None)

    window._quit_application()

    assert config.last_player_paused is True
```

- [ ] **Step 2: Run the focused player window tests to verify they fail**

Run: `uv run pytest tests/test_player_window_ui.py -q`
Expected: FAIL because `open_session()` does not accept `start_paused`, load calls always use `pause=False`, and playback state is not persisted into config.

- [ ] **Step 3: Add the minimal PlayerWindow implementation**

```python
def open_session(self, session, start_paused: bool = False) -> None:
    self.session = session
    self.current_index = session.start_index
    self.current_speed = session.speed
    speed_text = self._speed_text(session.speed)
    speed_index = self.speed_combo.findText(speed_text)
    if speed_index >= 0:
        self.speed_combo.setCurrentIndex(speed_index)
    self.is_playing = not start_paused
    self._set_last_player_paused(start_paused)
    self._update_play_button_icon()
    self.playlist.clear()
    for item in session.playlist:
        self.playlist.addItem(QListWidgetItem(item.title))
    self.playlist.setCurrentRow(self.current_index)
    self.progress.setValue(0)
    self._load_current_item(session.start_position_seconds, pause=start_paused)
    self.report_timer.start()
    self.progress_timer.start()
```

```python
def _load_current_item(self, start_position_seconds: int = 0, pause: bool = False) -> None:
    if self.session is None:
        return
    current_item = self.session.playlist[self.current_index]
    self.details.setPlainText(
        f"标题: {self.session.vod.vod_name}\n"
        f"当前: {current_item.title}\n"
        f"URL: {current_item.url}"
    )
    try:
        self.video.load(current_item.url, pause=pause, start_seconds=start_position_seconds)
        self.video.set_speed(self.current_speed)
        self.video.set_volume(self.volume_slider.value())
    except Exception as exc:
        self.details.append(f"\n播放失败: {exc}")
```

```python
def _set_last_player_paused(self, paused: bool) -> None:
    if self.config is None:
        return
    self.config.last_player_paused = paused
    self._save_config()
```

```python
def toggle_playback(self) -> None:
    if self.is_playing:
        self.video.pause()
    else:
        self.video.resume()
    self.is_playing = not self.is_playing
    self._set_last_player_paused(not self.is_playing)
    self._update_play_button_icon()
```

```python
def _quit_application(self) -> None:
    self._quit_requested = True
    if self.config is not None:
        self.config.last_active_window = "player"
    self._set_last_player_paused(not self.is_playing)
    self._persist_geometry()
    app = QApplication.instance()
    if app is not None:
        app.quit()


def _return_to_main(self) -> None:
    try:
        self.video.pause()
    except Exception:
        pass
    self.is_playing = False
    self._set_last_player_paused(True)
    self._update_play_button_icon()
    if self.config is not None:
        self.config.last_active_window = "main"
    self._persist_geometry()
    self.hide()
    self.closed_to_main.emit()
```

- [ ] **Step 4: Run the focused player window tests to verify they pass**

Run: `uv run pytest tests/test_player_window_ui.py -q`
Expected: PASS

- [ ] **Step 5: Commit the player window slice**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py docs/superpowers/plans/2026-04-14-paused-player-restore.md
git commit -m "feat: restore paused state in player window"
```

## Task 3: Thread Paused Restore Through MainWindow Restore Flow

**Files:**
- Modify: `tests/test_app.py`
- Modify: `src/atv_player/ui/main_window.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing restore-flow tests**

```python
def test_main_window_open_player_starts_new_sessions_playing(qtbot, monkeypatch) -> None:
    class FakePlayerWindow:
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

    monkeypatch.setattr(main_window_module, "PlayerWindow", FakePlayerWindow)
    config = AppConfig(last_player_paused=True)
    window = MainWindow(
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    request = OpenPlayerRequest(
        vod=VodItem(vod_id="vod-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
        clicked_index=0,
        source_mode="detail",
        source_vod_id="vod-1",
    )

    window.open_player(request)

    assert window.player_window.opened == [({"vod": request.vod, "playlist": request.playlist, "clicked_index": 0}, False)]
    assert config.last_player_paused is False


def test_main_window_restore_last_player_opens_paused_from_config(qtbot, monkeypatch) -> None:
    class RestoreBrowseController:
        def build_request_from_detail(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="Movie"),
                playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
                clicked_index=0,
                source_mode="detail",
                source_vod_id=vod_id,
            )

    class RecordingPlayerWindow:
        closed_to_main = type("SignalStub", (), {"connect": lambda self, cb: None})()

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

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
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
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    assert window.player_window.opened[0][1] is True
```

- [ ] **Step 2: Run the restore-flow tests to verify they fail**

Run: `uv run pytest tests/test_app.py -q`
Expected: FAIL because `open_player()` always calls `open_session(session)` without a paused-mode parameter and restores cannot consume `config.last_player_paused`.

- [ ] **Step 3: Add the minimal MainWindow implementation**

```python
def open_player(self, request, restore_paused_state: bool = False) -> None:
    session = self.player_controller.create_session(
        request.vod,
        request.playlist,
        request.clicked_index,
    )
    if self.player_window is None:
        self.player_window = PlayerWindow(self.player_controller, self.config, self._save_config)
        if hasattr(self.player_window, "closed_to_main"):
            self.player_window.closed_to_main.connect(self._show_main_again)
    self.config.last_active_window = "player"
    self.config.last_playback_mode = request.source_mode
    self.config.last_playback_path = request.source_path
    self.config.last_playback_vod_id = request.source_vod_id
    self.config.last_playback_clicked_vod_id = request.source_clicked_vod_id
    start_paused = self.config.last_player_paused if restore_paused_state else False
    if not restore_paused_state:
        self.config.last_player_paused = False
    self.config.main_window_geometry = bytes(self.saveGeometry())
    self._save_config()
    self.player_window.open_session(session, start_paused=start_paused)
    self.player_window.show()
    self.player_window.raise_()
    self.player_window.activateWindow()
    self.hide()
```

```python
def restore_last_player(self):
    mode = self.config.last_playback_mode
    if mode == "detail" and self.config.last_playback_vod_id:
        request = self.browse_controller.build_request_from_detail(self.config.last_playback_vod_id)
    elif mode == "folder" and self.config.last_playback_path and self.config.last_playback_clicked_vod_id:
        items, _ = self.browse_controller.load_folder(self.config.last_playback_path)
        clicked = next((item for item in items if item.vod_id == self.config.last_playback_clicked_vod_id), None)
        if clicked is None:
            return None
        request = self.browse_controller.build_request_from_folder_item(clicked, items)
    else:
        return None
    self.open_player(request, restore_paused_state=True)
    return self.player_window
```

- [ ] **Step 4: Run the restore-flow tests to verify they pass**

Run: `uv run pytest tests/test_app.py -q`
Expected: PASS

- [ ] **Step 5: Commit the restore-flow slice**

```bash
git add tests/test_app.py src/atv_player/ui/main_window.py docs/superpowers/plans/2026-04-14-paused-player-restore.md
git commit -m "feat: restore paused playback state on reopen"
```

## Task 4: Run Focused Verification And Finalize

**Files:**
- Test: `tests/test_storage.py`
- Test: `tests/test_player_window_ui.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Run the full focused verification set**

Run: `uv run pytest tests/test_storage.py tests/test_player_window_ui.py tests/test_app.py -q`
Expected: PASS with storage, restore-flow, and player-window coverage all green.

- [ ] **Step 2: Review the changed behavior manually in code**

```python
# Confirm these end states in the final diff:
# - AppConfig includes last_player_paused with default False
# - SettingsRepository migrates and persists last_player_paused
# - MainWindow.open_player(..., restore_paused_state=False) only restores pause state when asked
# - PlayerWindow.open_session(..., start_paused=False) passes pause through to video.load(...)
# - toggle_playback(), _return_to_main(), and _quit_application() keep config.last_player_paused in sync
```

- [ ] **Step 3: Commit the verified result**

```bash
git add src/atv_player/models.py src/atv_player/storage.py src/atv_player/ui/main_window.py src/atv_player/ui/player_window.py tests/test_storage.py tests/test_app.py tests/test_player_window_ui.py docs/superpowers/plans/2026-04-14-paused-player-restore.md
git commit -m "feat: preserve paused state when restoring player"
```
