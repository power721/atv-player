# Player Cursor Autohide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide the mouse cursor after a 3 second delay while video is playing and the pointer stays inside the video area, then restore it immediately on movement, pause, or leave.

**Architecture:** Keep the change localized to `PlayerWindow`. Use a single-shot `QTimer` plus a video-widget event filter to manage cursor visibility without pushing playback-state logic into `MpvWidget`.

**Tech Stack:** Python 3.12, PySide6 widgets, pytest, pytest-qt

---

## File Structure

- `src/atv_player/ui/player_window.py`
  - Owns cursor autohide state, timer wiring, event filtering, and playback-state integration.
- `tests/test_player_window_ui.py`
  - Owns focused UI regression tests for delayed cursor hiding in the video area.

### Task 1: Add Cursor Autohide UI Tests

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_player_window_mouse_move_in_video_restarts_cursor_autohide_when_playing(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.is_playing = True
    window._video_pointer_inside = True
    window._set_video_cursor_hidden(True)

    window._handle_video_mouse_activity()

    assert window.video.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert window._cursor_hide_timer.isActive() is True


def test_player_window_cursor_hide_timer_hides_video_cursor_only_while_playing_and_inside(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.is_playing = True
    window._video_pointer_inside = True

    window._hide_video_cursor_if_idle()

    assert window.video.cursor().shape() == Qt.CursorShape.BlankCursor
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_mouse_move_in_video_restarts_cursor_autohide_when_playing tests/test_player_window_ui.py::test_player_window_cursor_hide_timer_hides_video_cursor_only_while_playing_and_inside -q`
Expected: FAIL because `PlayerWindow` does not yet expose cursor autohide helpers or timer state.

- [ ] **Step 3: Write the minimal implementation**

```python
self.video.setMouseTracking(True)
self.video.installEventFilter(self)
self._cursor_hide_timer = QTimer(self)
self._cursor_hide_timer.setSingleShot(True)
self._cursor_hide_timer.timeout.connect(self._hide_video_cursor_if_idle)
self._video_pointer_inside = False
```

```python
def _set_video_cursor_hidden(self, hidden: bool) -> None:
    self.video.setCursor(Qt.CursorShape.BlankCursor if hidden else Qt.CursorShape.ArrowCursor)


def _handle_video_mouse_activity(self) -> None:
    self._set_video_cursor_hidden(False)
    if self.is_playing and self._video_pointer_inside:
        self._cursor_hide_timer.start(1500)
    else:
        self._cursor_hide_timer.stop()


def _hide_video_cursor_if_idle(self) -> None:
    if self.is_playing and self._video_pointer_inside:
        self._set_video_cursor_hidden(True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_mouse_move_in_video_restarts_cursor_autohide_when_playing tests/test_player_window_ui.py::test_player_window_cursor_hide_timer_hides_video_cursor_only_while_playing_and_inside -q`
Expected: PASS with mouse activity showing the cursor and the idle callback hiding it again.

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "feat: auto-hide cursor over playing video"
```

### Task 2: Restore Cursor On Pause And Leave

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_player_window_pausing_playback_restores_video_cursor_and_stops_autohide(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.is_playing = True
    window._video_pointer_inside = True
    window._cursor_hide_timer.start(1500)
    window._set_video_cursor_hidden(True)

    window.toggle_playback()

    assert window.is_playing is False
    assert window.video.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert window._cursor_hide_timer.isActive() is False


def test_player_window_video_leave_restores_cursor_and_stops_autohide(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.is_playing = True
    window._video_pointer_inside = True
    window._cursor_hide_timer.start(1500)
    window._set_video_cursor_hidden(True)

    window._handle_video_leave()

    assert window.video.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert window._cursor_hide_timer.isActive() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_pausing_playback_restores_video_cursor_and_stops_autohide tests/test_player_window_ui.py::test_player_window_video_leave_restores_cursor_and_stops_autohide -q`
Expected: FAIL because pause and leave do not currently reset cursor autohide state.

- [ ] **Step 3: Write the minimal implementation**

```python
def _restore_video_cursor(self) -> None:
    self._cursor_hide_timer.stop()
    self._set_video_cursor_hidden(False)


def _handle_video_leave(self) -> None:
    self._video_pointer_inside = False
    self._restore_video_cursor()
```

```python
def toggle_playback(self) -> None:
    if self.is_playing:
        self.video.pause()
    else:
        self.video.resume()
    self.is_playing = not self.is_playing
    if not self.is_playing:
        self._restore_video_cursor()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_pausing_playback_restores_video_cursor_and_stops_autohide tests/test_player_window_ui.py::test_player_window_video_leave_restores_cursor_and_stops_autohide -q`
Expected: PASS with pause and video leave restoring a visible cursor immediately.

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "feat: restore video cursor on pause and leave"
```
