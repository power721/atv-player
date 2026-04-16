# Emby Playback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route Emby playback through `/emby-play` for stream URL resolution, progress reporting, and stop reporting while disabling local history for Emby sessions.

**Architecture:** Keep Emby browsing and detail parsing in `EmbyController`, but move runtime playback behavior into session-level hooks carried by `OpenPlayerRequest` and `PlayerSession`. `PlayerWindow` calls these hooks generically, and `MpvWidget` gains optional HTTP header support.

**Tech Stack:** Python, PySide6, pytest, httpx, python-mpv

---

### Task 1: Lock the `/emby-play` API contract in tests

**Files:**
- Modify: `tests/test_api_client.py`
- Modify: `src/atv_player/api.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_api_client_gets_emby_playback_source() -> None:
    ...

def test_api_client_reports_emby_playback_progress() -> None:
    ...

def test_api_client_stops_emby_playback() -> None:
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_api_client.py::test_api_client_gets_emby_playback_source tests/test_api_client.py::test_api_client_reports_emby_playback_progress tests/test_api_client.py::test_api_client_stops_emby_playback`
Expected: FAIL because `ApiClient` has no `/emby-play` methods.

- [ ] **Step 3: Write minimal implementation**

```python
def get_emby_playback_source(self, vod_id: str) -> dict[str, Any]:
    return self._request("GET", f"/emby-play/{self._vod_token}", params={"t": 0, "id": vod_id})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_api_client.py::test_api_client_gets_emby_playback_source tests/test_api_client.py::test_api_client_reports_emby_playback_progress tests/test_api_client.py::test_api_client_stops_emby_playback`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_api_client.py src/atv_player/api.py
git commit -m "feat: add emby playback api client"
```

### Task 2: Add session-level playback hooks for Emby

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/controllers/player_controller.py`
- Modify: `src/atv_player/controllers/emby_controller.py`
- Test: `tests/test_player_controller.py`
- Test: `tests/test_emby_controller.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_player_controller_skips_local_history_when_session_disables_it() -> None:
    ...

def test_player_controller_reports_progress_via_session_hook_without_saving_history() -> None:
    ...

def test_emby_build_request_disables_local_history_and_exposes_playback_hooks() -> None:
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_player_controller.py tests/test_emby_controller.py`
Expected: FAIL because sessions have no playback hooks or local-history toggle.

- [ ] **Step 3: Write minimal implementation**

```python
@dataclass(slots=True)
class OpenPlayerRequest:
    ...
    use_local_history: bool = True
    playback_loader: Callable[[PlayItem], None] | None = None
    playback_progress_reporter: Callable[[PlayItem, int], None] | None = None
    playback_stopper: Callable[[PlayItem], None] | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_player_controller.py tests/test_emby_controller.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/models.py src/atv_player/controllers/player_controller.py src/atv_player/controllers/emby_controller.py tests/test_player_controller.py tests/test_emby_controller.py
git commit -m "feat: add emby playback session hooks"
```

### Task 3: Load Emby stream URLs into mpv and stop sessions cleanly

**Files:**
- Modify: `src/atv_player/player/mpv_widget.py`
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`
- Test: `tests/test_mpv_widget.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_player_window_loads_play_item_via_session_loader_and_passes_headers(qtbot) -> None:
    ...

def test_player_window_stops_session_when_switching_items(qtbot) -> None:
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/test_player_window_ui.py tests/test_mpv_widget.py`
Expected: FAIL because player window never invokes runtime loaders/stoppers and mpv widget cannot receive headers.

- [ ] **Step 3: Write minimal implementation**

```python
def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/test_player_window_ui.py tests/test_mpv_widget.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/atv_player/player/mpv_widget.py src/atv_player/ui/player_window.py tests/test_player_window_ui.py tests/test_mpv_widget.py
git commit -m "feat: route emby playback through emby-play"
```

### Task 4: Run regression verification

**Files:**
- Test: `tests/test_api_client.py`
- Test: `tests/test_emby_controller.py`
- Test: `tests/test_player_controller.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Run focused regression tests**

Run: `uv run pytest -q tests/test_api_client.py tests/test_emby_controller.py tests/test_player_controller.py tests/test_player_window_ui.py`
Expected: PASS

- [ ] **Step 2: Run full suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-04-16-emby-playback-design.md docs/superpowers/plans/2026-04-16-emby-playback.md
git commit -m "docs: add emby playback design"
```
