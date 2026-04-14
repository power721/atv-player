# Player Controls Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Polish the player window controls so icons communicate intent and state more clearly, replay is available from the control row, tooltips expose shortcuts, the controls breathe better, and exiting fullscreen restores a previously maximized window.

**Architecture:** Keep the implementation localized to `PlayerWindow` and its focused UI tests. Reuse the existing button helper path for icon buttons, add only the narrow state needed for mute icon toggling and fullscreen restoration, and introduce SVG assets only where the current icon set cannot express the required semantics.

**Tech Stack:** Python 3.12, PySide6 widgets, pytest, pytest-qt, SVG icon assets

---

## File Structure

- `src/atv_player/ui/player_window.py`
  - Owns player window widget construction, playback control wiring, tooltip/cursor configuration, replay behavior, and fullscreen state transitions.
- `tests/test_player_window_ui.py`
  - Owns focused UI regression tests for the player window controls and fullscreen behavior.
- `src/atv_player/icons/seek-backward.svg`
  - New dedicated icon for relative backward seek.
- `src/atv_player/icons/seek-forward.svg`
  - New dedicated icon for relative forward seek.
- `src/atv_player/icons/volume-on.svg`
  - New dedicated icon for the unmuted state.
- `src/atv_player/icons/refresh.svg`
  - New dedicated icon for replaying the current item.

### Task 1: Distinguish Seek Icons From Previous/Next Episode Icons

**Files:**
- Create: `src/atv_player/icons/seek-backward.svg`
- Create: `src/atv_player/icons/seek-forward.svg`
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing test**

```python
def test_player_window_uses_distinct_seek_icons(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.prev_button.icon().cacheKey() != window.backward_button.icon().cacheKey()
    assert window.next_button.icon().cacheKey() != window.forward_button.icon().cacheKey()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_uses_distinct_seek_icons -q`
Expected: FAIL because `backward_button` and `prev_button` still use `previous.svg`, and `forward_button` and `next_button` still use `next.svg`.

- [ ] **Step 3: Write the minimal implementation**

```xml
<!-- src/atv_player/icons/seek-backward.svg -->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M11 7l-5 5 5 5" />
  <path d="M18 7l-5 5 5 5" />
</svg>
```

```xml
<!-- src/atv_player/icons/seek-forward.svg -->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M13 7l5 5-5 5" />
  <path d="M6 7l5 5-5 5" />
</svg>
```

```python
# src/atv_player/ui/player_window.py
self.prev_button = self._create_icon_button("previous.svg", "上一集")
self.next_button = self._create_icon_button("next.svg", "下一集")
self.backward_button = self._create_icon_button("seek-backward.svg", "后退")
self.forward_button = self._create_icon_button("seek-forward.svg", "前进")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_uses_distinct_seek_icons -q`
Expected: PASS with the seek buttons now loading different icon assets from the episode navigation buttons.

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py src/atv_player/icons/seek-backward.svg src/atv_player/icons/seek-forward.svg
git commit -m "feat: distinguish seek control icons"
```

### Task 2: Distinguish Muted And Unmuted Icons

**Files:**
- Create: `src/atv_player/icons/volume-on.svg`
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing test**

```python
def test_player_window_mute_button_icon_tracks_mute_state(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.toggle_mute_calls = 0

        def toggle_mute(self) -> None:
            self.toggle_mute_calls += 1

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    unmuted_icon = window.mute_button.icon().cacheKey()

    window.mute_button.click()
    muted_icon = window.mute_button.icon().cacheKey()

    window.mute_button.click()

    assert window.video.toggle_mute_calls == 2
    assert muted_icon != unmuted_icon
    assert window.mute_button.icon().cacheKey() == unmuted_icon
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_mute_button_icon_tracks_mute_state -q`
Expected: FAIL because the mute button always uses the same `volume-off.svg` icon.

- [ ] **Step 3: Write the minimal implementation**

```xml
<!-- src/atv_player/icons/volume-on.svg -->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M5 9h4l5-4v14l-5-4H5z" />
  <path d="M18 9a4 4 0 0 1 0 6" />
  <path d="M20 7a7 7 0 0 1 0 10" />
</svg>
```

```python
# src/atv_player/ui/player_window.py
self._is_muted = False
self.mute_button = self._create_icon_button("volume-on.svg", "静音")
```

```python
# src/atv_player/ui/player_window.py
def _set_button_icon(self, button: QPushButton, icon_name: str) -> None:
    button.setIcon(QIcon(str(self._icons_dir / icon_name)))


def _update_mute_button_icon(self) -> None:
    icon_name = "volume-off.svg" if self._is_muted else "volume-on.svg"
    self._set_button_icon(self.mute_button, icon_name)


def _toggle_mute(self) -> None:
    try:
        self.video.toggle_mute()
        self._is_muted = not self._is_muted
        self._update_mute_button_icon()
    except Exception as exc:
        self.details.append(f"\n静音失败: {exc}")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_mute_button_icon_tracks_mute_state -q`
Expected: PASS with the mute button switching between unmuted and muted icons on each successful toggle.

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py src/atv_player/icons/volume-on.svg
git commit -m "feat: show mute state icon"
```

### Task 3: Add A Refresh Button For Replay

**Files:**
- Create: `src/atv_player/icons/refresh.svg`
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing test**

```python
def test_player_window_refresh_button_replays_current_item(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.set_speed_calls: list[float] = []
            self.set_volume_calls: list[int] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))

        def set_speed(self, value: float) -> None:
            self.set_speed_calls.append(value)

        def set_volume(self, value: int) -> None:
            self.set_volume_calls.append(value)

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.volume_slider.setValue(35)
    window.open_session(make_player_session(start_index=1, speed=1.5))
    window.video.load_calls.clear()
    window.video.set_speed_calls.clear()
    window.video.set_volume_calls.clear()

    window.refresh_button.click()

    assert window.current_index == 1
    assert window.playlist.currentRow() == 1
    assert window.video.load_calls == [("http://m/2.m3u8", 0)]
    assert window.video.set_speed_calls == [1.5]
    assert window.video.set_volume_calls == [35]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_refresh_button_replays_current_item -q`
Expected: FAIL because `refresh_button` and replay logic do not exist yet.

- [ ] **Step 3: Write the minimal implementation**

```xml
<!-- src/atv_player/icons/refresh.svg -->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M21 12a9 9 0 1 1-2.64-6.36" />
  <path d="M21 3v6h-6" />
</svg>
```

```python
# src/atv_player/ui/player_window.py
self.refresh_button = self._create_icon_button("refresh.svg", "重新播放")
```

```python
# src/atv_player/ui/player_window.py
control_group_layout.addWidget(self.backward_button)
control_group_layout.addWidget(self.forward_button)
control_group_layout.addWidget(self.refresh_button)
control_group_layout.addWidget(self.wide_button)
```

```python
# src/atv_player/ui/player_window.py
self.refresh_button.clicked.connect(self._replay_current_item)
```

```python
# src/atv_player/ui/player_window.py
def _replay_current_item(self) -> None:
    if self.session is None:
        return
    self.is_playing = True
    self._update_play_button_icon()
    self.playlist.setCurrentRow(self.current_index)
    self._load_current_item(start_position_seconds=0)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_refresh_button_replays_current_item -q`
Expected: PASS with the refresh button reloading the current playlist item from the beginning while preserving row selection, speed, and volume.

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py src/atv_player/icons/refresh.svg
git commit -m "feat: add replay control button"
```

### Task 4: Add Shortcut Tooltips And Pointing-Hand Cursor To Playback Controls

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing test**

```python
def test_player_window_playback_controls_show_shortcuts_and_pointing_cursor(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.play_button.toolTip() == "播放/暂停 (Space)"
    assert window.prev_button.toolTip() == "上一集 (PgUp)"
    assert window.next_button.toolTip() == "下一集 (PgDn)"
    assert window.backward_button.toolTip() == "后退 (Left)"
    assert window.forward_button.toolTip() == "前进 (Right)"
    assert window.mute_button.toolTip() == "静音 (M)"
    assert window.fullscreen_button.toolTip() == "全屏 (Enter)"
    assert window.play_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert window.refresh_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert window.fullscreen_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_playback_controls_show_shortcuts_and_pointing_cursor -q`
Expected: FAIL because the button tooltips are action-only labels and the icon button helper does not set a pointing-hand cursor.

- [ ] **Step 3: Write the minimal implementation**

```python
# src/atv_player/ui/player_window.py
def _format_tooltip(self, label: str, shortcut: str | None = None) -> str:
    if shortcut is None:
        return label
    return f"{label} ({shortcut})"


def _create_icon_button(self, icon_name: str, tooltip: str, shortcut: str | None = None) -> QPushButton:
    button = QPushButton("")
    button.setToolTip(self._format_tooltip(tooltip, shortcut))
    button.setIcon(QIcon(str(self._icons_dir / icon_name)))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFixedHeight(28)
    return button
```

```python
# src/atv_player/ui/player_window.py
self.play_button = self._create_icon_button("play.svg", "播放/暂停", "Space")
self.prev_button = self._create_icon_button("previous.svg", "上一集", "PgUp")
self.next_button = self._create_icon_button("next.svg", "下一集", "PgDn")
self.backward_button = self._create_icon_button("seek-backward.svg", "后退", "Left")
self.forward_button = self._create_icon_button("seek-forward.svg", "前进", "Right")
self.mute_button = self._create_icon_button("volume-on.svg", "静音", "M")
self.refresh_button = self._create_icon_button("refresh.svg", "重新播放")
self.fullscreen_button = self._create_icon_button("maximize.svg", "全屏", "Enter")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_playback_controls_show_shortcuts_and_pointing_cursor -q`
Expected: PASS with shortcut text visible on the covered controls and a pointing-hand cursor on hover.

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "feat: add control shortcut tooltips"
```

### Task 5: Add Padding Around The Playback Control Area

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing test**

```python
def test_player_window_adds_padding_around_bottom_controls(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    margins = window.bottom_layout.contentsMargins()

    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (12, 6, 12, 6)
    assert window.bottom_area.maximumHeight() == 72
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_adds_padding_around_bottom_controls -q`
Expected: FAIL because the bottom layout margins are still zero and `bottom_area.maximumHeight()` is still `60`.

- [ ] **Step 3: Write the minimal implementation**

```python
# src/atv_player/ui/player_window.py
self.bottom_area.setMaximumHeight(72)
bottom_layout = QVBoxLayout(self.bottom_area)
self.bottom_layout = bottom_layout
bottom_layout.setContentsMargins(12, 6, 12, 6)
bottom_layout.setSpacing(4)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_adds_padding_around_bottom_controls -q`
Expected: PASS with the bottom controls no longer flush against the container edges.

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "feat: add player controls padding"
```

### Task 6: Preserve Maximized Window State When Exiting Fullscreen

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing test**

```python
def test_player_window_exit_fullscreen_restores_maximized_state(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.showMaximized()
    qtbot.waitUntil(window.isMaximized)

    window.toggle_fullscreen()
    assert window.isFullScreen() is True

    window.toggle_fullscreen()

    assert window.isFullScreen() is False
    assert window.isMaximized() is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_exit_fullscreen_restores_maximized_state -q`
Expected: FAIL because the current fullscreen exit path always calls `showNormal()`.

- [ ] **Step 3: Write the minimal implementation**

```python
# src/atv_player/ui/player_window.py
self._was_maximized_before_fullscreen = False
```

```python
# src/atv_player/ui/player_window.py
def toggle_fullscreen(self) -> None:
    if self.isFullScreen():
        if self._was_maximized_before_fullscreen:
            self.showMaximized()
        else:
            self.showNormal()
        self._apply_visibility_state()
        return
    self._was_maximized_before_fullscreen = self.isMaximized()
    self.showFullScreen()
    self._apply_visibility_state()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_exit_fullscreen_restores_maximized_state -q`
Expected: PASS with fullscreen exit restoring the window to maximized when that was the pre-fullscreen state.

- [ ] **Step 5: Run the focused regression suite and commit**

Run: `uv run pytest tests/test_player_window_ui.py -q`
Expected: PASS with the new fullscreen restoration coverage and no regressions in the existing player control tests.

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "fix: restore maximized window after fullscreen"
```
