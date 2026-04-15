# Player Slider Hover Tooltips Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show hover tooltips on the player progress slider with formatted time and on the volume slider with percentage text.

**Architecture:** Extend `ClickableSlider` with an optional hover-tooltip formatter callback so the slider can convert hover positions into value-specific tooltip text without changing click-to-seek behavior. `PlayerWindow` wires formatter functions for the progress and volume sliders and existing playback syncing remains unchanged.

**Tech Stack:** Python 3.14, PySide6, pytest-qt

---

### Task 1: Add failing hover-tooltip tests

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_player_window_progress_slider_hover_formats_time(qtbot, monkeypatch):
    ...


def test_player_window_volume_slider_hover_formats_percent(qtbot, monkeypatch):
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_player_window_ui.py -k "hover_formats"`
Expected: FAIL because no tooltip is shown for slider hover.

### Task 2: Implement slider hover tooltips

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write minimal implementation**

```python
class ClickableSlider(QSlider):
    def set_hover_tooltip_formatter(self, formatter):
        ...

    def mouseMoveEvent(self, event):
        ...

    def leaveEvent(self, event):
        ...
```

- [ ] **Step 2: Wire player formatters**

```python
self.progress.set_hover_tooltip_formatter(self._format_time)
self.volume_slider.set_hover_tooltip_formatter(lambda value: f"{value}%")
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `uv run pytest tests/test_player_window_ui.py -k "hover_formats"`
Expected: PASS

### Task 3: Verify player window regressions

**Files:**
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Run player window UI tests**

Run: `uv run pytest tests/test_player_window_ui.py`
Expected: PASS
