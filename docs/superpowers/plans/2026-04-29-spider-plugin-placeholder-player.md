# Spider Plugin Placeholder Player Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open the player window immediately for spider-plugin cards, then hydrate playlist and playback URL asynchronously while keeping failures inside the player log.

**Architecture:** `MainWindow` opens a placeholder plugin session synchronously from card metadata, then starts the normal plugin request in the background. `PlayerWindow` gains placeholder-session support and an async `playback_loader` path so plugin playback URL resolution no longer blocks the UI thread.

**Tech Stack:** Python, PySide6, pytest-qt

---

### Task 1: Add regression tests for placeholder player open

**Files:**
- Modify: `tests/test_app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing test**

```python
def test_main_window_plugin_card_opens_placeholder_player_immediately_and_hydrates_later(...):
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py::test_main_window_plugin_card_opens_placeholder_player_immediately_and_hydrates_later -q`
Expected: FAIL because plugin card clicks still wait for the full request.

- [ ] **Step 3: Write minimal implementation**

```python
def _open_spider_item(...):
    placeholder_request = self._build_placeholder_player_request(...)
    self._open_player_immediately(placeholder_request)
    self._start_plugin_open_request(...)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py::test_main_window_plugin_card_opens_placeholder_player_immediately_and_hydrates_later -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_app.py src/atv_player/ui/main_window.py
git commit -m "feat: open spider player before detail request finishes"
```

### Task 2: Add failure-retention coverage for placeholder player

**Files:**
- Modify: `tests/test_app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing test**

```python
def test_main_window_plugin_card_failure_keeps_placeholder_player_open_and_logs_error(...):
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py::test_main_window_plugin_card_failure_keeps_placeholder_player_open_and_logs_error -q`
Expected: FAIL because the current flow uses modal errors instead of player-window logs.

- [ ] **Step 3: Write minimal implementation**

```python
def _handle_plugin_open_request_failed(...):
    self._append_player_status_log(f"详情加载失败: {message}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py::test_main_window_plugin_card_failure_keeps_placeholder_player_open_and_logs_error -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_app.py src/atv_player/ui/main_window.py
git commit -m "feat: keep placeholder spider player open on detail failure"
```

### Task 3: Add placeholder-session support in the player window

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/controllers/player_controller.py`
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing test**

```python
def test_player_window_can_open_placeholder_session_without_playlist(qtbot) -> None:
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_can_open_placeholder_session_without_playlist -q`
Expected: FAIL because `open_session()` assumes a non-empty playlist.

- [ ] **Step 3: Write minimal implementation**

```python
@dataclass(slots=True)
class PlayerSession:
    initial_log_message: str = ""
    is_placeholder: bool = False

def open_session(self, session, start_paused: bool = False) -> None:
    ...
    if session.initial_log_message:
        self._append_log(session.initial_log_message)
    if not session.playlist:
        return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_can_open_placeholder_session_without_playlist -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/models.py src/atv_player/controllers/player_controller.py src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: support placeholder player sessions"
```

### Task 4: Resolve spider playback URLs asynchronously

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/controllers/player_controller.py`
- Modify: `src/atv_player/plugins/controller.py`
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing test**

```python
def test_player_window_loads_play_item_via_async_session_loader_without_blocking_open_session(...):
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_loads_play_item_via_async_session_loader_without_blocking_open_session -q`
Expected: FAIL because `playback_loader` still runs on the UI thread.

- [ ] **Step 3: Write minimal implementation**

```python
class _PlaybackLoaderSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)

def _start_playback_loader(...):
    threading.Thread(target=run, daemon=True).start()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_player_window_ui.py::test_player_window_loads_play_item_via_async_session_loader_without_blocking_open_session -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/models.py src/atv_player/controllers/player_controller.py src/atv_player/plugins/controller.py src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: async load spider playback urls"
```

### Task 5: Verify the integrated flow

**Files:**
- Modify: `tests/test_app.py`
- Modify: `tests/test_player_window_ui.py`
- Modify: `tests/test_spider_plugin_controller.py`
- Modify: `tests/test_player_controller.py`
- Test: `tests/test_app.py`
- Test: `tests/test_player_window_ui.py`
- Test: `tests/test_spider_plugin_controller.py`
- Test: `tests/test_player_controller.py`

- [ ] **Step 1: Run focused verification**

Run: `uv run pytest tests/test_app.py tests/test_spider_plugin_controller.py tests/test_player_controller.py tests/test_player_window_ui.py -k "plugin_card or placeholder or playback_loader" -q`
Expected: PASS for all focused regressions.

- [ ] **Step 2: Run broader verification**

Run: `uv run pytest tests/test_app.py tests/test_spider_plugin_controller.py tests/test_player_controller.py tests/test_player_window_ui.py -q`
Expected: PASS, or one known unrelated failure called out explicitly with evidence.

- [ ] **Step 3: Commit**

```bash
git add src/atv_player/models.py src/atv_player/controllers/player_controller.py src/atv_player/plugins/controller.py src/atv_player/ui/main_window.py src/atv_player/ui/player_window.py tests/test_app.py tests/test_player_window_ui.py docs/superpowers/specs/2026-04-29-spider-plugin-placeholder-player-design.md docs/superpowers/plans/2026-04-29-spider-plugin-placeholder-player.md
git commit -m "feat: open spider player before playback resolves"
```
