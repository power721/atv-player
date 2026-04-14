# Player Window Vertical Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the player window to a vertical shell so the video, playlist, and details stay in the upper region while playback controls span the full window width at the bottom.

**Architecture:** Keep the change localized to `PlayerWindow` and its focused UI tests. Replace the current "video column owns bottom controls" structure with an outer `QVBoxLayout` shell that stacks the existing horizontal `main_splitter` above `bottom_area`, preserving the right sidebar vertical splitter and existing fullscreen/toggle behavior.

**Tech Stack:** Python 3.14, PySide6, pytest, pytest-qt

---

## File Structure

- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`
- Reference only: `docs/superpowers/specs/2026-04-14-player-window-vertical-layout-design.md`

### Task 1: Lock The Vertical Shell With Failing UI Tests

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing layout and restore tests**

```python
def test_player_window_uses_vertical_shell_with_bottom_controls(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    root_layout = window.layout()

    assert root_layout is not None
    assert root_layout.count() == 2
    assert root_layout.itemAt(0).widget() is window.main_splitter
    assert root_layout.itemAt(1).widget() is window.bottom_area
    assert window.main_splitter.orientation() == Qt.Orientation.Horizontal
    assert window.sidebar_splitter.orientation() == Qt.Orientation.Vertical


def test_player_window_bottom_controls_are_not_nested_inside_video_pane(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    main_container = window.main_splitter.widget(0)
    assert main_container is not None
    assert main_container.layout().indexOf(window.bottom_area) == -1


def test_player_window_persists_and_restores_horizontal_content_splitter_state(qtbot) -> None:
    saved = {"count": 0}
    config = AppConfig()
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)
    window.show()
    window.main_splitter.setSizes([920, 280])

    window._persist_geometry()

    restored = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(restored)
    restored.show()

    assert config.player_main_splitter_state is not None
    assert saved["count"] >= 1
    assert restored.main_splitter.saveState() == QByteArray(config.player_main_splitter_state)


def test_player_window_falls_back_when_saved_splitter_state_is_invalid(qtbot) -> None:
    config = AppConfig(player_main_splitter_state=b"not-a-real-splitter-state")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.show()

    sizes = window.main_splitter.sizes()

    assert len(sizes) == 2
    assert all(size > 0 for size in sizes)
```

- [ ] **Step 2: Run the focused UI tests to verify red**

Run: `uv run pytest tests/test_player_window_ui.py -q`
Expected: FAIL because `bottom_area` is still added to the left video column and the root layout only contains `main_splitter`.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_player_window_ui.py
git commit -m "test: cover vertical player window shell"
```

### Task 2: Refactor PlayerWindow To Use The New Outer Layout

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Replace the old nested left-column layout with an outer vertical shell**

```python
    def _restore_main_splitter_state(self) -> None:
        if self.config is None or not self.config.player_main_splitter_state:
            self.main_splitter.setSizes([960, 320])
            return
        restored = self.main_splitter.restoreState(QByteArray(self.config.player_main_splitter_state))
        if not restored:
            self.main_splitter.setSizes([960, 320])

    video_container = QWidget()
    video_layout = QVBoxLayout(video_container)
    video_layout.setContentsMargins(0, 0, 0, 0)
    video_layout.addWidget(self.video)

    self.sidebar_splitter = QSplitter(Qt.Orientation.Vertical)
    self.sidebar_splitter.addWidget(self.playlist)
    self.sidebar_splitter.addWidget(self.details)
    self.sidebar_splitter.setChildrenCollapsible(True)

    sidebar_layout = QVBoxLayout()
    sidebar_layout.addWidget(self.sidebar_actions_widget)
    sidebar_layout.addWidget(self.sidebar_splitter)
    self.sidebar_container = QWidget()
    self.sidebar_container.setLayout(sidebar_layout)

    self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
    self.main_splitter.addWidget(video_container)
    self.main_splitter.addWidget(self.sidebar_container)
    self.main_splitter.setStretchFactor(0, 3)
    self.main_splitter.setStretchFactor(1, 1)
    self._restore_main_splitter_state()

    layout = QVBoxLayout(self)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(self.main_splitter, 1)
    layout.addWidget(self.bottom_area, 0)
```

- [ ] **Step 2: Keep visibility and persistence behavior aligned with the new shell**

```python
    def _apply_visibility_state(self) -> None:
        is_fullscreen = self.isFullScreen()
        sidebar_hidden = is_fullscreen or self.wide_button.isChecked()
        self.bottom_area.setHidden(is_fullscreen)
        self.sidebar_actions_widget.setHidden(is_fullscreen)
        self.sidebar_container.setHidden(sidebar_hidden)
        self.playlist.setHidden(is_fullscreen or not self.toggle_playlist_button.isChecked())
        self.details.setHidden(is_fullscreen or not self.toggle_details_button.isChecked())

    def _persist_geometry(self) -> None:
        if self.config is None:
            return
        self.config.player_window_geometry = bytes(self.saveGeometry())
        self.config.player_main_splitter_state = bytes(self.main_splitter.saveState())
        self._save_config()
```

- [ ] **Step 3: Run the focused UI tests to verify green**

Run: `uv run pytest tests/test_player_window_ui.py -q`
Expected: PASS with the new vertical shell tests and the existing fullscreen, time-label, shortcut, and sidebar tests all green.

- [ ] **Step 4: Commit the layout refactor**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: move player controls into bottom region"
```

### Task 3: Regression Check Around Player Window Behavior

**Files:**
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Re-run the player window UI suite from a clean tree**

Run: `uv run pytest tests/test_player_window_ui.py -q`
Expected: PASS and no regressions in fullscreen hiding, sidebar toggles, progress syncing, restore behavior, or keyboard shortcuts.

- [ ] **Step 2: Summarize what changed before handing off**

```text
- Root player window now stacks `main_splitter` above `bottom_area`.
- Horizontal resizing remains on `main_splitter`; sidebar vertical resizing remains on `sidebar_splitter`.
- Playback controls no longer live inside the left video pane.
- Persisted splitter state continues to represent the horizontal video/sidebar split.
```
