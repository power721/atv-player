# Subtitle Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an embedded-subtitle selector to the player window so users can choose automatic Chinese-preferred subtitles, disable subtitles, or pick a specific internal subtitle track and keep that preference within the current playback session.

**Architecture:** Keep the work localized to `MpvWidget` and `PlayerWindow`. `MpvWidget` owns subtitle-track parsing and the narrow mpv-facing operations for auto/off/track selection; `PlayerWindow` owns the bottom-bar combo box, session-scoped subtitle preference, per-item refresh, cross-episode matching, and UI/log fallback behavior.

**Tech Stack:** Python 3.12, PySide6 widgets, pytest, pytest-qt, python-mpv

---

## File Structure

- `src/atv_player/player/mpv_widget.py`
  - Owns embedded subtitle track parsing, human-readable labels, Chinese-track detection, and mpv subtitle mode application.
- `src/atv_player/ui/player_window.py`
  - Owns the subtitle combo box, session-level subtitle preference state, UI refresh on item changes, cross-episode matching, and playback log fallback behavior.
- `tests/test_mpv_widget.py`
  - Owns focused unit tests for `MpvWidget` subtitle-track parsing and subtitle mode application.
- `tests/test_player_window_ui.py`
  - Owns focused UI tests for subtitle selector layout, item population, user-triggered selection, session-level preference reuse, and safe fallback behavior.

### Task 1: Add mpv Subtitle Track Parsing And Subtitle Mode Application

**Files:**
- Modify: `src/atv_player/player/mpv_widget.py`
- Modify: `tests/test_mpv_widget.py`
- Test: `tests/test_mpv_widget.py`

- [ ] **Step 1: Write the failing mpv subtitle tests**

Update `tests/test_mpv_widget.py` imports and add the subtitle tests after the existing playback-finished test:

```python
import sys
import types

from atv_player.player.mpv_widget import MpvWidget, SubtitleTrack
```

```python
def test_mpv_widget_lists_embedded_subtitle_tracks_with_readable_labels(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = types.SimpleNamespace(
        track_list=[
            {"id": 1, "type": "sub", "lang": "zh", "title": "", "default": True, "forced": False, "external": False},
            {"id": 2, "type": "sub", "lang": "eng", "title": "Signs", "default": False, "forced": True, "external": False},
            {"id": 3, "type": "audio", "lang": "ja", "title": "", "default": False, "forced": False, "external": False},
            {"id": 4, "type": "sub", "lang": "zho", "title": "外挂", "default": False, "forced": False, "external": True},
        ]
    )

    assert widget.subtitle_tracks() == [
        SubtitleTrack(id=1, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
        SubtitleTrack(id=2, title="Signs", lang="eng", is_default=False, is_forced=True, label="Signs (强制)"),
    ]


def test_mpv_widget_auto_mode_prefers_chinese_embedded_subtitles(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        sid="auto",
        track_list=[
            {"id": 3, "type": "sub", "lang": "eng", "title": "English", "default": False, "forced": False, "external": False},
            {"id": 5, "type": "sub", "lang": "chi", "title": "", "default": True, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_subtitle_mode("auto")

    assert applied_track_id == 5
    assert player.sid == 5


def test_mpv_widget_auto_mode_falls_back_to_mpv_default_without_chinese_tracks(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        sid=7,
        track_list=[
            {"id": 7, "type": "sub", "lang": "eng", "title": "English", "default": False, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_subtitle_mode("auto")

    assert applied_track_id is None
    assert player.sid == "auto"


def test_mpv_widget_can_disable_or_select_a_specific_embedded_subtitle_track(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(sid="auto", track_list=[])
    widget._player = player

    disabled_track_id = widget.apply_subtitle_mode("off")
    selected_track_id = widget.apply_subtitle_mode("track", track_id=9)

    assert disabled_track_id is None
    assert selected_track_id == 9
    assert player.sid == 9
```

- [ ] **Step 2: Run the focused mpv tests to verify they fail**

Run:

```bash
uv run pytest tests/test_mpv_widget.py::test_mpv_widget_lists_embedded_subtitle_tracks_with_readable_labels tests/test_mpv_widget.py::test_mpv_widget_auto_mode_prefers_chinese_embedded_subtitles tests/test_mpv_widget.py::test_mpv_widget_auto_mode_falls_back_to_mpv_default_without_chinese_tracks tests/test_mpv_widget.py::test_mpv_widget_can_disable_or_select_a_specific_embedded_subtitle_track -q
```

Expected: FAIL because `SubtitleTrack`, `subtitle_tracks()`, and `apply_subtitle_mode()` do not exist yet.

- [ ] **Step 3: Write the minimal mpv subtitle implementation**

Add the dataclass import in `src/atv_player/player/mpv_widget.py`:

```python
from dataclasses import dataclass
```

Add the new dataclass above `class MpvWidget(QWidget):`:

```python
@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    id: int
    title: str
    lang: str
    is_default: bool
    is_forced: bool
    label: str
```

Add these helpers and public methods to `MpvWidget`:

```python
    def _subtitle_language_label(self, lang: str) -> str:
        normalized = lang.strip().lower()
        return {
            "zh": "中文",
            "chi": "中文",
            "zho": "中文",
            "en": "English",
            "eng": "English",
            "ja": "日本語",
            "jpn": "日本語",
        }.get(normalized, normalized or "")

    def _subtitle_track_label(self, title: str, lang: str, is_default: bool, is_forced: bool, index: int) -> str:
        base = title.strip() or self._subtitle_language_label(lang) or f"字幕 {index}"
        suffixes = []
        if is_default:
            suffixes.append("默认")
        if is_forced:
            suffixes.append("强制")
        if not suffixes:
            return base
        return f"{base} ({'/'.join(suffixes)})"

    def _is_chinese_subtitle_track(self, track: SubtitleTrack) -> bool:
        if track.lang in {"zh", "chi", "zho"}:
            return True
        lowered_title = track.title.casefold()
        return any(token in lowered_title for token in ("中文", "简中", "繁中", "中字", "chinese"))

    def subtitle_tracks(self) -> list[SubtitleTrack]:
        if self._player is None:
            return []
        try:
            raw_tracks = getattr(self._player, "track_list", None) or []
        except Exception:
            return []

        tracks: list[SubtitleTrack] = []
        for index, raw_track in enumerate(raw_tracks, start=1):
            if raw_track.get("type") != "sub" or raw_track.get("external"):
                continue
            track_id = int(raw_track["id"])
            title = str(raw_track.get("title") or "").strip()
            lang = str(raw_track.get("lang") or "").strip().lower()
            is_default = bool(raw_track.get("default"))
            is_forced = bool(raw_track.get("forced"))
            tracks.append(
                SubtitleTrack(
                    id=track_id,
                    title=title,
                    lang=lang,
                    is_default=is_default,
                    is_forced=is_forced,
                    label=self._subtitle_track_label(title, lang, is_default, is_forced, len(tracks) + 1),
                )
            )
        return tracks

    def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
        if self._player is None:
            return None
        try:
            if mode == "off":
                self._player.sid = "no"
                return None
            if mode == "track" and track_id is not None:
                self._player.sid = track_id
                return track_id
            preferred_track = next((track for track in self.subtitle_tracks() if self._is_chinese_subtitle_track(track)), None)
            if preferred_track is not None:
                self._player.sid = preferred_track.id
                return preferred_track.id
            self._player.sid = "auto"
            return None
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return None
            raise
```

- [ ] **Step 4: Run the focused mpv tests to verify they pass**

Run:

```bash
uv run pytest tests/test_mpv_widget.py::test_mpv_widget_lists_embedded_subtitle_tracks_with_readable_labels tests/test_mpv_widget.py::test_mpv_widget_auto_mode_prefers_chinese_embedded_subtitles tests/test_mpv_widget.py::test_mpv_widget_auto_mode_falls_back_to_mpv_default_without_chinese_tracks tests/test_mpv_widget.py::test_mpv_widget_can_disable_or_select_a_specific_embedded_subtitle_track -q
```

Expected: PASS with readable embedded subtitle tracks and stable `auto` / `off` / `track` behavior.

- [ ] **Step 5: Commit**

```bash
git add tests/test_mpv_widget.py src/atv_player/player/mpv_widget.py
git commit -m "feat: add mpv subtitle selection primitives"
```

### Task 2: Add The Bottom-Bar Subtitle Selector And Current-Item Refresh

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing player window subtitle UI tests**

Update the `tests/test_player_window_ui.py` imports:

```python
from atv_player.player.mpv_widget import SubtitleTrack
```

Add these tests after `test_player_window_exposes_extended_playback_controls`:

```python
def test_player_window_exposes_subtitle_combo_with_default_auto_entry(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert isinstance(window.subtitle_combo, QComboBox)
    assert window.subtitle_combo.count() == 1
    assert window.subtitle_combo.itemText(0) == "自动选择"
    assert window.subtitle_combo.isEnabled() is False


def test_player_window_populates_embedded_subtitle_options_after_open_session(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [
                SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
            ]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return 11 if mode == "auto" else track_id

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    assert [window.subtitle_combo.itemText(index) for index in range(window.subtitle_combo.count())] == [
        "自动选择",
        "关闭字幕",
        "中文 (默认)",
        "English",
    ]
    assert window.subtitle_combo.isEnabled() is True
    assert window.video.subtitle_apply_calls[0] == ("auto", None)


def test_player_window_disables_subtitle_selector_when_current_item_has_no_embedded_subtitles(qtbot) -> None:
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

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    assert window.subtitle_combo.count() == 1
    assert window.subtitle_combo.itemText(0) == "自动选择"
    assert window.subtitle_combo.isEnabled() is False


def test_player_window_user_selection_applies_selected_subtitle_track(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

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

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.video.subtitle_apply_calls.clear()

    window.subtitle_combo.setCurrentIndex(3)

    assert window.video.subtitle_apply_calls == [("track", 12)]
```

- [ ] **Step 2: Run the focused player window subtitle tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_exposes_subtitle_combo_with_default_auto_entry tests/test_player_window_ui.py::test_player_window_populates_embedded_subtitle_options_after_open_session tests/test_player_window_ui.py::test_player_window_disables_subtitle_selector_when_current_item_has_no_embedded_subtitles tests/test_player_window_ui.py::test_player_window_user_selection_applies_selected_subtitle_track -q
```

Expected: FAIL because `PlayerWindow` does not yet define `subtitle_combo`, refresh subtitle options on load, or react to subtitle selection changes.

- [ ] **Step 3: Write the minimal player window subtitle UI implementation**

Update the imports at the top of `src/atv_player/ui/player_window.py`:

```python
from dataclasses import dataclass
```

```python
from atv_player.player.mpv_widget import MpvWidget, SubtitleTrack
```

Add the subtitle preference dataclass above `class PlayerWindow(QWidget):`:

```python
@dataclass(slots=True)
class SubtitlePreference:
    mode: str = "auto"
    title: str = ""
    lang: str = ""
    is_default: bool = False
    is_forced: bool = False
```

Initialize subtitle state in `PlayerWindow.__init__` right after speed controls:

```python
        self._subtitle_tracks: list[SubtitleTrack] = []
        self._subtitle_preference = SubtitlePreference()
        self.subtitle_combo = QComboBox()
        self.subtitle_combo.addItem("自动选择", ("auto", None))
        self.subtitle_combo.setEnabled(False)
```

Insert the combo into the bottom control group after `self.speed_combo`:

```python
        control_group_layout.addWidget(self.speed_combo)
        control_group_layout.addWidget(self.subtitle_combo)
        control_group_layout.addWidget(self.opening_spin)
```

Connect the selector in `__init__`:

```python
        self.subtitle_combo.currentIndexChanged.connect(self._change_subtitle_selection)
```

Add these helpers to `PlayerWindow`:

```python
    def _reset_subtitle_combo(self) -> None:
        self.subtitle_combo.blockSignals(True)
        self.subtitle_combo.clear()
        self.subtitle_combo.addItem("自动选择", ("auto", None))
        self.subtitle_combo.setCurrentIndex(0)
        self.subtitle_combo.setEnabled(False)
        self.subtitle_combo.blockSignals(False)

    def _remember_track_preference(self, track: SubtitleTrack) -> None:
        self._subtitle_preference = SubtitlePreference(
            mode="track",
            title=track.title,
            lang=track.lang,
            is_default=track.is_default,
            is_forced=track.is_forced,
        )

    def _populate_subtitle_combo(self, tracks: list[SubtitleTrack]) -> None:
        self.subtitle_combo.blockSignals(True)
        self.subtitle_combo.clear()
        self.subtitle_combo.addItem("自动选择", ("auto", None))
        if tracks:
            self.subtitle_combo.addItem("关闭字幕", ("off", None))
            for track in tracks:
                self.subtitle_combo.addItem(track.label, ("track", track.id))
        self.subtitle_combo.setEnabled(bool(tracks))
        self.subtitle_combo.setCurrentIndex(0)
        self.subtitle_combo.blockSignals(False)

    def _apply_subtitle_preference(self) -> None:
        mode = self._subtitle_preference.mode
        track_id = None
        if mode == "track":
            for track in self._subtitle_tracks:
                if track.title == self._subtitle_preference.title and track.lang == self._subtitle_preference.lang:
                    track_id = track.id
                    break
            if track_id is None and self._subtitle_tracks:
                track_id = self._subtitle_tracks[0].id
        applied_track_id = self.video.apply_subtitle_mode(mode if track_id is not None else "auto", track_id=track_id)
        if applied_track_id is None:
            self.subtitle_combo.setCurrentIndex(0)
            if mode != "off":
                self._subtitle_preference = SubtitlePreference()
            return
        for index, track in enumerate(self._subtitle_tracks, start=2):
            if track.id == applied_track_id:
                self.subtitle_combo.setCurrentIndex(index)
                return

    def _refresh_subtitle_state(self) -> None:
        if not hasattr(self.video, "subtitle_tracks") or not hasattr(self.video, "apply_subtitle_mode"):
            self._subtitle_tracks = []
            self._reset_subtitle_combo()
            return
        self._subtitle_tracks = self.video.subtitle_tracks()
        self._populate_subtitle_combo(self._subtitle_tracks)
        if not self._subtitle_tracks:
            self._subtitle_preference = SubtitlePreference()
            return
        self._apply_subtitle_preference()

    def _change_subtitle_selection(self, index: int) -> None:
        if index < 0:
            return
        mode, track_id = self.subtitle_combo.itemData(index)
        if mode == "auto":
            self._subtitle_preference = SubtitlePreference()
            self.video.apply_subtitle_mode("auto")
            return
        if mode == "off":
            self._subtitle_preference = SubtitlePreference(mode="off")
            self.video.apply_subtitle_mode("off")
            return
        track = next(track for track in self._subtitle_tracks if track.id == track_id)
        self._remember_track_preference(track)
        self.video.apply_subtitle_mode("track", track_id=track_id)
```

Call `_reset_subtitle_combo()` in `open_session()` before `_load_current_item()` and `_refresh_subtitle_state()` at the end of `_load_current_item()`:

```python
        self._reset_subtitle_combo()
        self._load_current_item(session.start_position_seconds, pause=start_paused)
```

```python
            self.video.load(current_item.url, pause=pause, start_seconds=effective_start_seconds)
            self.video.set_speed(self.current_speed)
            self.video.set_volume(self.volume_slider.value())
            self._refresh_subtitle_state()
```

- [ ] **Step 4: Run the focused player window subtitle tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_exposes_subtitle_combo_with_default_auto_entry tests/test_player_window_ui.py::test_player_window_populates_embedded_subtitle_options_after_open_session tests/test_player_window_ui.py::test_player_window_disables_subtitle_selector_when_current_item_has_no_embedded_subtitles tests/test_player_window_ui.py::test_player_window_user_selection_applies_selected_subtitle_track -q
```

Expected: PASS with a disabled default selector, track population after opening a session, and immediate subtitle mode changes when the user picks an item.

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "feat: add player subtitle selector"
```

### Task 3: Reuse Subtitle Preference Across Episodes And Add Safe Fallback Logging

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing cross-episode and fallback tests**

Add these tests after `test_player_window_user_selection_applies_selected_subtitle_track`:

```python
def test_player_window_reuses_subtitle_track_preference_for_next_episode(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.current_url = ""
            self.subtitle_apply_calls: list[tuple[str, str, int | None]] = []
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

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((self.current_url, mode, track_id))
            return track_id if mode == "track" else None

        def position_seconds(self) -> int:
            return 30

        def duration_seconds(self) -> int:
            return 120

    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.subtitle_combo.setCurrentIndex(3)
    window.video.subtitle_apply_calls.clear()

    window.play_next()

    assert ("http://m/2.m3u8", "track", 22) in window.video.subtitle_apply_calls
    assert window.subtitle_combo.currentText() == "English"


def test_player_window_falls_back_to_auto_when_previous_track_cannot_be_matched(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.current_url = ""
            self.subtitle_apply_calls: list[tuple[str, str, int | None]] = []
            self.tracks_by_url = {
                "http://m/1.m3u8": [
                    SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                    SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
                ],
                "http://m/2.m3u8": [
                    SubtitleTrack(id=21, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
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

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((self.current_url, mode, track_id))
            return 21 if mode == "auto" else track_id

        def position_seconds(self) -> int:
            return 30

        def duration_seconds(self) -> int:
            return 120

    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.subtitle_combo.setCurrentIndex(3)
    window.video.subtitle_apply_calls.clear()

    window.play_next()

    assert ("http://m/2.m3u8", "auto", None) in window.video.subtitle_apply_calls
    assert window.subtitle_combo.currentText() == "自动选择"


def test_player_window_logs_and_resets_when_subtitle_refresh_fails(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            raise RuntimeError("boom")

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    assert "字幕加载失败: boom" in window.log_view.toPlainText()
    assert window.subtitle_combo.count() == 1
    assert window.subtitle_combo.itemText(0) == "自动选择"
    assert window.subtitle_combo.isEnabled() is False
```

- [ ] **Step 2: Run the focused cross-episode and fallback tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_reuses_subtitle_track_preference_for_next_episode tests/test_player_window_ui.py::test_player_window_falls_back_to_auto_when_previous_track_cannot_be_matched tests/test_player_window_ui.py::test_player_window_logs_and_resets_when_subtitle_refresh_fails -q
```

Expected: FAIL because `PlayerWindow` does not yet match subtitle preference by metadata across episodes or log and reset safely when subtitle refresh raises.

- [ ] **Step 3: Implement preference matching and fallback behavior**

Replace the simplistic track matching in `src/atv_player/ui/player_window.py` with explicit scoring helpers:

```python
    def _subtitle_track_match_score(self, track: SubtitleTrack, preference: SubtitlePreference) -> tuple[int, int, int]:
        return (
            int(bool(preference.title) and track.title == preference.title),
            int(bool(preference.lang) and track.lang == preference.lang),
            int(track.is_forced == preference.is_forced and track.is_default == preference.is_default),
        )

    def _matching_track_for_preference(self) -> SubtitleTrack | None:
        if self._subtitle_preference.mode != "track" or not self._subtitle_tracks:
            return None
        ranked_tracks = sorted(
            self._subtitle_tracks,
            key=lambda track: self._subtitle_track_match_score(track, self._subtitle_preference),
            reverse=True,
        )
        best_track = ranked_tracks[0]
        if self._subtitle_track_match_score(best_track, self._subtitle_preference) == (0, 0, 0):
            return None
        return best_track
```

Update `_apply_subtitle_preference()` to use the matching helper and safe fallback:

```python
    def _apply_subtitle_preference(self) -> None:
        self.subtitle_combo.blockSignals(True)
        try:
            if self._subtitle_preference.mode == "off":
                self.video.apply_subtitle_mode("off")
                if self.subtitle_combo.count() > 1:
                    self.subtitle_combo.setCurrentIndex(1)
                return

            if self._subtitle_preference.mode == "track":
                matched_track = self._matching_track_for_preference()
                if matched_track is not None:
                    applied_track_id = self.video.apply_subtitle_mode("track", track_id=matched_track.id)
                    for index, track in enumerate(self._subtitle_tracks, start=2):
                        if track.id == applied_track_id:
                            self.subtitle_combo.setCurrentIndex(index)
                            return
                self._subtitle_preference = SubtitlePreference()

            applied_track_id = self.video.apply_subtitle_mode("auto")
            self.subtitle_combo.setCurrentIndex(0)
            if applied_track_id is not None:
                for index, track in enumerate(self._subtitle_tracks, start=2):
                    if track.id == applied_track_id:
                        self.subtitle_combo.setCurrentIndex(index)
                        break
        finally:
            self.subtitle_combo.blockSignals(False)
```

Wrap `_refresh_subtitle_state()` with safe logging and reset:

```python
    def _refresh_subtitle_state(self) -> None:
        if not hasattr(self.video, "subtitle_tracks") or not hasattr(self.video, "apply_subtitle_mode"):
            self._subtitle_tracks = []
            self._subtitle_preference = SubtitlePreference()
            self._reset_subtitle_combo()
            return
        try:
            self._subtitle_tracks = self.video.subtitle_tracks()
        except Exception as exc:
            self._subtitle_tracks = []
            self._subtitle_preference = SubtitlePreference()
            self._reset_subtitle_combo()
            self._append_log(f"字幕加载失败: {exc}")
            return
        self._populate_subtitle_combo(self._subtitle_tracks)
        if not self._subtitle_tracks:
            self._subtitle_preference = SubtitlePreference()
            return
        try:
            self._apply_subtitle_preference()
        except Exception as exc:
            self._subtitle_preference = SubtitlePreference()
            self._reset_subtitle_combo()
            self._append_log(f"字幕切换失败: {exc}")
```

- [ ] **Step 4: Run the focused cross-episode and fallback tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_reuses_subtitle_track_preference_for_next_episode tests/test_player_window_ui.py::test_player_window_falls_back_to_auto_when_previous_track_cannot_be_matched tests/test_player_window_ui.py::test_player_window_logs_and_resets_when_subtitle_refresh_fails -q
```

Expected: PASS with track preference reuse by metadata, auto fallback when a matching track disappears, and safe log/reset behavior on refresh errors.

- [ ] **Step 5: Run the full focused regression suite**

Run:

```bash
uv run pytest tests/test_mpv_widget.py tests/test_player_window_ui.py -q
```

Expected: PASS with all subtitle tests and existing player window regressions green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py tests/test_mpv_widget.py src/atv_player/player/mpv_widget.py
git commit -m "feat: reuse subtitle selection across episodes"
```
