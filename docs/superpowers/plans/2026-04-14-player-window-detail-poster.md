# Player Window Detail Poster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fixed-size static poster area at the top of the player window detail pane while preserving the existing metadata and playback log behavior.

**Architecture:** Keep the current detail container in `PlayerWindow`, but extend it into a three-part stack: poster label, metadata view, and log view. Render the poster directly from `session.vod.vod_pic` through a narrow local-path/Qt-loadable path, and fail closed to an empty reserved area when the poster is missing or unrenderable.

**Tech Stack:** Python 3.14, PySide6 widgets, pytest, pytest-qt

---

## File Structure

- Modify: `src/atv_player/ui/player_window.py`
  Add the poster widget, poster rendering helpers, and top-of-pane layout wiring.
- Modify: `tests/test_player_window_ui.py`
  Add focused UI tests for poster placement, poster rendering, and missing-poster behavior.

### Task 1: Add Failing Poster Layout And Behavior Tests

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing poster tests**

Update the imports at the top of `tests/test_player_window_ui.py` so the file can create a temporary image:

```python
from PySide6.QtCore import QByteArray, QEvent, Qt
from PySide6.QtGui import QColor, QCursor, QKeyEvent, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication, QComboBox
from PySide6.QtWidgets import QSplitter
```

Add these tests after `test_player_window_uses_detail_container_with_metadata_and_log_views`:

```python
def test_player_window_places_poster_widget_above_metadata_and_log_views(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    details_layout = window.details.layout()

    assert window.poster_label is not None
    assert details_layout.indexOf(window.poster_label) != -1
    assert details_layout.indexOf(window.poster_label) < details_layout.indexOf(window.metadata_view)
    assert details_layout.indexOf(window.poster_label) < details_layout.indexOf(window.log_view)
    assert window.poster_label.alignment() == Qt.AlignmentFlag.AlignCenter
    assert window.poster_label.minimumHeight() > 0


def test_player_window_renders_poster_when_session_has_vod_pic(qtbot, tmp_path) -> None:
    poster_path = tmp_path / "poster.png"
    pixmap = QPixmap(20, 30)
    pixmap.fill(QColor("red"))
    assert pixmap.save(str(poster_path)) is True

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
        vod=VodItem(vod_id="movie-1", vod_name="九寨沟", vod_pic=str(poster_path)),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    rendered = window.poster_label.pixmap()
    assert rendered is not None
    assert rendered.isNull() is False
    assert rendered.size().width() <= window.poster_label.maximumWidth()
    assert rendered.size().height() <= window.poster_label.maximumHeight()


def test_player_window_keeps_empty_reserved_poster_area_without_placeholder_text(qtbot) -> None:
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
        vod=VodItem(vod_id="movie-1", vod_name="九寨沟", vod_pic=""),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    rendered = window.poster_label.pixmap()
    assert rendered is None or rendered.isNull() is True
    assert window.poster_label.text() == ""
    assert window.poster_label.minimumHeight() > 0
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_places_poster_widget_above_metadata_and_log_views tests/test_player_window_ui.py::test_player_window_renders_poster_when_session_has_vod_pic tests/test_player_window_ui.py::test_player_window_keeps_empty_reserved_poster_area_without_placeholder_text -v
```

Expected: FAIL because `PlayerWindow` does not yet define `poster_label` or render poster state from `vod_pic`.

### Task 2: Implement The Poster Area And Verify Existing Detail Behavior

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the minimal poster widget and rendering code**

Update the imports in `src/atv_player/ui/player_window.py`:

```python
from PySide6.QtCore import QByteArray, QEvent, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QCursor, QIcon, QKeyEvent, QKeySequence, QMouseEvent, QPixmap, QShortcut
```

Add poster size constants near the other class constants:

```python
class PlayerWindow(QWidget):
    closed_to_main = Signal()
    _SEEK_SHORTCUT_SECONDS = 15
    _VOLUME_SHORTCUT_STEP = 5
    _CURSOR_HIDE_DELAY_MS = 3000
    _POSTER_SIZE = QSize(180, 260)
```

Create the poster widget in `__init__` before the metadata and log views:

```python
self.poster_label = QLabel()
self.poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
self.poster_label.setMinimumSize(self._POSTER_SIZE)
self.poster_label.setMaximumSize(self._POSTER_SIZE)
self.poster_label.setText("")
```

Insert the poster at the top of the existing detail layout:

```python
self.details = QWidget()
details_layout = QVBoxLayout(self.details)
details_layout.setContentsMargins(0, 0, 0, 0)
details_layout.setSpacing(6)
details_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
details_layout.addWidget(self.poster_label, 0, Qt.AlignmentFlag.AlignHCenter)
details_layout.addWidget(QLabel("影片详情"))
details_layout.addWidget(self.metadata_view, 3)
details_layout.addWidget(QLabel("播放日志"))
details_layout.addWidget(self.log_view, 1)
```

Add these helpers to `PlayerWindow`:

```python
def _clear_poster(self) -> None:
    self.poster_label.clear()
    self.poster_label.setText("")
    self.poster_label.setPixmap(QPixmap())


def _load_poster_pixmap(self, source: str) -> QPixmap:
    if not source:
        return QPixmap()
    pixmap = QPixmap(source)
    if pixmap.isNull():
        return QPixmap()
    return pixmap.scaled(
        self._POSTER_SIZE,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _render_poster(self) -> None:
    if self.session is None:
        self._clear_poster()
        return
    pixmap = self._load_poster_pixmap(self.session.vod.vod_pic)
    if pixmap.isNull():
        self._clear_poster()
        return
    self.poster_label.setText("")
    self.poster_label.setPixmap(pixmap)
```

Call `_render_poster()` from `open_session()` immediately after `self.session = session` and before `_render_metadata()`:

```python
def open_session(self, session, start_paused: bool = False) -> None:
    self.session = session
    self._render_poster()
    self._render_metadata()
    self._reset_log()
    self.current_index = session.start_index
    self.current_speed = session.speed
```

- [ ] **Step 2: Run the focused poster tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_places_poster_widget_above_metadata_and_log_views tests/test_player_window_ui.py::test_player_window_renders_poster_when_session_has_vod_pic tests/test_player_window_ui.py::test_player_window_keeps_empty_reserved_poster_area_without_placeholder_text -v
```

Expected: PASS

- [ ] **Step 3: Run the broader player-window detail regression tests**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_uses_detail_container_with_metadata_and_log_views tests/test_player_window_ui.py::test_player_window_renders_title_metadata_in_expected_order tests/test_player_window_ui.py::test_player_window_appends_runtime_failures_to_log_view_without_overwriting_metadata tests/test_player_window_ui.py::test_player_window_opening_new_session_refreshes_metadata_and_clears_old_logs -v
```

Expected: PASS

- [ ] **Step 4: Run the full player-window suite**

Run:

```bash
uv run pytest tests/test_player_window_ui.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py
git commit -m "feat: add poster to player detail pane"
```
