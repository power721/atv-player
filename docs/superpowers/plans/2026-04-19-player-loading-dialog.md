# Player Loading Dialog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a modal loading dialog while the player is asynchronously resolving a play item or preparing an `m3u8` URL, then dismiss it automatically when playback starts or the flow fails.

**Architecture:** Keep the change local to `PlayerWindow`. Add a lightweight modal dialog plus small helper methods for showing and dismissing it, then wire those helpers into the existing async resolution and playback-preparation request-id flow so stale callbacks remain ignored. Drive the change with UI tests in `tests/test_player_window_ui.py`.

**Tech Stack:** Python, PySide6, pytest, pytest-qt, existing `PlayerWindow` async request-id guards

---

## File Structure

- Modify: `src/atv_player/ui/player_window.py`
  Add a lightweight loading dialog, helper methods, and lifecycle wiring around deferred playback preparation.
- Modify: `tests/test_player_window_ui.py`
  Add UI tests for loading-dialog visibility across resolution, preparation, fallback, and stale-request cases.

### Task 1: Cover Deferred Detail Resolution With A Failing UI Test

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing test for detail-resolution loading state**

Add this test to `tests/test_player_window_ui.py` near the existing deferred-playback tests:

```python
def test_player_window_shows_loading_dialog_while_resolving_play_item(qtbot) -> None:
    controller = RecordingPlayerController()
    unblock = threading.Event()

    resolved_vod = VodItem(
        vod_id="ep-2",
        vod_name="Resolved Episode 2",
        items=[PlayItem(title="Episode 2", url="http://resolved/2.mp4", vod_id="ep-2")],
    )

    def detail_resolver(item: PlayItem) -> VodItem:
        unblock.wait(timeout=3)
        return resolved_vod

    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(title="Episode 1", url="http://m/1.mp4"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]
    session.detail_resolver = detail_resolver
    window.open_session(session)
    window.video.load_calls.clear()

    window.play_next()

    qtbot.waitUntil(lambda: window._loading_dialog is not None and window._loading_dialog.isVisible())
    assert window._loading_dialog.label.text() == "正在解析播放地址，请稍候..."

    unblock.set()

    qtbot.waitUntil(lambda: window.video.load_calls == [("http://resolved/2.mp4", 0)])
    qtbot.waitUntil(lambda: window._loading_dialog is None or not window._loading_dialog.isVisible())
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_shows_loading_dialog_while_resolving_play_item -q
```

Expected: FAIL because `PlayerWindow` does not expose `_loading_dialog` and does not show any modal waiting UI during detail resolution.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_player_window_ui.py
git commit -m "test: cover player loading dialog during detail resolution"
```

### Task 2: Implement The Loading Dialog And Make The First Test Pass

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py::test_player_window_shows_loading_dialog_while_resolving_play_item`

- [ ] **Step 1: Add a small internal loading-dialog class**

Update the `PySide6.QtWidgets` import section in `src/atv_player/ui/player_window.py` to include `QDialog`, `QDialogButtonBox`, and `QProgressBar`, then add this helper class above `PlayerWindow`:

```python
class _PlayerLoadingDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle("请稍候")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setFixedWidth(320)

        self.label = QLabel("正在解析播放地址，请稍候...", self)
        self.label.setWordWrap(True)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)

        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addWidget(self.progress)
```
```

- [ ] **Step 2: Add player-window loading-dialog state and helpers**

In `PlayerWindow.__init__`, initialize the state:

```python
        self._loading_dialog: _PlayerLoadingDialog | None = None
```

Then add these helper methods near the existing UI helper methods:

```python
    def _show_loading_dialog(self, message: str = "正在解析播放地址，请稍候...") -> None:
        dialog = self._loading_dialog
        if dialog is None:
            dialog = _PlayerLoadingDialog(self)
            self._loading_dialog = dialog
        dialog.label.setText(message)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _dismiss_loading_dialog(self) -> None:
        dialog = self._loading_dialog
        if dialog is None:
            return
        self._loading_dialog = None
        dialog.close()
        dialog.deleteLater()
```

- [ ] **Step 3: Wire the dialog into detail-resolution lifecycle**

Update these methods in `src/atv_player/ui/player_window.py`:

```python
    def _start_play_item_resolution(... ) -> None:
        if self.session is None:
            return
        self._show_loading_dialog()
        session = self.session
        ...

    def _handle_play_item_resolve_succeeded(self, request_id: int, resolved_vod: VodItem | None) -> None:
        if request_id != self._play_item_request_id:
            return
        pending_load = self._pending_play_item_load
        self._pending_play_item_load = None
        if resolved_vod is not None:
            self._apply_resolved_vod(resolved_vod)
        if pending_load is None or not pending_load.wait_for_load:
            self._dismiss_loading_dialog()
            return
        if self.session is None or self.current_index != pending_load.index:
            self._dismiss_loading_dialog()
            return
        current_item = self.session.playlist[self.current_index]
        if not current_item.url:
            self._dismiss_loading_dialog()
            self._restore_current_index(pending_load.previous_index)
            self._append_log(f"播放失败: 没有可用的播放地址: {current_item.title}")
            return
        try:
            if self._start_playback_prepare(
                previous_index=pending_load.previous_index,
                start_position_seconds=pending_load.start_position_seconds,
                pause=pending_load.pause,
            ):
                return
            self._dismiss_loading_dialog()
            self._start_current_item_playback(
                start_position_seconds=pending_load.start_position_seconds,
                pause=pending_load.pause,
            )
        except Exception as exc:
            self._dismiss_loading_dialog()
            self._restore_current_index(pending_load.previous_index)
            self._append_log(f"播放失败: {exc}")

    def _handle_play_item_resolve_failed(self, request_id: int, message: str) -> None:
        if request_id != self._play_item_request_id:
            return
        self._dismiss_loading_dialog()
        ...
```

- [ ] **Step 4: Run the focused test to verify it passes**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_shows_loading_dialog_while_resolving_play_item -q
```

Expected: PASS

- [ ] **Step 5: Commit the implementation**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: show player loading dialog during detail resolution"
```

### Task 3: Cover Playback Preparation And Failure Paths With Failing Tests

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing tests for `m3u8` preparation**

Add these tests to `tests/test_player_window_ui.py` near the existing `m3u8` preparation tests:

```python
def test_player_window_shows_loading_dialog_while_preparing_m3u8(qtbot) -> None:
    class BlockingM3U8AdFilter:
        def __init__(self) -> None:
            self.unblock = threading.Event()

        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            self.unblock.wait(timeout=3)
            return "/tmp/cleaned-playlist.m3u8"

    filter_service = BlockingM3U8AdFilter()
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="正片", url="https://media.example/path/index.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=filter_service)
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window._loading_dialog is not None and window._loading_dialog.isVisible())
    filter_service.unblock.set()
    qtbot.waitUntil(lambda: window.video.load_calls == [("/tmp/cleaned-playlist.m3u8", 0)])
    qtbot.waitUntil(lambda: window._loading_dialog is None or not window._loading_dialog.isVisible())


def test_player_window_closes_loading_dialog_when_m3u8_preparation_fails(qtbot) -> None:
    class FailingM3U8AdFilter:
        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            raise RuntimeError("network down")

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="正片", url="https://media.example/path/index.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=FailingM3U8AdFilter())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window.video.load_calls == [("https://media.example/path/index.m3u8", 0)])
    assert window._loading_dialog is None or not window._loading_dialog.isVisible()
    assert "广告过滤失败" in window.log_view.toPlainText()
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_shows_loading_dialog_while_preparing_m3u8 tests/test_player_window_ui.py::test_player_window_closes_loading_dialog_when_m3u8_preparation_fails -q
```

Expected: FAIL because the loading dialog is not yet shown or dismissed consistently for the playback-preparation path.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_player_window_ui.py
git commit -m "test: cover player loading dialog during m3u8 preparation"
```

### Task 4: Keep The Dialog Visible Across Chained Async Stages And Clear It On Invalidation

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Extend the tests for chained async flow and stale requests**

Add this test to `tests/test_player_window_ui.py`:

```python
def test_player_window_keeps_loading_dialog_during_resolution_to_m3u8_prepare_transition(qtbot) -> None:
    resolve_unblock = threading.Event()
    prepare_unblock = threading.Event()

    class BlockingFilter:
        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            prepare_unblock.wait(timeout=3)
            return "/tmp/resolved-cleaned.m3u8"

    class ResolvingController(FakePlayerController):
        def resolve_play_item_detail(self, session, play_item):
            resolve_unblock.wait(timeout=3)
            play_item.url = "https://media.example/path/resolved.m3u8"
            return VodItem(
                vod_id="ep-2",
                vod_name="Episode 2",
                items=[PlayItem(title="Episode 2", url=play_item.url, vod_id="ep-2")],
            )

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="正片", url="", vod_id="ep-2")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(ResolvingController(), m3u8_ad_filter=BlockingFilter())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: window._loading_dialog is not None and window._loading_dialog.isVisible())
    resolve_unblock.set()
    qtbot.waitUntil(lambda: window._loading_dialog is not None and window._loading_dialog.isVisible())
    prepare_unblock.set()
    qtbot.waitUntil(lambda: window.video.load_calls == [("/tmp/resolved-cleaned.m3u8", 0)])
    qtbot.waitUntil(lambda: window._loading_dialog is None or not window._loading_dialog.isVisible())
```

Add this stale-request test to `tests/test_player_window_ui.py`:

```python
def test_player_window_dismisses_loading_dialog_when_playback_request_is_invalidated(qtbot) -> None:
    controller = RecordingPlayerController()
    unblock = threading.Event()

    def detail_resolver(item: PlayItem) -> VodItem:
        unblock.wait(timeout=3)
        return VodItem(
            vod_id=item.vod_id,
            vod_name=item.title,
            items=[PlayItem(title=item.title, url=f"http://resolved/{item.vod_id}.mp4", vod_id=item.vod_id)],
        )

    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(title="Episode 1", url="http://m/1.mp4"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]
    session.detail_resolver = detail_resolver
    window.open_session(session)
    window.video.load_calls.clear()

    window.play_next()
    qtbot.waitUntil(lambda: window._loading_dialog is not None and window._loading_dialog.isVisible())

    window.play_previous()

    assert window._loading_dialog is None or not window._loading_dialog.isVisible()
    unblock.set()
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_keeps_loading_dialog_during_resolution_to_m3u8_prepare_transition tests/test_player_window_ui.py::test_player_window_dismisses_loading_dialog_when_playback_request_is_invalidated -q
```

Expected: FAIL because the dialog lifecycle is not yet aligned with both chained async stages and invalidation.

- [ ] **Step 3: Update dialog lifecycle for preparation, invalidation, and window close**

Update these methods in `src/atv_player/ui/player_window.py`:

```python
    def _start_playback_prepare(... ) -> bool:
        if self.session is None:
            return False
        current_item = self.session.playlist[self.current_index]
        should_prepare = getattr(self._m3u8_ad_filter, "should_prepare", None)
        if callable(should_prepare):
            if not should_prepare(current_item.url):
                return False
        elif ".m3u8" not in current_item.url.lower():
            return False
        self._show_loading_dialog()
        self._playback_prepare_request_id += 1
        ...

    def _invalidate_play_item_resolution(self) -> None:
        self._dismiss_loading_dialog()
        self._play_item_request_id += 1
        self._pending_play_item_load = None
        self._playback_prepare_request_id += 1
        self._pending_playback_prepare = None

    def _handle_playback_prepare_succeeded(self, request_id: int, prepared_url: str) -> None:
        if request_id != self._playback_prepare_request_id:
            return
        pending_prepare = self._pending_playback_prepare
        self._pending_playback_prepare = None
        if pending_prepare is None:
            self._dismiss_loading_dialog()
            return
        if self.session is None or self.current_index != pending_prepare.index:
            self._dismiss_loading_dialog()
            return
        current_item = self.session.playlist[self.current_index]
        current_item.url = prepared_url
        try:
            self._dismiss_loading_dialog()
            self._start_current_item_playback(
                start_position_seconds=pending_prepare.start_position_seconds,
                pause=pending_prepare.pause,
            )
        except Exception as exc:
            self._restore_current_index(pending_prepare.previous_index)
            self._append_log(f"播放失败: {exc}")

    def _handle_playback_prepare_failed(self, request_id: int, message: str) -> None:
        if request_id != self._playback_prepare_request_id:
            return
        pending_prepare = self._pending_playback_prepare
        self._pending_playback_prepare = None
        if pending_prepare is None:
            self._dismiss_loading_dialog()
            return
        if self.session is None or self.current_index != pending_prepare.index:
            self._dismiss_loading_dialog()
            return
        self._dismiss_loading_dialog()
        self._append_log(f"广告过滤失败，继续播放原地址: {message}")
        try:
            self._start_current_item_playback(
                start_position_seconds=pending_prepare.start_position_seconds,
                pause=pending_prepare.pause,
            )
        except Exception as exc:
            self._restore_current_index(pending_prepare.previous_index)
            self._append_log(f"播放失败: {exc}")

    def closeEvent(self, event: QCloseEvent) -> None:
        self._dismiss_loading_dialog()
        ...
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_window_ui.py::test_player_window_shows_loading_dialog_while_preparing_m3u8 tests/test_player_window_ui.py::test_player_window_closes_loading_dialog_when_m3u8_preparation_fails tests/test_player_window_ui.py::test_player_window_keeps_loading_dialog_during_resolution_to_m3u8_prepare_transition tests/test_player_window_ui.py::test_player_window_dismisses_loading_dialog_when_playback_request_is_invalidated -q
```

Expected: PASS

- [ ] **Step 5: Commit the lifecycle fixes**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: keep player loading dialog in sync with async playback prep"
```

### Task 5: Run The Relevant Regression Suite

**Files:**
- Modify: none
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Run the targeted player-window regression tests**

Run:

```bash
uv run pytest tests/test_player_window_ui.py -q
```

Expected: PASS

- [ ] **Step 2: Verify no unintended controller regressions**

Run:

```bash
uv run pytest tests/test_player_controller.py -q
```

Expected: PASS

- [ ] **Step 3: Commit verification-only checkpoint if needed**

If the implementation commits are already clean and no new files changed during verification, skip this step. Otherwise:

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "test: verify player loading dialog behavior"
```
