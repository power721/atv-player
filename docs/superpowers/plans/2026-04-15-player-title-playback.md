# Player Title Playback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the player window title show the current video title while playback is active and revert to `alist-tvbox 播放器` when playback is paused.

**Architecture:** Keep title ownership inside `PlayerWindow` because the title depends only on Qt window state, `self.is_playing`, `self.session`, and `self.current_index`. Add one internal helper that derives the active playback title safely and call it from session-open, play/pause, playlist-item changes, and return-to-main flows.

**Tech Stack:** Python, PySide6, pytest-qt

---

## File Map

- Modify: `src/atv_player/ui/player_window.py`
  - Owns the player window title, playback toggles, playlist navigation, and return-to-main flow.
- Modify: `tests/test_player_window_ui.py`
  - Owns the Qt UI regression coverage for player window playback behavior.

### Task 1: Add title updates for open-session and play/pause

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing tests**

Add these tests near the existing `open_session` and `toggle_playback` coverage in `tests/test_player_window_ui.py`:

```python
def test_player_window_shows_video_title_while_playing(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 2", url="http://m/2.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.windowTitle() == "Movie - Episode 2"


def test_player_window_pausing_playback_restores_application_title(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1))

    window.toggle_playback()

    assert window.is_playing is False
    assert window.windowTitle() == "alist-tvbox 播放器"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_shows_video_title_while_playing tests/test_player_window_ui.py::test_player_window_pausing_playback_restores_application_title -q`

Expected: FAIL because `PlayerWindow` still keeps the fixed title `alist-tvbox 播放器`.

- [ ] **Step 3: Write the minimal implementation**

In `src/atv_player/ui/player_window.py`, add small title helpers near `_update_play_button_icon()` and route existing state changes through them:

```python
    def _default_window_title(self) -> str:
        return "alist-tvbox 播放器"

    def _active_playback_title(self) -> str:
        if self.session is None or not self.session.playlist:
            return self._default_window_title()
        current_item = self.session.playlist[self.current_index]
        parts = [self.session.vod.vod_name.strip(), current_item.title.strip()]
        parts = [part for part in parts if part]
        if not parts:
            return self._default_window_title()
        return " - ".join(parts)

    def _refresh_window_title(self) -> None:
        if not self.is_playing:
            self.setWindowTitle(self._default_window_title())
            return
        self.setWindowTitle(self._active_playback_title())
```

Update the constructor and state transitions to use the helper:

```python
        self.setWindowTitle(self._default_window_title())
```

```python
        self._update_play_button_icon()
        self._refresh_window_title()
```

Add the refresh call in both `open_session()` and `toggle_playback()` immediately after `self.is_playing` is set and the play button state is updated.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_shows_video_title_while_playing tests/test_player_window_ui.py::test_player_window_pausing_playback_restores_application_title -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "feat: update player title during playback"
```

### Task 2: Keep the title correct for paused-open, episode changes, and return-to-main

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_player_window_ui.py` near the existing paused-open and playlist navigation coverage:

```python
def test_player_window_opening_session_paused_keeps_application_title(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(make_player_session(start_index=1), start_paused=True)

    assert window.is_playing is False
    assert window.windowTitle() == "alist-tvbox 播放器"


def test_player_window_play_next_updates_window_title_to_new_item(qtbot) -> None:
    controller = RecordingPlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=0))

    window.play_next()

    assert window.current_index == 1
    assert window.windowTitle() == "Movie - Episode 2"


def test_player_window_return_to_main_restores_application_title(qtbot) -> None:
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=0))

    window._return_to_main()

    assert window.windowTitle() == "alist-tvbox 播放器"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_opening_session_paused_keeps_application_title tests/test_player_window_ui.py::test_player_window_play_next_updates_window_title_to_new_item tests/test_player_window_ui.py::test_player_window_return_to_main_restores_application_title -q`

Expected: FAIL because paused opens and playlist changes do not refresh the title, and returning to main does not explicitly restore the application title.

- [ ] **Step 3: Write the minimal implementation**

Refresh the title anywhere the effective playback title changes in `src/atv_player/ui/player_window.py`:

```python
    def _play_item_at_index(self, index: int, start_position_seconds: int = 0, pause: bool = False) -> None:
        if self.session is None:
            return
        previous_index = self.current_index
        self.current_index = index
        try:
            self.playlist.setCurrentRow(self.current_index)
            self._load_current_item(start_position_seconds=start_position_seconds, pause=pause)
            self._refresh_window_title()
        except Exception:
            self.current_index = previous_index
            self.playlist.setCurrentRow(previous_index)
            self._refresh_window_title()
            raise
```

Restore the default title when leaving active playback:

```python
    def _return_to_main(self) -> None:
        try:
            self.video.pause()
        except Exception:
            pass
        self.is_playing = False
        self._refresh_window_title()
        self._restore_video_cursor()
        self._set_last_player_paused(True)
        self._update_play_button_icon()
        ...
```

Keep `open_session(..., start_paused=True)` on the default title by relying on the Task 1 helper after `self.is_playing = not start_paused`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_opening_session_paused_keeps_application_title tests/test_player_window_ui.py::test_player_window_play_next_updates_window_title_to_new_item tests/test_player_window_ui.py::test_player_window_return_to_main_restores_application_title -q`

Expected: PASS

- [ ] **Step 5: Run the focused regression slice**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_can_open_session_paused tests/test_player_window_ui.py::test_player_window_keyboard_shortcuts_control_playback_navigation_and_view tests/test_player_window_ui.py::test_player_window_return_to_main_hides_window_without_closing_session tests/test_player_window_ui.py::test_player_window_shows_video_title_while_playing tests/test_player_window_ui.py::test_player_window_pausing_playback_restores_application_title tests/test_player_window_ui.py::test_player_window_opening_session_paused_keeps_application_title tests/test_player_window_ui.py::test_player_window_play_next_updates_window_title_to_new_item tests/test_player_window_ui.py::test_player_window_return_to_main_restores_application_title -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "test: cover player title playback state"
```

## Self-Review

- Spec coverage: title source, pause fallback, open-session, episode changes, and return-to-main all map to Task 1 or Task 2. No controller or persistence work was added.
- Placeholder scan: no `TBD`, `TODO`, or vague test instructions remain; every code-changing step includes concrete code or commands.
- Type consistency: the plan uses only existing `PlayerWindow`, `PlayerSession`, `VodItem`, `PlayItem`, `open_session()`, `toggle_playback()`, `play_next()`, and `_return_to_main()` names already present in the codebase.
