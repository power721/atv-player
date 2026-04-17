# Player Context Menu Subtitle Size Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add primary and secondary subtitle size controls to the existing video right-click menu, with independent size state, session-level carry-forward, and capability-based degradation.

**Architecture:** Keep subtitle-size property access inside `MpvWidget`, where mpv-specific property names and support detection already belong. Extend `PlayerWindow` with two new size submenus, session-level size state, menu enablement logic, and cross-episode reapply that mirrors the existing subtitle position flow.

**Tech Stack:** Python 3.12, PySide6 widgets, pytest, pytest-qt, python-mpv

---

## File Structure

- Modify: `src/atv_player/player/mpv_widget.py`
  - Add primary and secondary subtitle size read/write helpers plus support detection methods.
- Modify: `src/atv_player/ui/player_window.py`
  - Add size submenu construction, size state, menu action handlers, support flags, and episode-to-episode reapply logic.
- Modify: `tests/test_mpv_widget.py`
  - Add focused unit tests for subtitle size read/write and missing-property detection.
- Modify: `tests/test_player_window_ui.py`
  - Add focused UI tests for menu structure, size actions, clamping, session reuse, disablement, and failure logging.

### Task 1: Add Failing mpv Subtitle Size Tests

**Files:**
- Modify: `tests/test_mpv_widget.py`
- Test: `tests/test_mpv_widget.py`

- [ ] **Step 1: Write the failing subtitle-size tests**

Add these tests after the existing secondary subtitle position support test in `tests/test_mpv_widget.py`:

```python
def test_mpv_widget_reads_and_writes_primary_subtitle_scale(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"sub-scale": 1.0}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.subtitle_scale() == 100

    widget.set_subtitle_scale(115)

    assert widget.subtitle_scale() == 115


def test_mpv_widget_reads_and_writes_secondary_subtitle_scale(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"secondary-sub-scale": 1.0}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.secondary_subtitle_scale() == 100

    widget.set_secondary_subtitle_scale(130)

    assert widget.secondary_subtitle_scale() == 130


def test_mpv_widget_reports_primary_subtitle_scale_unsupported_when_property_is_missing(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __getitem__(self, key: str) -> object:
            raise RuntimeError(("mpv property does not exist", -8, (object(), b"options/sub-scale", b"1.0")))

    widget._player = FakePlayer()

    assert widget.supports_subtitle_scale() is False


def test_mpv_widget_reports_secondary_subtitle_scale_unsupported_when_property_is_missing(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __getitem__(self, key: str) -> object:
            raise RuntimeError(("mpv property does not exist", -8, (object(), b"options/secondary-sub-scale", b"1.0")))

    widget._player = FakePlayer()

    assert widget.supports_secondary_subtitle_scale() is False
```

- [ ] **Step 2: Run the focused mpv tests to verify they fail**

Run:

```bash
uv run pytest \
  tests/test_mpv_widget.py::test_mpv_widget_reads_and_writes_primary_subtitle_scale \
  tests/test_mpv_widget.py::test_mpv_widget_reads_and_writes_secondary_subtitle_scale \
  tests/test_mpv_widget.py::test_mpv_widget_reports_primary_subtitle_scale_unsupported_when_property_is_missing \
  tests/test_mpv_widget.py::test_mpv_widget_reports_secondary_subtitle_scale_unsupported_when_property_is_missing \
  -q
```

Expected: FAIL because `subtitle_scale()`, `set_subtitle_scale()`, `secondary_subtitle_scale()`, `set_secondary_subtitle_scale()`, `supports_subtitle_scale()`, and `supports_secondary_subtitle_scale()` do not exist yet.

- [ ] **Step 3: Commit the failing mpv tests**

```bash
git add tests/test_mpv_widget.py
git commit -m "test: cover mpv subtitle size controls"
```

### Task 2: Implement mpv Subtitle Size Primitives

**Files:**
- Modify: `src/atv_player/player/mpv_widget.py`
- Test: `tests/test_mpv_widget.py`

- [ ] **Step 1: Add primary subtitle size read/write helpers**

Add these methods below `set_secondary_subtitle_position()` in `src/atv_player/player/mpv_widget.py`:

```python
    def subtitle_scale(self) -> int:
        value = self._player_property("sub-scale", 1.0)
        try:
            return int(round(float(value) * 100))
        except (TypeError, ValueError):
            return 100

    def set_subtitle_scale(self, value: int) -> None:
        clamped = max(50, min(int(value), 200))
        self._set_player_property("sub-scale", clamped / 100)
```

- [ ] **Step 2: Add secondary subtitle size read/write helpers**

Add these methods below `set_subtitle_scale()`:

```python
    def secondary_subtitle_scale(self) -> int:
        value = self._player_property("secondary-sub-scale", 1.0)
        try:
            return int(round(float(value) * 100))
        except (TypeError, ValueError):
            return 100

    def set_secondary_subtitle_scale(self, value: int) -> None:
        clamped = max(50, min(int(value), 200))
        self._set_player_property("secondary-sub-scale", clamped / 100)
```

- [ ] **Step 3: Add subtitle size support detection**

Add these methods below `set_secondary_subtitle_scale()`:

```python
    def supports_subtitle_scale(self) -> bool:
        if self._player is None:
            return False
        try:
            _ = self._player["sub-scale"]
            return True
        except Exception as exc:
            if self._is_missing_mpv_property_error(exc):
                return False
            if getattr(self._player, "core_shutdown", False):
                return False
            raise

    def supports_secondary_subtitle_scale(self) -> bool:
        if self._player is None:
            return False
        try:
            _ = self._player["secondary-sub-scale"]
            return True
        except Exception as exc:
            if self._is_missing_mpv_property_error(exc):
                return False
            if getattr(self._player, "core_shutdown", False):
                return False
            raise
```

- [ ] **Step 4: Run the focused mpv tests to verify they pass**

Run:

```bash
uv run pytest \
  tests/test_mpv_widget.py::test_mpv_widget_reads_and_writes_primary_subtitle_scale \
  tests/test_mpv_widget.py::test_mpv_widget_reads_and_writes_secondary_subtitle_scale \
  tests/test_mpv_widget.py::test_mpv_widget_reports_primary_subtitle_scale_unsupported_when_property_is_missing \
  tests/test_mpv_widget.py::test_mpv_widget_reports_secondary_subtitle_scale_unsupported_when_property_is_missing \
  -q
```

Expected: PASS.

- [ ] **Step 5: Run the full mpv widget suite**

Run:

```bash
uv run pytest tests/test_mpv_widget.py -q
```

Expected: PASS for the full file.

- [ ] **Step 6: Commit the mpv subtitle size implementation**

```bash
git add src/atv_player/player/mpv_widget.py tests/test_mpv_widget.py
git commit -m "feat: add mpv subtitle size primitives"
```

### Task 3: Add Failing Player Window Tests For Size Menus And Actions

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing size-menu tests**

Add these tests after the existing context-menu position tests in `tests/test_player_window_ui.py`:

```python
def test_player_window_context_menu_includes_primary_and_secondary_subtitle_size_submenus(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def supports_subtitle_scale(self) -> bool:
            return True

        def subtitle_scale(self) -> int:
            return 100

        def set_subtitle_scale(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_scale(self) -> bool:
            return True

        def secondary_subtitle_scale(self) -> int:
            return 100

        def set_secondary_subtitle_scale(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()

    assert [action.text() for action in menu.actions()] == [
        "主字幕",
        "次字幕",
        "主字幕位置",
        "次字幕位置",
        "主字幕大小",
        "次字幕大小",
        "音轨",
    ]
    assert [action.text() for action in _submenu_actions(menu, "主字幕大小")] == [
        "很小",
        "小",
        "默认",
        "大",
        "很大",
        "",
        "缩小 5%",
        "放大 5%",
        "重置",
    ]


def test_player_window_context_menu_size_actions_update_video_layer(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.subtitle_scale_value = 100
            self.secondary_subtitle_scale_value = 100

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def supports_subtitle_scale(self) -> bool:
            return True

        def subtitle_scale(self) -> int:
            return self.subtitle_scale_value

        def set_subtitle_scale(self, value: int) -> None:
            self.subtitle_scale_value = value

        def supports_secondary_subtitle_scale(self) -> bool:
            return True

        def secondary_subtitle_scale(self) -> int:
            return self.secondary_subtitle_scale_value

        def set_secondary_subtitle_scale(self, value: int) -> None:
            self.secondary_subtitle_scale_value = value

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "主字幕大小") if action.text() == "大").trigger()
    next(action for action in _submenu_actions(menu, "次字幕大小") if action.text() == "放大 5%").trigger()

    assert window.video.subtitle_scale_value == 115
    assert window.video.secondary_subtitle_scale_value == 105
```

- [ ] **Step 2: Run the focused UI tests to verify they fail**

Run:

```bash
uv run pytest \
  tests/test_player_window_ui.py::test_player_window_context_menu_includes_primary_and_secondary_subtitle_size_submenus \
  tests/test_player_window_ui.py::test_player_window_context_menu_size_actions_update_video_layer \
  -q
```

Expected: FAIL because the subtitle size menus and size action handlers do not exist yet.

- [ ] **Step 3: Commit the failing size-menu UI tests**

```bash
git add tests/test_player_window_ui.py
git commit -m "test: cover context menu subtitle size actions"
```

### Task 4: Implement Subtitle Size Menus And Action Handling

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Add subtitle scale state and presets**

Add these class constants near `_SUBTITLE_POSITION_PRESETS` in `src/atv_player/ui/player_window.py`:

```python
    _SUBTITLE_SCALE_PRESETS = {
        "很小": 70,
        "小": 85,
        "默认": 100,
        "大": 115,
        "很大": 130,
    }
```

Add these fields in `PlayerWindow.__init__` after the existing subtitle position fields:

```python
        self._main_subtitle_scale = 100
        self._secondary_subtitle_scale = 100
        self._main_subtitle_scale_supported = False
        self._secondary_subtitle_scale_supported = False
```

- [ ] **Step 2: Extend the context menu to include the new size submenus**

Update `_build_video_context_menu()` to insert the new submenus before `音轨`:

```python
        menu.addMenu(self._build_subtitle_scale_menu(menu, title="主字幕大小", secondary=False))
        menu.addMenu(self._build_subtitle_scale_menu(menu, title="次字幕大小", secondary=True))
```

- [ ] **Step 3: Add subtitle size menu construction and handlers**

Add these methods near `_build_subtitle_position_menu()`:

```python
    def _build_subtitle_scale_menu(self, parent: QWidget, title: str, secondary: bool) -> QMenu:
        menu = QMenu(title, parent)
        if secondary and not self._secondary_subtitle_scale_supported:
            menu.setEnabled(False)
            return menu
        if not secondary and not self._main_subtitle_scale_supported:
            menu.setEnabled(False)
            return menu

        group = QActionGroup(menu)
        group.setExclusive(True)
        current_value = self._secondary_subtitle_scale if secondary else self._main_subtitle_scale

        for label, value in self._SUBTITLE_SCALE_PRESETS.items():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(current_value == value)
            action.triggered.connect(
                lambda _checked=False, value=value, secondary=secondary: self._set_subtitle_scale_from_menu(value, secondary)
            )
            group.addAction(action)

        menu.addSeparator()
        menu.addAction("缩小 5%", lambda secondary=secondary: self._step_subtitle_scale(-5, secondary))
        menu.addAction("放大 5%", lambda secondary=secondary: self._step_subtitle_scale(5, secondary))
        menu.addAction("重置", lambda secondary=secondary: self._set_subtitle_scale_from_menu(100, secondary))
        return menu

    def _set_subtitle_scale_from_menu(self, value: int, secondary: bool) -> None:
        clamped = max(50, min(int(value), 200))
        try:
            if secondary:
                if not self._secondary_subtitle_scale_supported:
                    return
                self.video.set_secondary_subtitle_scale(clamped)
                self._secondary_subtitle_scale = clamped
            else:
                if not self._main_subtitle_scale_supported:
                    return
                self.video.set_subtitle_scale(clamped)
                self._main_subtitle_scale = clamped
        except Exception as exc:
            label = "次字幕大小设置失败" if secondary else "主字幕大小设置失败"
            self._append_log(f"{label}: {exc}")

    def _step_subtitle_scale(self, delta: int, secondary: bool) -> None:
        current = self._secondary_subtitle_scale if secondary else self._main_subtitle_scale
        self._set_subtitle_scale_from_menu(current + delta, secondary)
```

- [ ] **Step 4: Run the focused size-menu UI tests to verify they pass**

Run:

```bash
uv run pytest \
  tests/test_player_window_ui.py::test_player_window_context_menu_includes_primary_and_secondary_subtitle_size_submenus \
  tests/test_player_window_ui.py::test_player_window_context_menu_size_actions_update_video_layer \
  -q
```

Expected: PASS.

- [ ] **Step 5: Commit the subtitle size menu implementation**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: add context menu subtitle size controls"
```

### Task 5: Add Failing UI Tests For Size Session Reuse, Disablement, And Failure Recovery

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Add the failing size-reuse and degradation tests**

Append these tests after the existing size action tests:

```python
def test_player_window_reuses_primary_and_secondary_subtitle_scale_for_next_episode(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.current_url = ""
            self.subtitle_scale_value = 100
            self.secondary_subtitle_scale_value = 100
            self.tracks_by_url = {
                "http://m/1.m3u8": [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="简体中文 (默认)")],
                "http://m/2.m3u8": [SubtitleTrack(id=21, title="", lang="zh", is_default=True, is_forced=False, label="简体中文 (默认)")],
            }

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.current_url = url

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return self.tracks_by_url[self.current_url]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 21 if mode == "auto" else track_id

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def supports_subtitle_scale(self) -> bool:
            return True

        def subtitle_scale(self) -> int:
            return self.subtitle_scale_value

        def set_subtitle_scale(self, value: int) -> None:
            self.subtitle_scale_value = value

        def supports_secondary_subtitle_scale(self) -> bool:
            return True

        def secondary_subtitle_scale(self) -> int:
            return self.secondary_subtitle_scale_value

        def set_secondary_subtitle_scale(self, value: int) -> None:
            self.secondary_subtitle_scale_value = value

        def position_seconds(self) -> int:
            return 30

        def duration_seconds(self) -> int:
            return 120

    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "主字幕大小") if action.text() == "大").trigger()
    next(action for action in _submenu_actions(menu, "次字幕大小") if action.text() == "很大").trigger()

    window.play_next()

    assert window.video.subtitle_scale_value == 115
    assert window.video.secondary_subtitle_scale_value == 130


def test_player_window_disables_unsupported_subtitle_size_menus(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def supports_subtitle_scale(self) -> bool:
            return False

        def supports_secondary_subtitle_scale(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    primary_menu = next(action.menu() for action in menu.actions() if action.text() == "主字幕大小")
    secondary_menu = next(action.menu() for action in menu.actions() if action.text() == "次字幕大小")

    assert primary_menu is not None
    assert secondary_menu is not None
    assert primary_menu.isEnabled() is False
    assert secondary_menu.isEnabled() is False


def test_player_window_logs_when_supported_subtitle_scale_write_fails(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def supports_subtitle_scale(self) -> bool:
            return True

        def subtitle_scale(self) -> int:
            return 100

        def set_subtitle_scale(self, value: int) -> None:
            raise RuntimeError("scale boom")

        def supports_secondary_subtitle_scale(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "主字幕大小") if action.text() == "大").trigger()

    assert "主字幕大小设置失败: scale boom" in window.log_view.toPlainText()
```

- [ ] **Step 2: Run the focused UI tests to verify they fail**

Run:

```bash
uv run pytest \
  tests/test_player_window_ui.py::test_player_window_reuses_primary_and_secondary_subtitle_scale_for_next_episode \
  tests/test_player_window_ui.py::test_player_window_disables_unsupported_subtitle_size_menus \
  tests/test_player_window_ui.py::test_player_window_logs_when_supported_subtitle_scale_write_fails \
  -q
```

Expected: FAIL because subtitle-scale session reuse, support flags, and error handling do not exist yet.

- [ ] **Step 3: Commit the failing size reuse tests**

```bash
git add tests/test_player_window_ui.py
git commit -m "test: cover subtitle size session state"
```

### Task 5: Implement Subtitle Size Session State And Degradation

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Refresh subtitle scale support and state from the video layer**

Inside `_refresh_subtitle_state()` in `src/atv_player/ui/player_window.py`, after the existing subtitle position refresh, add:

```python
        self._main_subtitle_scale_supported = bool(
            getattr(self.video, "supports_subtitle_scale", lambda: hasattr(self.video, "subtitle_scale"))()
        )
        self._secondary_subtitle_scale_supported = bool(
            getattr(
                self.video,
                "supports_secondary_subtitle_scale",
                lambda: hasattr(self.video, "secondary_subtitle_scale"),
            )()
        )
        if self._main_subtitle_scale_supported and hasattr(self.video, "subtitle_scale"):
            self._main_subtitle_scale = self.video.subtitle_scale()
        if self._secondary_subtitle_scale_supported and hasattr(self.video, "secondary_subtitle_scale"):
            self._secondary_subtitle_scale = self.video.secondary_subtitle_scale()
```

- [ ] **Step 2: Reapply subtitle sizes during subtitle refresh**

Still inside `_refresh_subtitle_state()`, after the existing subtitle position reapply block, add:

```python
        if self._main_subtitle_scale_supported and hasattr(self.video, "set_subtitle_scale"):
            try:
                self.video.set_subtitle_scale(self._main_subtitle_scale)
            except Exception as exc:
                self._append_log(f"主字幕大小设置失败: {exc}")
        if self._secondary_subtitle_scale_supported and hasattr(self.video, "set_secondary_subtitle_scale"):
            try:
                self.video.set_secondary_subtitle_scale(self._secondary_subtitle_scale)
            except Exception as exc:
                self._append_log(f"次字幕大小设置失败: {exc}")
```

- [ ] **Step 3: Run the focused subtitle-size reuse tests to verify they pass**

Run:

```bash
uv run pytest \
  tests/test_player_window_ui.py::test_player_window_reuses_primary_and_secondary_subtitle_scale_for_next_episode \
  tests/test_player_window_ui.py::test_player_window_disables_unsupported_subtitle_size_menus \
  tests/test_player_window_ui.py::test_player_window_logs_when_supported_subtitle_scale_write_fails \
  -q
```

Expected: PASS.

- [ ] **Step 4: Run the focused regression suite**

Run:

```bash
uv run pytest tests/test_mpv_widget.py -q
uv run pytest tests/test_player_window_ui.py -k "subtitle or audio or context_menu" -q
```

Expected: PASS for both commands.

- [ ] **Step 5: Commit the subtitle-size state implementation**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: persist subtitle size context menu state"
```

## Self-Review

- Spec coverage:
  - Two new size submenus in the context menu: covered by Task 3 and Task 4.
  - Primary and secondary subtitle size read/write: covered by Task 1 and Task 2.
  - Fixed presets plus 5% step actions: covered by Task 3 and Task 4.
  - Session-level carry-forward across episodes: covered by Task 5.
  - Independent support detection and menu disablement: covered by Task 1, Task 2, and Task 5.
  - Non-fatal error logging: covered by Task 5.
- Placeholder scan:
  - No `TODO`, `TBD`, or deferred “implement later” markers remain.
  - Each code-changing step includes concrete code or exact commands.
- Type consistency:
  - The plan consistently uses `subtitle_scale()`, `set_subtitle_scale()`, `secondary_subtitle_scale()`, `set_secondary_subtitle_scale()`, `supports_subtitle_scale()`, and `supports_secondary_subtitle_scale()`.
