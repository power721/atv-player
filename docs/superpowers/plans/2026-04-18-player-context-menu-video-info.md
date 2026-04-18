# Player Context Menu Video Info Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `视频信息` action to the player video right-click menu that toggles mpv's built-in persistent playback information overlay.

**Architecture:** Keep the change inside the existing player UI and mpv wrapper layers. `MpvWidget` gets one narrow helper that delegates to mpv's built-in stats script binding, and `PlayerWindow` wires that helper into the existing context-menu builder with the same non-fatal logging pattern used by other menu-triggered player actions.

**Tech Stack:** Python 3.12, PySide6, python-mpv, pytest, pytest-qt

---

## File Structure

- `src/atv_player/player/mpv_widget.py`
  Responsibility: own the mpv-facing command that toggles the built-in stats overlay and handle shutdown-safe failure behavior.
- `tests/test_mpv_widget.py`
  Responsibility: prove the wrapper issues the expected mpv command and stays non-fatal when the player shuts down during the command.
- `src/atv_player/ui/player_window.py`
  Responsibility: extend the existing video context menu with a top-level `视频信息` action and log menu-triggered failures.
- `tests/test_player_window_ui.py`
  Responsibility: prove the new top-level action appears in the menu, calls the video layer, and logs failures without crashing.

### Task 1: Add an mpv wrapper helper for video info

**Files:**
- Modify: `tests/test_mpv_widget.py`
- Modify: `src/atv_player/player/mpv_widget.py`

- [ ] **Step 1: Write the failing tests**

Add these tests near the other small wrapper behavior tests in `tests/test_mpv_widget.py`:

```python
def test_mpv_widget_toggles_video_info_overlay(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.command_calls: list[tuple[object, ...]] = []
            self.core_shutdown = False

        def command(self, *args) -> None:
            self.command_calls.append(args)

    widget._player = FakePlayer()

    widget.toggle_video_info()

    assert widget._player.command_calls == [
        ("script-binding", "stats/display-stats-toggle")
    ]


def test_mpv_widget_ignores_video_info_toggle_when_player_shuts_down(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.core_shutdown = False

        def command(self, *args) -> None:
            self.core_shutdown = True
            raise RuntimeError("core is gone")

    widget._player = FakePlayer()

    widget.toggle_video_info()
```

- [ ] **Step 2: Run the wrapper tests to verify they fail**

Run:

```bash
uv run pytest tests/test_mpv_widget.py::test_mpv_widget_toggles_video_info_overlay tests/test_mpv_widget.py::test_mpv_widget_ignores_video_info_toggle_when_player_shuts_down -v
```

Expected:

- `test_mpv_widget_toggles_video_info_overlay` fails with `AttributeError: 'MpvWidget' object has no attribute 'toggle_video_info'`
- `test_mpv_widget_ignores_video_info_toggle_when_player_shuts_down` fails for the same reason

- [ ] **Step 3: Write the minimal implementation**

Add this method in `src/atv_player/player/mpv_widget.py` after `apply_audio_mode()` and before `mouseDoubleClickEvent()`:

```python
    def toggle_video_info(self) -> None:
        if self._player is None:
            return
        try:
            self._player.command("script-binding", "stats/display-stats-toggle")
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise
```

- [ ] **Step 4: Run the wrapper tests to verify they pass**

Run:

```bash
uv run pytest tests/test_mpv_widget.py::test_mpv_widget_toggles_video_info_overlay tests/test_mpv_widget.py::test_mpv_widget_ignores_video_info_toggle_when_player_shuts_down -v
```

Expected:

- both tests pass

- [ ] **Step 5: Commit the wrapper change**

Run:

```bash
git add tests/test_mpv_widget.py src/atv_player/player/mpv_widget.py
git commit -m "feat: add mpv video info toggle helper"
```

### Task 2: Add the `视频信息` action to the player context menu

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Modify: `src/atv_player/ui/player_window.py`
- Regression Test: `tests/test_mpv_widget.py`

- [ ] **Step 1: Write the failing UI tests**

Update the existing menu-structure test in `tests/test_player_window_ui.py` so the expected top-level action list includes `视频信息`, and add the following two tests nearby:

```python
def test_player_window_context_menu_video_info_action_calls_video_layer(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.video_info_toggles = 0

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

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return True

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

        def toggle_video_info(self) -> None:
            self.video_info_toggles += 1

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    video_info_action = next(action for action in menu.actions() if action.text() == "视频信息")
    video_info_action.trigger()

    assert window.video.video_info_toggles == 1


def test_player_window_context_menu_video_info_action_logs_failures(qtbot) -> None:
    class FakeVideo:
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

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return True

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

        def toggle_video_info(self) -> None:
            raise RuntimeError("info boom")

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in menu.actions() if action.text() == "视频信息").trigger()

    assert "视频信息显示失败: info boom" in window.log_view.toPlainText()
```

Also change the existing top-level menu expectation in `test_player_window_builds_video_context_menu_with_track_submenus()` to:

```python
    assert [action.text() for action in menu.actions()] == [
        "主字幕",
        "次字幕",
        "主字幕位置",
        "次字幕位置",
        "主字幕大小",
        "次字幕大小",
        "音轨",
        "视频信息",
    ]
```

- [ ] **Step 2: Run the UI tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_builds_video_context_menu_with_track_submenus tests/test_player_window_ui.py::test_player_window_context_menu_video_info_action_calls_video_layer tests/test_player_window_ui.py::test_player_window_context_menu_video_info_action_logs_failures -v
```

Expected:

- the menu-structure test fails because `视频信息` is missing
- the two new tests fail because the action and handler do not exist yet

- [ ] **Step 3: Write the minimal UI implementation**

In `src/atv_player/ui/player_window.py`, update `_build_video_context_menu()` and add a handler method after `_step_subtitle_scale()`:

```python
    def _build_video_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addMenu(self._build_primary_subtitle_menu(menu))
        menu.addMenu(self._build_secondary_subtitle_menu(menu))
        menu.addMenu(self._build_subtitle_position_menu(menu, title="主字幕位置", secondary=False))
        menu.addMenu(self._build_subtitle_position_menu(menu, title="次字幕位置", secondary=True))
        menu.addMenu(self._build_subtitle_scale_menu(menu, title="主字幕大小", secondary=False))
        menu.addMenu(self._build_subtitle_scale_menu(menu, title="次字幕大小", secondary=True))
        menu.addMenu(self._build_audio_menu(menu))
        menu.addAction("视频信息", self._toggle_video_info_from_menu)
        return menu
```

```python
    def _toggle_video_info_from_menu(self) -> None:
        try:
            self.video.toggle_video_info()
        except Exception as exc:
            self._append_log(f"视频信息显示失败: {exc}")
```

- [ ] **Step 4: Run focused regression tests to verify they pass**

Run:

```bash
uv run pytest tests/test_mpv_widget.py::test_mpv_widget_toggles_video_info_overlay tests/test_mpv_widget.py::test_mpv_widget_ignores_video_info_toggle_when_player_shuts_down tests/test_player_window_ui.py::test_player_window_builds_video_context_menu_with_track_submenus tests/test_player_window_ui.py::test_player_window_context_menu_video_info_action_calls_video_layer tests/test_player_window_ui.py::test_player_window_context_menu_video_info_action_logs_failures -v
```

Expected:

- all five tests pass

- [ ] **Step 5: Commit the UI integration**

Run:

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py tests/test_mpv_widget.py src/atv_player/player/mpv_widget.py
git commit -m "feat: add player context menu video info action"
```
