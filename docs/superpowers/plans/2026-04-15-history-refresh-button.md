# History Refresh Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a refresh button to the playback history page so users can reload the current page of history without losing the current page number or page size.

**Architecture:** Keep the change localized to `HistoryPage` and its UI tests. The button reuses the existing `load_history()` code path, so pagination state, authorization handling, and API error handling remain centralized in one place instead of introducing a separate refresh method on the controller.

**Tech Stack:** Python 3.12, PySide6 widgets, pytest, pytest-qt

---

## File Structure

- `src/atv_player/ui/history_page.py`
  - Add the `刷新` button to the history action row and wire it to `load_history()`.
- `tests/test_browse_page_ui.py`
  - Add focused UI tests for refresh button presence and refresh behavior preserving pagination state.

### Task 1: Add The History Refresh Button And Preserve Current Pagination State

**Files:**
- Modify: `src/atv_player/ui/history_page.py:28-83`
- Modify: `tests/test_browse_page_ui.py:130-140`
- Modify: `tests/test_browse_page_ui.py:536-632`
- Test: `tests/test_browse_page_ui.py`

- [ ] **Step 1: Write the failing history-page refresh tests**

Add these tests to `tests/test_browse_page_ui.py` after `test_history_page_centers_content_container` and `test_history_page_delete_reloads_previous_page_when_last_page_becomes_empty` respectively:

```python
def test_history_page_exposes_refresh_button(qtbot) -> None:
    page = HistoryPage(FakeHistoryController())
    qtbot.addWidget(page)

    assert page.refresh_button.text() == "刷新"


def test_history_page_refresh_reuses_current_page_state(qtbot) -> None:
    class Controller:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def load_page(self, page: int, size: int):
            self.calls.append((page, size))
            return [], 120

    controller = Controller()
    page = HistoryPage(controller)
    qtbot.addWidget(page)

    page.page_size_combo.setCurrentText("30")
    page.load_history()
    page.next_page()
    controller.calls.clear()

    page.refresh_button.click()

    assert controller.calls == [(2, 30)]
```

- [ ] **Step 2: Run the focused history-page tests to verify they fail**

Run:

```bash
uv run pytest tests/test_browse_page_ui.py::test_history_page_exposes_refresh_button tests/test_browse_page_ui.py::test_history_page_refresh_reuses_current_page_state -q
```

Expected: FAIL because `HistoryPage` does not expose `refresh_button` yet.

- [ ] **Step 3: Write the minimal history-page implementation**

Update the button construction block in `src/atv_player/ui/history_page.py`:

```python
        self.delete_button = QPushButton("删除")
        self.clear_button = QPushButton("清空")
        self.refresh_button = QPushButton("刷新")
        self.prev_page_button = QPushButton("上一页")
        self.next_page_button = QPushButton("下一页")
```

Update the action-row layout so the refresh button sits with the existing history actions:

```python
        actions = QHBoxLayout()
        actions.addWidget(self.delete_button)
        actions.addWidget(self.clear_button)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        actions.addWidget(self.prev_page_button)
        actions.addWidget(self.page_label)
        actions.addWidget(self.next_page_button)
        actions.addWidget(self.page_size_combo)
```

Connect the button directly to `load_history()` in the signal wiring section:

```python
        self.delete_button.clicked.connect(self.delete_selected)
        self.clear_button.clicked.connect(self.clear_all)
        self.refresh_button.clicked.connect(self.load_history)
        self.table.cellDoubleClicked.connect(self._open_selected)
        self.prev_page_button.clicked.connect(self.previous_page)
        self.next_page_button.clicked.connect(self.next_page)
        self.page_size_combo.currentIndexChanged.connect(self._change_page_size)
        self._update_pagination_controls()
```

- [ ] **Step 4: Run the focused history-page tests to verify they pass**

Run:

```bash
uv run pytest tests/test_browse_page_ui.py::test_history_page_exposes_refresh_button tests/test_browse_page_ui.py::test_history_page_refresh_reuses_current_page_state -q
```

Expected: PASS with a visible `刷新` button and a refresh action that reloads the current page using the existing `current_page` and `page_size`.

- [ ] **Step 5: Run the broader history-page regression slice**

Run:

```bash
uv run pytest tests/test_browse_page_ui.py::test_history_page_centers_content_container tests/test_browse_page_ui.py::test_history_page_formats_episode_progress_and_time tests/test_browse_page_ui.py::test_history_page_loads_selected_page_and_page_size tests/test_browse_page_ui.py::test_history_page_disables_prev_and_next_when_unavailable tests/test_browse_page_ui.py::test_history_page_delete_reloads_previous_page_when_last_page_becomes_empty tests/test_browse_page_ui.py::test_history_page_exposes_refresh_button tests/test_browse_page_ui.py::test_history_page_refresh_reuses_current_page_state -q
```

Expected: PASS with the existing history-page layout, formatting, pagination, and delete behavior unchanged.

- [ ] **Step 6: Commit**

```bash
git add tests/test_browse_page_ui.py src/atv_player/ui/history_page.py
git commit -m "feat: add history refresh button"
```
