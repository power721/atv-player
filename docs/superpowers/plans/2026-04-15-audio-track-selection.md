# Audio Track Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an embedded-audio selector to the player window so users can choose automatic Chinese- or Mandarin-preferred audio, pick a specific internal audio track, and keep that preference within the current playback session.

**Architecture:** Keep the work localized to `MpvWidget` and `PlayerWindow`. `MpvWidget` owns audio-track parsing, label generation, preferred-track detection, and the narrow mpv-facing operations for `auto` and explicit track selection; `PlayerWindow` owns the bottom-bar combo box, session-scoped audio preference, per-item refresh, cross-episode matching, and UI/log fallback behavior.

**Tech Stack:** Python 3.12, PySide6 widgets, pytest, pytest-qt, python-mpv

---

## File Structure

- `src/atv_player/player/mpv_widget.py`
  - Extend the existing mpv wrapper with embedded audio-track metadata, preferred-audio selection, and a dedicated `audio_tracks_changed` signal.
- `src/atv_player/ui/player_window.py`
  - Add the audio combo box, session-level audio preference state, refresh hooks, cross-episode matching, and playback-log fallback behavior.
- `tests/test_mpv_widget.py`
  - Add focused unit tests for `MpvWidget` audio-track parsing, preferred-audio selection, explicit track selection, and track-list signal emission.
- `tests/test_player_window_ui.py`
  - Add focused UI tests for audio selector presence, option population, user-triggered selection, session-level preference reuse, post-load refresh, and safe fallback behavior.

### Task 1: Add mpv Audio Track Parsing And Audio Mode Application

**Files:**
- Modify: `src/atv_player/player/mpv_widget.py:10-23`
- Modify: `src/atv_player/player/mpv_widget.py:66-91`
- Modify: `src/atv_player/player/mpv_widget.py:217-293`
- Modify: `tests/test_mpv_widget.py`
- Test: `tests/test_mpv_widget.py`

- [ ] **Step 1: Write the failing mpv audio tests**

Add `AudioTrack` to the imports in `tests/test_mpv_widget.py`:

```python
from atv_player.player.mpv_widget import AudioTrack, MpvWidget, SubtitleTrack
```

Append these tests after the existing subtitle tests:

```python
def test_mpv_widget_emits_audio_tracks_changed_when_mpv_track_list_updates(qtbot, monkeypatch) -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.play_calls: list[str] = []
            self.pause = False
            self._track_list_observer = None

        def event_callback(self, *event_types):
            def register(callback):
                return callback

            return register

        def observe_property(self, name: str, handler) -> None:
            assert name == "track-list"
            self._track_list_observer = handler

        def play(self, url: str) -> None:
            self.play_calls.append(url)

    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = FakePlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: player)
    changes = {"count": 0}
    widget.audio_tracks_changed.connect(lambda: changes.__setitem__("count", changes["count"] + 1))

    widget.load("http://m/1.m3u8")
    player._track_list_observer("track-list", [{"id": 1, "type": "audio"}])

    assert player.play_calls == ["http://m/1.m3u8"]
    assert changes["count"] == 1


def test_mpv_widget_lists_embedded_audio_tracks_with_readable_labels(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = types.SimpleNamespace(
        track_list=[
            {"id": 1, "type": "audio", "lang": "cmn", "title": "", "default": True, "forced": False, "external": False},
            {"id": 2, "type": "audio", "lang": "eng", "title": "English Dub", "default": False, "forced": False, "external": False},
            {"id": 3, "type": "sub", "lang": "zh", "title": "", "default": True, "forced": False, "external": False},
            {"id": 4, "type": "audio", "lang": "jpn", "title": "", "default": False, "forced": False, "external": True},
        ]
    )

    assert widget.audio_tracks() == [
        AudioTrack(id=1, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
        AudioTrack(id=2, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
    ]


def test_mpv_widget_auto_mode_prefers_chinese_or_mandarin_audio(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        aid="auto",
        track_list=[
            {"id": 3, "type": "audio", "lang": "eng", "title": "English", "default": True, "forced": False, "external": False},
            {"id": 5, "type": "audio", "lang": "cmn", "title": "", "default": False, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_audio_mode("auto")

    assert applied_track_id == 5
    assert player.aid == 5


def test_mpv_widget_auto_mode_falls_back_to_mpv_default_without_preferred_audio(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(
        aid=7,
        track_list=[
            {"id": 7, "type": "audio", "lang": "eng", "title": "English", "default": False, "forced": False, "external": False},
        ],
    )
    widget._player = player

    applied_track_id = widget.apply_audio_mode("auto")

    assert applied_track_id is None
    assert player.aid == "auto"


def test_mpv_widget_can_select_a_specific_embedded_audio_track(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    player = types.SimpleNamespace(aid="auto", track_list=[])
    widget._player = player

    selected_track_id = widget.apply_audio_mode("track", track_id=9)

    assert selected_track_id == 9
    assert player.aid == 9
```

- [ ] **Step 2: Run the focused mpv audio tests to verify they fail**

Run:

```bash
uv run pytest tests/test_mpv_widget.py::test_mpv_widget_emits_audio_tracks_changed_when_mpv_track_list_updates tests/test_mpv_widget.py::test_mpv_widget_lists_embedded_audio_tracks_with_readable_labels tests/test_mpv_widget.py::test_mpv_widget_auto_mode_prefers_chinese_or_mandarin_audio tests/test_mpv_widget.py::test_mpv_widget_auto_mode_falls_back_to_mpv_default_without_preferred_audio tests/test_mpv_widget.py::test_mpv_widget_can_select_a_specific_embedded_audio_track -q
```

Expected: FAIL because `AudioTrack`, `audio_tracks_changed`, `audio_tracks()`, and `apply_audio_mode()` do not exist yet.

- [ ] **Step 3: Write the minimal mpv audio implementation**

Add the new dataclass beside `SubtitleTrack` in `src/atv_player/player/mpv_widget.py`:

```python
@dataclass(frozen=True, slots=True)
class AudioTrack:
    id: int
    title: str
    lang: str
    is_default: bool
    is_forced: bool
    label: str
```

Extend the signal declarations in `MpvWidget`:

```python
class MpvWidget(QWidget):
    double_clicked = Signal()
    playback_finished = Signal()
    subtitle_tracks_changed = Signal()
    audio_tracks_changed = Signal()
```

Update the `track-list` observer to emit both track signals:

```python
        def handle_track_list(_property_name, _tracks) -> None:
            self.subtitle_tracks_changed.emit()
            self.audio_tracks_changed.emit()
```

Add audio helpers below the subtitle helpers:

```python
    def _audio_language_label(self, lang: str) -> str:
        normalized = lang.strip().lower()
        return {
            "zh": "中文",
            "chi": "中文",
            "zho": "中文",
            "cmn": "国语",
            "en": "English",
            "eng": "English",
            "ja": "日语",
            "jpn": "日语",
        }.get(normalized, normalized or "")

    def _audio_track_label(self, title: str, lang: str, is_default: bool, is_forced: bool, index: int) -> str:
        base = title.strip() or self._audio_language_label(lang) or f"音轨 {index}"
        suffixes = []
        if is_default:
            suffixes.append("默认")
        if is_forced:
            suffixes.append("强制")
        if not suffixes:
            return base
        return f"{base} ({'/'.join(suffixes)})"

    def _is_preferred_audio_track(self, track: AudioTrack) -> bool:
        if track.lang in {"zh", "chi", "zho", "cmn"}:
            return True
        lowered_title = track.title.casefold()
        return any(token in lowered_title for token in ("中文", "国语", "普通话", "mandarin", "chinese"))

    def audio_tracks(self) -> list[AudioTrack]:
        if self._player is None:
            return []
        try:
            raw_tracks = getattr(self._player, "track_list", None) or []
        except Exception:
            return []

        tracks: list[AudioTrack] = []
        for raw_track in raw_tracks:
            if raw_track.get("type") != "audio" or raw_track.get("external"):
                continue
            title = str(raw_track.get("title") or "").strip()
            lang = str(raw_track.get("lang") or "").strip().lower()
            is_default = bool(raw_track.get("default"))
            is_forced = bool(raw_track.get("forced"))
            tracks.append(
                AudioTrack(
                    id=int(raw_track["id"]),
                    title=title,
                    lang=lang,
                    is_default=is_default,
                    is_forced=is_forced,
                    label=self._audio_track_label(title, lang, is_default, is_forced, len(tracks) + 1),
                )
            )
        return tracks

    def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
        if self._player is None:
            return None
        try:
            if mode == "track" and track_id is not None:
                self._player.aid = track_id
                return track_id
            preferred_track = next((track for track in self.audio_tracks() if self._is_preferred_audio_track(track)), None)
            if preferred_track is not None:
                self._player.aid = preferred_track.id
                return preferred_track.id
            self._player.aid = "auto"
            return None
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return None
            raise
```

- [ ] **Step 4: Run the focused mpv audio tests to verify they pass**

Run:

```bash
uv run pytest tests/test_mpv_widget.py::test_mpv_widget_emits_audio_tracks_changed_when_mpv_track_list_updates tests/test_mpv_widget.py::test_mpv_widget_lists_embedded_audio_tracks_with_readable_labels tests/test_mpv_widget.py::test_mpv_widget_auto_mode_prefers_chinese_or_mandarin_audio tests/test_mpv_widget.py::test_mpv_widget_auto_mode_falls_back_to_mpv_default_without_preferred_audio tests/test_mpv_widget.py::test_mpv_widget_can_select_a_specific_embedded_audio_track -q
```

Expected: PASS with readable embedded audio tracks, stable `auto` / `track` behavior, and an emitted audio-track refresh signal.

- [ ] **Step 5: Commit**

```bash
git add tests/test_mpv_widget.py src/atv_player/player/mpv_widget.py
git commit -m "feat: add mpv audio selection primitives"
```

### Task 2: Add The Player-Window Audio Selector And Basic Selection Wiring

**Files:**
- Modify: `src/atv_player/ui/player_window.py:69-83`
- Modify: `src/atv_player/ui/player_window.py:148-157`
- Modify: `src/atv_player/ui/player_window.py:245-246`
- Modify: `src/atv_player/ui/player_window.py:304-315`
- Modify: `src/atv_player/ui/player_window.py:492-512`
- Modify: `tests/test_player_window_ui.py:1100-1224`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing player-window audio UI tests**

Update the imports in `tests/test_player_window_ui.py`:

```python
from atv_player.player.mpv_widget import AudioTrack, SubtitleTrack
```

Append these tests after `test_player_window_exposes_subtitle_combo_with_default_auto_entry`:

```python
def test_player_window_exposes_audio_combo_with_default_auto_entry(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert isinstance(window.audio_combo, QComboBox)
    assert window.audio_combo.count() == 1
    assert window.audio_combo.itemText(0) == "自动选择"
    assert window.audio_combo.isEnabled() is False


def test_player_window_populates_embedded_audio_options_after_open_session(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []
            self.audio_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return [
                AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
            ]

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((mode, track_id))
            return 31 if mode == "auto" else track_id

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    assert [window.audio_combo.itemText(index) for index in range(window.audio_combo.count())] == [
        "自动选择",
        "国语 (默认)",
        "English Dub",
    ]
    assert window.audio_combo.isEnabled() is True
    assert window.video.audio_apply_calls[0] == ("auto", None)


def test_player_window_disables_audio_selector_when_current_item_has_no_embedded_audio_options(qtbot) -> None:
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

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    assert window.audio_combo.count() == 1
    assert window.audio_combo.itemText(0) == "自动选择"
    assert window.audio_combo.isEnabled() is False


def test_player_window_user_selection_applies_selected_audio_track(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.audio_apply_calls: list[tuple[str, int | None]] = []

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
            return [
                AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
            ]

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((mode, track_id))
            return track_id

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.video.audio_apply_calls.clear()

    window.audio_combo.setCurrentIndex(2)

    assert window.video.audio_apply_calls == [("track", 32)]
```

- [ ] **Step 2: Run the focused player-window audio UI tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_exposes_audio_combo_with_default_auto_entry tests/test_player_window_ui.py::test_player_window_populates_embedded_audio_options_after_open_session tests/test_player_window_ui.py::test_player_window_disables_audio_selector_when_current_item_has_no_embedded_audio_options tests/test_player_window_ui.py::test_player_window_user_selection_applies_selected_audio_track -q
```

Expected: FAIL because `audio_combo`, audio refresh, and audio selection handlers do not exist yet.

- [ ] **Step 3: Write the minimal player-window audio selector implementation**

Add `AudioTrack` to the imports and define a parallel preference dataclass near `SubtitlePreference`:

```python
from atv_player.player.mpv_widget import AudioTrack, MpvWidget, SubtitleTrack
```

```python
@dataclass(slots=True)
class AudioPreference:
    mode: str = "auto"
    title: str = ""
    lang: str = ""
    is_default: bool = False
    is_forced: bool = False
```

Initialize audio state and combo box in `PlayerWindow.__init__`:

```python
        self._subtitle_tracks: list[SubtitleTrack] = []
        self._subtitle_preference = SubtitlePreference()
        self.subtitle_combo = QComboBox()
        self.subtitle_combo.addItem("自动选择", ("auto", None))
        self.subtitle_combo.setEnabled(False)
        self._audio_tracks: list[AudioTrack] = []
        self._audio_preference = AudioPreference()
        self.audio_combo = QComboBox()
        self.audio_combo.addItem("自动选择", ("auto", None))
        self.audio_combo.setEnabled(False)
```

Insert the new combo beside the subtitle combo:

```python
        control_group_layout.addWidget(self.speed_combo)
        control_group_layout.addWidget(self.subtitle_combo)
        control_group_layout.addWidget(self.audio_combo)
        control_group_layout.addWidget(self.opening_spin)
```

Connect the selector and track-refresh signal:

```python
        self.subtitle_combo.currentIndexChanged.connect(self._change_subtitle_selection)
        self.audio_combo.currentIndexChanged.connect(self._change_audio_selection)
```

```python
        self.video_widget.subtitle_tracks_changed.connect(self._refresh_subtitle_state)
        self.video_widget.audio_tracks_changed.connect(self._refresh_audio_state)
```

Reset and refresh the audio state when opening or loading an item:

```python
        self.progress.setValue(0)
        self._reset_subtitle_combo()
        self._reset_audio_combo()
```

```python
        self.video.load(current_item.url, pause=pause, start_seconds=effective_start_seconds)
        self.video.set_speed(self.current_speed)
        self.video.set_volume(self.volume_slider.value())
        self._refresh_subtitle_state()
        self._refresh_audio_state()
```

Add the basic audio helpers after the subtitle helpers:

```python
    def _reset_audio_combo(self) -> None:
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        self.audio_combo.addItem("自动选择", ("auto", None))
        self.audio_combo.setCurrentIndex(0)
        self.audio_combo.setEnabled(False)
        self.audio_combo.blockSignals(False)

    def _populate_audio_combo(self, tracks: list[AudioTrack]) -> None:
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        self.audio_combo.addItem("自动选择", ("auto", None))
        for track in tracks:
            self.audio_combo.addItem(track.label, ("track", track.id))
        self.audio_combo.setEnabled(bool(tracks))
        self.audio_combo.setCurrentIndex(0)
        self.audio_combo.blockSignals(False)

    def _refresh_audio_state(self) -> None:
        if not hasattr(self.video, "audio_tracks") or not hasattr(self.video, "apply_audio_mode"):
            self._audio_tracks = []
            self._audio_preference = AudioPreference()
            self._reset_audio_combo()
            return
        self._audio_tracks = self.video.audio_tracks()
        self._populate_audio_combo(self._audio_tracks)
        if not self._audio_tracks:
            self._audio_preference = AudioPreference()
            return
        self.video.apply_audio_mode("auto")
        self.audio_combo.setCurrentIndex(0)

    def _change_audio_selection(self, index: int) -> None:
        if index < 0:
            return
        item_data = self.audio_combo.itemData(index)
        if item_data is None:
            return
        mode, track_id = item_data
        if mode == "auto":
            self._audio_preference = AudioPreference()
            self.video.apply_audio_mode("auto")
            return
        track = next(track for track in self._audio_tracks if track.id == track_id)
        self._audio_preference = AudioPreference(
            mode="track",
            title=track.title,
            lang=track.lang,
            is_default=track.is_default,
            is_forced=track.is_forced,
        )
        self.video.apply_audio_mode("track", track_id=track_id)
```

- [ ] **Step 4: Run the focused player-window audio UI tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_exposes_audio_combo_with_default_auto_entry tests/test_player_window_ui.py::test_player_window_populates_embedded_audio_options_after_open_session tests/test_player_window_ui.py::test_player_window_disables_audio_selector_when_current_item_has_no_embedded_audio_options tests/test_player_window_ui.py::test_player_window_user_selection_applies_selected_audio_track -q
```

Expected: PASS with the new bottom-bar audio selector visible, populated, and wired to the video-layer audio API.

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "feat: add player audio selector"
```

### Task 3: Reuse Audio Preference Across Episodes And Add Safe Refresh Recovery

**Files:**
- Modify: `src/atv_player/ui/player_window.py:783-881`
- Modify: `tests/test_player_window_ui.py:1227-1396`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing cross-episode and refresh-recovery tests**

Append these tests after `test_player_window_user_selection_applies_selected_audio_track`:

```python
def test_player_window_reuses_audio_track_preference_for_next_episode(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.current_url = ""
            self.audio_apply_calls: list[tuple[str, str, int | None]] = []
            self.tracks_by_url = {
                "http://m/1.m3u8": [
                    AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                    AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
                ],
                "http://m/2.m3u8": [
                    AudioTrack(id=41, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                    AudioTrack(id=42, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
                ],
            }

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.current_url = url

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return self.tracks_by_url[self.current_url]

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((self.current_url, mode, track_id))
            return track_id if mode == "track" else 41

        def position_seconds(self) -> int:
            return 30

        def duration_seconds(self) -> int:
            return 120

    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.audio_combo.setCurrentIndex(2)
    window.video.audio_apply_calls.clear()

    window.play_next()

    assert ("http://m/2.m3u8", "track", 42) in window.video.audio_apply_calls
    assert window.audio_combo.currentText() == "English Dub"


def test_player_window_falls_back_to_auto_when_previous_audio_track_cannot_be_matched(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.current_url = ""
            self.audio_apply_calls: list[tuple[str, str, int | None]] = []
            self.tracks_by_url = {
                "http://m/1.m3u8": [
                    AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                    AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
                ],
                "http://m/2.m3u8": [
                    AudioTrack(id=41, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                ],
            }
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.current_url = url

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return self.tracks_by_url[self.current_url]

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((self.current_url, mode, track_id))
            return 41 if mode == "auto" else track_id

        def position_seconds(self) -> int:
            return 30

        def duration_seconds(self) -> int:
            return 120

    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.audio_combo.setCurrentIndex(2)
    window.video.audio_apply_calls.clear()

    window.play_next()

    assert ("http://m/2.m3u8", "auto", None) in window.video.audio_apply_calls
    assert window.audio_combo.currentText() == "自动选择"


def test_player_window_logs_and_resets_when_audio_refresh_fails(qtbot) -> None:
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
            raise RuntimeError("boom")

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    assert "音轨加载失败: boom" in window.log_view.toPlainText()
    assert window.audio_combo.count() == 1
    assert window.audio_combo.itemText(0) == "自动选择"
    assert window.audio_combo.isEnabled() is False


def test_player_window_refreshes_audio_options_when_mpv_reports_tracks_after_load(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    load_calls: list[tuple[str, bool, int]] = []
    audio_apply_calls: list[tuple[str, int | None]] = []
    tracks_call_count = {"count": 0}

    def fake_audio_tracks() -> list[AudioTrack]:
        tracks_call_count["count"] += 1
        if tracks_call_count["count"] == 1:
            return []
        return [AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)")]

    window.video_widget.load = lambda url, pause=False, start_seconds=0: load_calls.append((url, pause, start_seconds))
    window.video_widget.set_speed = lambda speed: None
    window.video_widget.set_volume = lambda value: None
    window.video_widget.subtitle_tracks = lambda: []
    window.video_widget.apply_subtitle_mode = lambda mode, track_id=None: None
    window.video_widget.audio_tracks = fake_audio_tracks
    window.video_widget.apply_audio_mode = (
        lambda mode, track_id=None: audio_apply_calls.append((mode, track_id)) or (31 if mode == "auto" else track_id)
    )
    window.video_widget.position_seconds = lambda: 0

    window.open_session(make_player_session(start_index=0))

    assert load_calls == [("http://m/1.m3u8", False, 0)]
    assert window.audio_combo.count() == 1
    assert window.audio_combo.isEnabled() is False

    window.video_widget.audio_tracks_changed.emit()

    assert [window.audio_combo.itemText(index) for index in range(window.audio_combo.count())] == [
        "自动选择",
        "国语 (默认)",
    ]
    assert window.audio_combo.isEnabled() is True
    assert audio_apply_calls == [("auto", None)]
```

- [ ] **Step 2: Run the focused cross-episode and refresh-recovery tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_reuses_audio_track_preference_for_next_episode tests/test_player_window_ui.py::test_player_window_falls_back_to_auto_when_previous_audio_track_cannot_be_matched tests/test_player_window_ui.py::test_player_window_logs_and_resets_when_audio_refresh_fails tests/test_player_window_ui.py::test_player_window_refreshes_audio_options_when_mpv_reports_tracks_after_load -q
```

Expected: FAIL because the player window does not yet match audio preferences across episodes, log audio refresh failures, or react to delayed audio track updates.

- [ ] **Step 3: Implement audio preference matching and safe refresh recovery**

Add the preference helpers after the basic audio helpers in `src/atv_player/ui/player_window.py`:

```python
    def _remember_audio_track_preference(self, track: AudioTrack) -> None:
        self._audio_preference = AudioPreference(
            mode="track",
            title=track.title,
            lang=track.lang,
            is_default=track.is_default,
            is_forced=track.is_forced,
        )

    def _audio_track_match_score(self, track: AudioTrack, preference: AudioPreference) -> tuple[int, int, int]:
        return (
            int(bool(preference.title) and track.title == preference.title),
            int(bool(preference.lang) and track.lang == preference.lang),
            int(track.is_forced == preference.is_forced and track.is_default == preference.is_default),
        )

    def _matching_audio_track_for_preference(self) -> AudioTrack | None:
        if self._audio_preference.mode != "track" or not self._audio_tracks:
            return None
        ranked_tracks = sorted(
            self._audio_tracks,
            key=lambda track: self._audio_track_match_score(track, self._audio_preference),
            reverse=True,
        )
        best_track = ranked_tracks[0]
        if self._audio_track_match_score(best_track, self._audio_preference) == (0, 0, 0):
            return None
        return best_track

    def _apply_audio_preference(self) -> None:
        self.audio_combo.blockSignals(True)
        try:
            if self._audio_preference.mode == "track":
                matched_track = self._matching_audio_track_for_preference()
                if matched_track is not None:
                    applied_track_id = self.video.apply_audio_mode("track", track_id=matched_track.id)
                    for index, track in enumerate(self._audio_tracks, start=1):
                        if track.id == applied_track_id:
                            self.audio_combo.setCurrentIndex(index)
                            return
                self._audio_preference = AudioPreference()

            self.video.apply_audio_mode("auto")
            self.audio_combo.setCurrentIndex(0)
        finally:
            self.audio_combo.blockSignals(False)
```

Update `_refresh_audio_state()` to match the subtitle error-handling pattern:

```python
    def _refresh_audio_state(self) -> None:
        if not hasattr(self.video, "audio_tracks") or not hasattr(self.video, "apply_audio_mode"):
            self._audio_tracks = []
            self._audio_preference = AudioPreference()
            self._reset_audio_combo()
            return
        try:
            self._audio_tracks = self.video.audio_tracks()
        except Exception as exc:
            self._audio_tracks = []
            self._audio_preference = AudioPreference()
            self._reset_audio_combo()
            self._append_log(f"音轨加载失败: {exc}")
            return
        self._populate_audio_combo(self._audio_tracks)
        if not self._audio_tracks:
            self._audio_preference = AudioPreference()
            return
        try:
            self._apply_audio_preference()
        except Exception as exc:
            self._audio_preference = AudioPreference()
            self._reset_audio_combo()
            self._append_log(f"音轨切换失败: {exc}")
```

Update `_change_audio_selection()` so explicit user picks are remembered by metadata instead of only the raw track id:

```python
    def _change_audio_selection(self, index: int) -> None:
        if index < 0:
            return
        item_data = self.audio_combo.itemData(index)
        if item_data is None:
            return
        mode, track_id = item_data
        if mode == "auto":
            self._audio_preference = AudioPreference()
            self.video.apply_audio_mode("auto")
            return
        track = next(track for track in self._audio_tracks if track.id == track_id)
        self._remember_audio_track_preference(track)
        self.video.apply_audio_mode("track", track_id=track_id)
```

- [ ] **Step 4: Run the focused cross-episode and refresh-recovery tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_reuses_audio_track_preference_for_next_episode tests/test_player_window_ui.py::test_player_window_falls_back_to_auto_when_previous_audio_track_cannot_be_matched tests/test_player_window_ui.py::test_player_window_logs_and_resets_when_audio_refresh_fails tests/test_player_window_ui.py::test_player_window_refreshes_audio_options_when_mpv_reports_tracks_after_load -q
```

Expected: PASS with session-scoped audio preference reuse, automatic fallback to `自动选择`, delayed post-load audio refresh, and safe log-backed recovery on failures.

- [ ] **Step 5: Run the combined regression slice**

Run:

```bash
uv run pytest tests/test_mpv_widget.py tests/test_player_window_ui.py -q
```

Expected: PASS with both subtitle and audio selector coverage green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "feat: preserve audio track selection in session"
```
