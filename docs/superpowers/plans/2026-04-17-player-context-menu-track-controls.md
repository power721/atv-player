# Player Context Menu Track Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a video-surface right-click menu that controls primary subtitles, secondary subtitles, subtitle positions, and audio tracks while keeping the existing bottom-bar subtitle and audio selectors synchronized.

**Architecture:** Keep mpv-specific behavior inside `MpvWidget` and keep the menu, session-level preference reuse, and UI synchronization inside `PlayerWindow`. Implement the feature in three layers: mpv primitives first, menu construction and action wiring second, then cross-episode state reuse and failure recovery last.

**Tech Stack:** Python 3.12, PySide6 widgets, pytest, pytest-qt, python-mpv

---

## File Structure

- Modify: `src/atv_player/player/mpv_widget.py`
  - Add secondary subtitle selection and subtitle-position helpers on top of the existing primary subtitle and audio helpers.
- Modify: `src/atv_player/ui/player_window.py`
  - Add right-click menu creation, submenu actions, state synchronization with the bottom-bar selectors, session-level secondary subtitle preference, and independent subtitle-position state.
- Modify: `tests/test_mpv_widget.py`
  - Add focused unit tests for secondary subtitle and subtitle-position mpv wrapper behavior.
- Modify: `tests/test_player_window_ui.py`
  - Add focused UI tests for context-menu structure, menu action wiring, selector synchronization, position control behavior, session reuse, and failure recovery.

### Task 1: Add Failing mpv Wrapper Tests For Secondary Subtitles And Subtitle Position

**Files:**
- Modify: `tests/test_mpv_widget.py`
- Test: `tests/test_mpv_widget.py`

- [ ] **Step 1: Write the failing secondary-subtitle and subtitle-position tests**

Add these tests after the existing primary subtitle tests in `tests/test_mpv_widget.py`:

```python
def test_mpv_widget_can_disable_or_select_a_specific_secondary_subtitle_track(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(track_list=[])
    widget._player = player

    disabled_track_id = widget.apply_secondary_subtitle_mode("off")
    selected_track_id = widget.apply_secondary_subtitle_mode("track", track_id=12)

    assert disabled_track_id is None
    assert selected_track_id == 12
    assert player.secondary_sid == 12


def test_mpv_widget_reads_and_writes_primary_subtitle_position(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"sub-pos": 50}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.subtitle_position() == 50

    widget.set_subtitle_position(70)

    assert widget.subtitle_position() == 70


def test_mpv_widget_reads_and_writes_secondary_subtitle_position(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)

    class FakePlayer:
        def __init__(self) -> None:
            self.options = {"secondary-sub-pos": 50}

        def __getitem__(self, key: str) -> object:
            return self.options[key]

        def __setitem__(self, key: str, value: object) -> None:
            self.options[key] = value

    widget._player = FakePlayer()

    assert widget.secondary_subtitle_position() == 50

    widget.set_secondary_subtitle_position(30)

    assert widget.secondary_subtitle_position() == 30
```

- [ ] **Step 2: Run the focused mpv tests to verify they fail**

Run:

```bash
uv run pytest \
  tests/test_mpv_widget.py::test_mpv_widget_can_disable_or_select_a_specific_secondary_subtitle_track \
  tests/test_mpv_widget.py::test_mpv_widget_reads_and_writes_primary_subtitle_position \
  tests/test_mpv_widget.py::test_mpv_widget_reads_and_writes_secondary_subtitle_position \
  -q
```

Expected: FAIL because `apply_secondary_subtitle_mode()`, `subtitle_position()`, `set_subtitle_position()`, `secondary_subtitle_position()`, and `set_secondary_subtitle_position()` do not exist yet.

- [ ] **Step 3: Commit the failing test additions**

```bash
git add tests/test_mpv_widget.py
git commit -m "test: cover mpv secondary subtitle controls"
```

### Task 2: Implement mpv Secondary Subtitle And Subtitle Position Primitives

**Files:**
- Modify: `src/atv_player/player/mpv_widget.py`
- Test: `tests/test_mpv_widget.py`

- [ ] **Step 1: Add minimal mpv property helpers**

Inside `MpvWidget`, add these helpers above `apply_subtitle_mode()`:

```python
    def _player_property(self, name: str, default: object | None = None) -> object | None:
        if self._player is None:
            return default
        try:
            return self._player[name]
        except Exception:
            if hasattr(self._player, name.replace("-", "_")):
                return getattr(self._player, name.replace("-", "_"))
            return default

    def _set_player_property(self, name: str, value: object) -> None:
        if self._player is None:
            return
        try:
            if hasattr(type(self._player), "__setitem__"):
                self._player[name] = value
            else:
                setattr(self._player, name.replace("-", "_"), value)
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise
```

- [ ] **Step 2: Add the secondary subtitle mode API**

Add this method below `apply_subtitle_mode()`:

```python
    def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
        if self._player is None:
            return None
        try:
            if mode == "off":
                self._set_player_property("secondary-sid", "no")
                return None
            if mode == "track" and track_id is not None:
                self._set_player_property("secondary-sid", track_id)
                return track_id
            self._set_player_property("secondary-sid", "no")
            return None
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return None
            raise
```

- [ ] **Step 3: Add the subtitle-position getters and setters**

Add these methods below `apply_secondary_subtitle_mode()`:

```python
    def subtitle_position(self) -> int:
        value = self._player_property("sub-pos", 50)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 50

    def set_subtitle_position(self, value: int) -> None:
        clamped = max(0, min(int(value), 100))
        self._set_player_property("sub-pos", clamped)

    def secondary_subtitle_position(self) -> int:
        value = self._player_property("secondary-sub-pos", 50)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 50

    def set_secondary_subtitle_position(self, value: int) -> None:
        clamped = max(0, min(int(value), 100))
        self._set_player_property("secondary-sub-pos", clamped)
```

- [ ] **Step 4: Run the focused mpv tests to verify they pass**

Run:

```bash
uv run pytest \
  tests/test_mpv_widget.py::test_mpv_widget_can_disable_or_select_a_specific_secondary_subtitle_track \
  tests/test_mpv_widget.py::test_mpv_widget_reads_and_writes_primary_subtitle_position \
  tests/test_mpv_widget.py::test_mpv_widget_reads_and_writes_secondary_subtitle_position \
  -q
```

Expected: PASS.

- [ ] **Step 5: Run the full mpv widget suite to verify no regression**

Run:

```bash
uv run pytest tests/test_mpv_widget.py -q
```

Expected: PASS for the full file.

- [ ] **Step 6: Commit the mpv wrapper implementation**

```bash
git add src/atv_player/player/mpv_widget.py tests/test_mpv_widget.py
git commit -m "feat: add mpv context menu subtitle primitives"
```

### Task 3: Add Failing Player Window Tests For Video Context Menu Structure And Track Actions

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Add menu structure and action-wiring tests**

Add these imports near the top of `tests/test_player_window_ui.py`:

```python
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu
```

Add these helpers and tests after the existing subtitle and audio selector tests:

```python
def _submenu_actions(menu: QMenu, title: str) -> list[QAction]:
    submenu = next(action.menu() for action in menu.actions() if action.text() == title)
    assert submenu is not None
    return submenu.actions()


def test_player_window_builds_video_context_menu_with_track_submenus(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [
                SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
            ]

        def audio_tracks(self) -> list[AudioTrack]:
            return [
                AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
            ]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 11 if mode == "auto" else track_id

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None if mode == "off" else track_id

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 31 if mode == "auto" else track_id

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
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
        "音轨",
    ]
    assert [action.text() for action in _submenu_actions(menu, "主字幕")] == ["自动选择", "关闭字幕", "中文 (默认)", "English"]
    assert [action.text() for action in _submenu_actions(menu, "次字幕")] == ["关闭次字幕", "中文 (默认)", "English"]
    assert [action.text() for action in _submenu_actions(menu, "音轨")] == ["自动选择", "国语 (默认)", "English Dub"]


def test_player_window_context_menu_primary_subtitle_action_syncs_bottom_combo(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.audio_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [
                SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
            ]

        def audio_tracks(self) -> list[AudioTrack]:
            return [AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)")]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((mode, track_id))
            return 31

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.video.subtitle_apply_calls.clear()

    menu = window._build_video_context_menu()
    english_action = next(action for action in _submenu_actions(menu, "主字幕") if action.text() == "English")
    english_action.trigger()

    assert window.video.subtitle_apply_calls == [("track", 12)]
    assert window.subtitle_combo.currentText() == "English"


def test_player_window_context_menu_secondary_subtitle_and_audio_actions_call_video_layer(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.secondary_subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.audio_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)")]

        def audio_tracks(self) -> list[AudioTrack]:
            return [
                AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
            ]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 11

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.secondary_subtitle_apply_calls.append((mode, track_id))
            return track_id

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((mode, track_id))
            return track_id if mode == "track" else 31

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.video.audio_apply_calls.clear()

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "次字幕") if action.text() == "中文 (默认)").trigger()
    next(action for action in _submenu_actions(menu, "音轨") if action.text() == "English Dub").trigger()

    assert window.video.secondary_subtitle_apply_calls == [("track", 11)]
    assert window.video.audio_apply_calls == [("track", 32)]
    assert window.audio_combo.currentText() == "English Dub"
```

- [ ] **Step 2: Run the focused UI tests to verify they fail**

Run:

```bash
uv run pytest \
  tests/test_player_window_ui.py::test_player_window_builds_video_context_menu_with_track_submenus \
  tests/test_player_window_ui.py::test_player_window_context_menu_primary_subtitle_action_syncs_bottom_combo \
  tests/test_player_window_ui.py::test_player_window_context_menu_secondary_subtitle_and_audio_actions_call_video_layer \
  -q
```

Expected: FAIL because `_build_video_context_menu()` and the related context-menu action handlers do not exist yet.

- [ ] **Step 3: Commit the failing UI tests**

```bash
git add tests/test_player_window_ui.py
git commit -m "test: cover player context menu track actions"
```

### Task 4: Implement The Player Window Context Menu And Track Action Wiring

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Add the context-menu imports and session state fields**

Update the imports near the top of `src/atv_player/ui/player_window.py`:

```python
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QCursor, QIcon, QImage, QKeyEvent, QKeySequence, QMouseEvent, QPixmap, QShortcut
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QStyleOptionSlider, QToolTip
```

Add this dataclass below `SubtitlePreference`:

```python
@dataclass(slots=True)
class SecondarySubtitlePreference:
    mode: str = "off"
    title: str = ""
    lang: str = ""
    is_default: bool = False
    is_forced: bool = False
```

Add these fields in `PlayerWindow.__init__` next to the existing subtitle and audio fields:

```python
        self._secondary_subtitle_preference = SecondarySubtitlePreference()
        self._main_subtitle_position = 50
        self._secondary_subtitle_position = 50
```

- [ ] **Step 2: Enable a custom context menu on the video widget**

Inside `PlayerWindow.__init__`, after `_configure_video_surface_widgets()` and before the playback button connections, add:

```python
        self.video_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.video_widget.customContextMenuRequested.connect(self._show_video_context_menu)
```

- [ ] **Step 3: Add the menu-construction helpers**

Add these helpers near `_change_audio_selection()`:

```python
    _SUBTITLE_POSITION_PRESETS = {
        "顶部": 10,
        "偏上": 30,
        "默认": 50,
        "偏下": 70,
        "底部": 90,
    }

    def _show_video_context_menu(self, pos) -> None:
        menu = self._build_video_context_menu()
        menu.exec(self.video_widget.mapToGlobal(pos))

    def _build_video_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addMenu(self._build_primary_subtitle_menu(menu))
        menu.addMenu(self._build_secondary_subtitle_menu(menu))
        menu.addMenu(self._build_subtitle_position_menu(menu, title="主字幕位置", secondary=False))
        menu.addMenu(self._build_subtitle_position_menu(menu, title="次字幕位置", secondary=True))
        menu.addMenu(self._build_audio_menu(menu))
        return menu
```

- [ ] **Step 4: Add the primary subtitle and audio submenus**

Add these methods:

```python
    def _build_primary_subtitle_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu("主字幕", parent)
        group = QActionGroup(menu)
        group.setExclusive(True)

        auto_action = menu.addAction("自动选择")
        auto_action.setCheckable(True)
        auto_action.setChecked(self._subtitle_preference.mode == "auto")
        auto_action.triggered.connect(lambda: self._set_primary_subtitle_from_menu("auto", None))
        group.addAction(auto_action)

        off_action = menu.addAction("关闭字幕")
        off_action.setCheckable(True)
        off_action.setChecked(self._subtitle_preference.mode == "off")
        off_action.triggered.connect(lambda: self._set_primary_subtitle_from_menu("off", None))
        group.addAction(off_action)

        for track in self._subtitle_tracks:
            action = menu.addAction(track.label)
            action.setCheckable(True)
            action.setChecked(
                self._subtitle_preference.mode == "track"
                and self._subtitle_preference.title == track.title
                and self._subtitle_preference.lang == track.lang
            )
            action.triggered.connect(lambda _checked=False, track_id=track.id: self._set_primary_subtitle_from_menu("track", track_id))
            group.addAction(action)

        return menu

    def _build_audio_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu("音轨", parent)
        group = QActionGroup(menu)
        group.setExclusive(True)

        auto_action = menu.addAction("自动选择")
        auto_action.setCheckable(True)
        auto_action.setChecked(self._audio_preference.mode == "auto")
        auto_action.triggered.connect(lambda: self._set_audio_from_menu("auto", None))
        group.addAction(auto_action)

        for track in self._audio_tracks:
            action = menu.addAction(track.label)
            action.setCheckable(True)
            action.setChecked(
                self._audio_preference.mode == "track"
                and self._audio_preference.title == track.title
                and self._audio_preference.lang == track.lang
            )
            action.triggered.connect(lambda _checked=False, track_id=track.id: self._set_audio_from_menu("track", track_id))
            group.addAction(action)

        return menu
```

- [ ] **Step 5: Add the secondary subtitle submenu and menu action handlers**

Add these methods:

```python
    def _build_secondary_subtitle_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu("次字幕", parent)
        group = QActionGroup(menu)
        group.setExclusive(True)

        off_action = menu.addAction("关闭次字幕")
        off_action.setCheckable(True)
        off_action.setChecked(self._secondary_subtitle_preference.mode == "off")
        off_action.triggered.connect(lambda: self._set_secondary_subtitle_from_menu("off", None))
        group.addAction(off_action)

        for track in self._subtitle_tracks:
            action = menu.addAction(track.label)
            action.setCheckable(True)
            action.setChecked(
                self._secondary_subtitle_preference.mode == "track"
                and self._secondary_subtitle_preference.title == track.title
                and self._secondary_subtitle_preference.lang == track.lang
            )
            action.triggered.connect(
                lambda _checked=False, track_id=track.id: self._set_secondary_subtitle_from_menu("track", track_id)
            )
            group.addAction(action)

        return menu

    def _set_primary_subtitle_from_menu(self, mode: str, track_id: int | None) -> None:
        if mode == "auto":
            self.subtitle_combo.setCurrentIndex(0)
            return
        if mode == "off":
            self.subtitle_combo.setCurrentIndex(1)
            return
        for index in range(self.subtitle_combo.count()):
            item_data = self.subtitle_combo.itemData(index)
            if item_data == ("track", track_id):
                self.subtitle_combo.setCurrentIndex(index)
                return

    def _set_audio_from_menu(self, mode: str, track_id: int | None) -> None:
        if mode == "auto":
            self.audio_combo.setCurrentIndex(0)
            return
        for index in range(self.audio_combo.count()):
            item_data = self.audio_combo.itemData(index)
            if item_data == ("track", track_id):
                self.audio_combo.setCurrentIndex(index)
                return

    def _set_secondary_subtitle_from_menu(self, mode: str, track_id: int | None) -> None:
        try:
            if mode == "off":
                self._secondary_subtitle_preference = SecondarySubtitlePreference()
                self.video.apply_secondary_subtitle_mode("off")
                return
            track = next((track for track in self._subtitle_tracks if track.id == track_id), None)
            if track is None:
                return
            self._secondary_subtitle_preference = SecondarySubtitlePreference(
                mode="track",
                title=track.title,
                lang=track.lang,
                is_default=track.is_default,
                is_forced=track.is_forced,
            )
            self.video.apply_secondary_subtitle_mode("track", track_id=track.id)
        except Exception as exc:
            self._append_log(f"次字幕切换失败: {exc}")
```

- [ ] **Step 6: Run the focused context-menu UI tests to verify they pass**

Run:

```bash
uv run pytest \
  tests/test_player_window_ui.py::test_player_window_builds_video_context_menu_with_track_submenus \
  tests/test_player_window_ui.py::test_player_window_context_menu_primary_subtitle_action_syncs_bottom_combo \
  tests/test_player_window_ui.py::test_player_window_context_menu_secondary_subtitle_and_audio_actions_call_video_layer \
  -q
```

Expected: PASS.

- [ ] **Step 7: Commit the menu scaffolding implementation**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: add player track context menu"
```

### Task 5: Add Failing UI Tests For Subtitle Position Controls, Session Reuse, And Failure Recovery

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Add the position-control and cross-episode tests**

Append these tests after the menu action tests:

```python
def test_player_window_context_menu_position_actions_update_video_layer(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.subtitle_position_value = 50
            self.secondary_subtitle_position_value = 50

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
            return self.subtitle_position_value

        def set_subtitle_position(self, value: int) -> None:
            self.subtitle_position_value = value

        def secondary_subtitle_position(self) -> int:
            return self.secondary_subtitle_position_value

        def set_secondary_subtitle_position(self, value: int) -> None:
            self.secondary_subtitle_position_value = value

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "主字幕位置") if action.text() == "偏下").trigger()
    next(action for action in _submenu_actions(menu, "次字幕位置") if action.text() == "上移 5%").trigger()

    assert window.video.subtitle_position_value == 70
    assert window.video.secondary_subtitle_position_value == 45


def test_player_window_reuses_secondary_subtitle_and_position_preferences_for_next_episode(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.current_url = ""
            self.secondary_subtitle_apply_calls: list[tuple[str, str, int | None]] = []
            self.subtitle_position_value = 50
            self.secondary_subtitle_position_value = 50
            self.tracks_by_url = {
                "http://m/1.m3u8": [
                    SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                    SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
                ],
                "http://m/2.m3u8": [
                    SubtitleTrack(id=21, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                    SubtitleTrack(id=22, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
                ],
            }

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.current_url = url

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return self.tracks_by_url[self.current_url]

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 21 if mode == "auto" else track_id

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.secondary_subtitle_apply_calls.append((self.current_url, mode, track_id))
            return track_id if mode == "track" else None

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return self.subtitle_position_value

        def set_subtitle_position(self, value: int) -> None:
            self.subtitle_position_value = value

        def secondary_subtitle_position(self) -> int:
            return self.secondary_subtitle_position_value

        def set_secondary_subtitle_position(self, value: int) -> None:
            self.secondary_subtitle_position_value = value

        def position_seconds(self) -> int:
            return 30

        def duration_seconds(self) -> int:
            return 120

    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "次字幕") if action.text() == "English").trigger()
    next(action for action in _submenu_actions(menu, "主字幕位置") if action.text() == "偏下").trigger()
    next(action for action in _submenu_actions(menu, "次字幕位置") if action.text() == "偏上").trigger()
    window.video.secondary_subtitle_apply_calls.clear()

    window.play_next()

    assert ("http://m/2.m3u8", "track", 22) in window.video.secondary_subtitle_apply_calls
    assert window.video.subtitle_position_value == 70
    assert window.video.secondary_subtitle_position_value == 30


def test_player_window_logs_and_recovers_when_secondary_subtitle_or_position_apply_fails(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)")]

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 11

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            raise RuntimeError("secondary boom")

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            raise RuntimeError("position boom")

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "次字幕") if action.text() == "中文 (默认)").trigger()
    next(action for action in _submenu_actions(menu, "主字幕位置") if action.text() == "偏下").trigger()

    assert "次字幕切换失败: secondary boom" in window.log_view.toPlainText()
    assert "主字幕位置设置失败: position boom" in window.log_view.toPlainText()
```

- [ ] **Step 2: Run the focused UI tests to verify they fail**

Run:

```bash
uv run pytest \
  tests/test_player_window_ui.py::test_player_window_context_menu_position_actions_update_video_layer \
  tests/test_player_window_ui.py::test_player_window_reuses_secondary_subtitle_and_position_preferences_for_next_episode \
  tests/test_player_window_ui.py::test_player_window_logs_and_recovers_when_secondary_subtitle_or_position_apply_fails \
  -q
```

Expected: FAIL because position-menu helpers, session reuse for secondary subtitles, and failure logging for position writes do not exist yet.

- [ ] **Step 3: Commit the failing reuse and recovery tests**

```bash
git add tests/test_player_window_ui.py
git commit -m "test: cover context menu subtitle position state"
```

### Task 6: Implement Subtitle Position Menus, Session Reuse, And Failure Recovery

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Add secondary-subtitle preference matching and reuse**

Add these helpers near `_matching_track_for_preference()`:

```python
    def _secondary_subtitle_track_match_score(
        self,
        track: SubtitleTrack,
        preference: SecondarySubtitlePreference,
    ) -> tuple[int, int, int]:
        return (
            int(bool(preference.title) and track.title == preference.title),
            int(bool(preference.lang) and track.lang == preference.lang),
            int(track.is_forced == preference.is_forced and track.is_default == preference.is_default),
        )

    def _matching_secondary_track_for_preference(self) -> SubtitleTrack | None:
        if self._secondary_subtitle_preference.mode != "track" or not self._subtitle_tracks:
            return None
        ranked_tracks = sorted(
            self._subtitle_tracks,
            key=lambda track: self._secondary_subtitle_track_match_score(track, self._secondary_subtitle_preference),
            reverse=True,
        )
        best_track = ranked_tracks[0]
        if self._secondary_subtitle_track_match_score(best_track, self._secondary_subtitle_preference) == (0, 0, 0):
            return None
        return best_track

    def _apply_secondary_subtitle_preference(self) -> None:
        if self._secondary_subtitle_preference.mode == "off":
            self.video.apply_secondary_subtitle_mode("off")
            return
        matched_track = self._matching_secondary_track_for_preference()
        if matched_track is None:
            self._secondary_subtitle_preference = SecondarySubtitlePreference()
            self.video.apply_secondary_subtitle_mode("off")
            return
        self.video.apply_secondary_subtitle_mode("track", track_id=matched_track.id)
```

- [ ] **Step 2: Add subtitle-position menu builders and handlers**

Add these methods:

```python
    def _build_subtitle_position_menu(self, parent: QWidget, title: str, secondary: bool) -> QMenu:
        menu = QMenu(title, parent)
        group = QActionGroup(menu)
        group.setExclusive(True)
        current_value = self._secondary_subtitle_position if secondary else self._main_subtitle_position

        for label, value in self._SUBTITLE_POSITION_PRESETS.items():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(current_value == value)
            action.triggered.connect(
                lambda _checked=False, value=value, secondary=secondary: self._set_subtitle_position_from_menu(value, secondary)
            )
            group.addAction(action)

        menu.addSeparator()
        menu.addAction("上移 5%", lambda secondary=secondary: self._step_subtitle_position(-5, secondary))
        menu.addAction("下移 5%", lambda secondary=secondary: self._step_subtitle_position(5, secondary))
        menu.addAction("重置", lambda secondary=secondary: self._set_subtitle_position_from_menu(50, secondary))
        return menu

    def _set_subtitle_position_from_menu(self, value: int, secondary: bool) -> None:
        clamped = max(0, min(int(value), 100))
        try:
            if secondary:
                self.video.set_secondary_subtitle_position(clamped)
                self._secondary_subtitle_position = clamped
            else:
                self.video.set_subtitle_position(clamped)
                self._main_subtitle_position = clamped
        except Exception as exc:
            label = "次字幕位置设置失败" if secondary else "主字幕位置设置失败"
            self._append_log(f"{label}: {exc}")

    def _step_subtitle_position(self, delta: int, secondary: bool) -> None:
        current = self._secondary_subtitle_position if secondary else self._main_subtitle_position
        self._set_subtitle_position_from_menu(current + delta, secondary)
```

- [ ] **Step 3: Refresh position state from the video layer during session open**

Inside `_refresh_subtitle_state()`, after `self._populate_subtitle_combo(self._subtitle_tracks)`, add:

```python
        if hasattr(self.video, "subtitle_position"):
            self._main_subtitle_position = self.video.subtitle_position()
        if hasattr(self.video, "secondary_subtitle_position"):
            self._secondary_subtitle_position = self.video.secondary_subtitle_position()
```

Inside the successful branch of `_refresh_subtitle_state()`, after `_apply_subtitle_preference()`, add:

```python
            if hasattr(self.video, "apply_secondary_subtitle_mode"):
                self._apply_secondary_subtitle_preference()
            if hasattr(self.video, "set_subtitle_position"):
                self.video.set_subtitle_position(self._main_subtitle_position)
            if hasattr(self.video, "set_secondary_subtitle_position"):
                self.video.set_secondary_subtitle_position(self._secondary_subtitle_position)
```

- [ ] **Step 4: Run the focused position and reuse tests to verify they pass**

Run:

```bash
uv run pytest \
  tests/test_player_window_ui.py::test_player_window_context_menu_position_actions_update_video_layer \
  tests/test_player_window_ui.py::test_player_window_reuses_secondary_subtitle_and_position_preferences_for_next_episode \
  tests/test_player_window_ui.py::test_player_window_logs_and_recovers_when_secondary_subtitle_or_position_apply_fails \
  -q
```

Expected: PASS.

- [ ] **Step 5: Run the full related regression suite**

Run:

```bash
uv run pytest tests/test_mpv_widget.py -q
uv run pytest tests/test_player_window_ui.py -k "subtitle or audio or context_menu" -q
```

Expected: PASS for both commands.

- [ ] **Step 6: Commit the reuse and recovery implementation**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: persist context menu track choices in session"
```

## Self-Review

- Spec coverage:
  - Video-area right-click menu: covered by Task 3 and Task 4.
  - Primary subtitle, secondary subtitle, audio track actions: covered by Task 3 and Task 4.
  - Independent primary and secondary subtitle positions with presets and step actions: covered by Task 5 and Task 6.
  - Session-only carry-forward across episodes: covered by Task 5 and Task 6.
  - Non-fatal logging and safe recovery: covered by Task 5 and Task 6.
  - Existing bottom-bar primary subtitle and audio selectors remain synchronized: covered by Task 3 and Task 4.
- Placeholder scan:
  - No `TODO`, `TBD`, or deferred “implement later” text remains.
  - Every code-changing step includes concrete code or a concrete command.
- Type consistency:
  - The plan consistently uses `SecondarySubtitlePreference`, `_build_video_context_menu()`, `apply_secondary_subtitle_mode()`, `subtitle_position()`, `set_subtitle_position()`, `secondary_subtitle_position()`, and `set_secondary_subtitle_position()`.
