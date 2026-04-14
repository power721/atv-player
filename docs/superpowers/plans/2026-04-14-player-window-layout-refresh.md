# Player Window Layout Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh the player window layout so playback controls are centered, volume controls are right-aligned with an icon and constrained slider width, progress shows elapsed and total time, and fullscreen hides the entire bottom area plus sidebar content.

**Architecture:** Keep the work localized to `PlayerWindow` and its UI tests. Rebuild the bottom UI into explicit progress and control containers, add a small time-formatting helper plus a unified visibility refresh path, and verify behavior through focused `pytest-qt` tests before and after implementation.

**Tech Stack:** Python 3.14, PySide6, pytest, pytest-qt

---

## File Structure

- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`

## Task 1: Lock In The New Layout With Failing Tests

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_player_window_shows_time_labels_and_constrained_volume_group(qtbot) -> None:
    ...


def test_player_window_syncs_time_labels_from_video_progress(qtbot) -> None:
    ...


def test_player_window_fullscreen_hides_bottom_area_and_sidebar_contents(qtbot) -> None:
    ...
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_player_window_ui.py -q`
Expected: FAIL because the new labels, grouped layout widgets, and fullscreen visibility behavior do not exist yet.

## Task 2: Implement The Minimal PlayerWindow Changes

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Add the minimal implementation**

```python
class PlayerWindow(QWidget):
    def _format_time(self, seconds: int) -> str:
        ...

    def _apply_visibility_state(self) -> None:
        ...
```

- [ ] **Step 2: Run the focused tests to verify they pass**

Run: `uv run pytest tests/test_player_window_ui.py -q`
Expected: PASS

## Task 3: Verify No Regressions In Nearby UI

**Files:**
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Run the focused player window test suite again**

Run: `uv run pytest tests/test_player_window_ui.py -q`
Expected: PASS with all player window UI tests green.

- [ ] **Step 2: Commit**

```bash
git add tests/test_player_window_ui.py src/atv_player/ui/player_window.py docs/superpowers/plans/2026-04-14-player-window-layout-refresh.md
git commit -m "feat: refresh player window controls layout"
```
