# Browse And History Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add minimal, testable pagination controls to the browse and history views so both pages can navigate backend pages and change page size.

**Architecture:** Keep pagination state inside each Qt page widget. `BrowsePage` owns per-path pagination memory for the current app session, while `HistoryPage` owns one global history pagination state. Both pages expose the same small desktop control surface: previous button, current page label, next button, and page size combo box.

**Tech Stack:** Python 3.12, PySide6 Qt Widgets, pytest, pytest-qt

---

## File Structure

### Application Files

- Modify: `src/atv_player/ui/browse_page.py`
  Purpose: add browse pagination state, per-path memory, control row, and paged controller calls.
- Modify: `src/atv_player/ui/history_page.py`
  Purpose: add history pagination state, control row, and page-aware reload behavior after delete and clear.

### Test Files

- Modify: `tests/test_browse_page_ui.py`
  Purpose: cover browse pagination behavior, history pagination behavior, and control enablement from the UI layer.

### Responsibility Map

- `BrowsePage` keeps `current_page`, `page_size`, `total_items`, and `_page_state_by_path`.
- `HistoryPage` keeps `current_page`, `page_size`, and `total_items` for the history list only.
- `tests/test_browse_page_ui.py` remains the main UI regression surface for both widgets.

## Task 1: Add Browse Page Pagination

**Files:**
- Modify: `tests/test_browse_page_ui.py`
- Modify: `src/atv_player/ui/browse_page.py`

- [ ] **Step 1: Write the failing browse pagination tests**

```python
# tests/test_browse_page_ui.py
def test_browse_page_loads_selected_page_and_page_size(qtbot) -> None:
    controller = FakeBrowseController()
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page.page_size_combo.setCurrentText("30")
    page.load_path("/电影")
    controller.loaded_paths.clear()

    page.next_page()

    assert controller.load_calls[-1] == ("/电影", 2, 30)
    assert page.page_label.text() == "第 2 / 4 页"


def test_browse_page_resets_to_first_page_for_new_path(qtbot) -> None:
    controller = FakeBrowseController()
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page.load_path("/电影")
    page.next_page()
    page.load_path("/剧集")

    assert controller.load_calls[-1] == ("/剧集", 1, 50)
    assert page.current_page == 1


def test_browse_page_remembers_page_state_per_path(qtbot) -> None:
    controller = FakeBrowseController()
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page.page_size_combo.setCurrentText("30")
    page.load_path("/电影")
    page.next_page()
    page.load_path("/剧集")
    page.load_path("/电影")

    assert controller.load_calls[-1] == ("/电影", 2, 30)
    assert page.current_page == 2
    assert page.page_size == 30


def test_browse_page_disables_prev_and_next_when_unavailable(qtbot) -> None:
    controller = FakeBrowseController(total=30)
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page.load_path("/电影")

    assert page.prev_page_button.isEnabled() is False
    assert page.next_page_button.isEnabled() is False
```

- [ ] **Step 2: Run the targeted browse tests to verify they fail**

Run: `uv run pytest tests/test_browse_page_ui.py -q`
Expected: FAIL with missing `page_size_combo`, `next_page`, `page_label`, or wrong controller call signatures

- [ ] **Step 3: Implement minimal browse pagination state and controls**

```python
# src/atv_player/ui/browse_page.py
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton


class BrowsePage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.current_page = 1
        self.page_size = 50
        self.total_items = 0
        self._page_state_by_path: dict[str, tuple[int, int]] = {}
        self.prev_page_button = QPushButton("上一页")
        self.next_page_button = QPushButton("下一页")
        self.page_label = QLabel("第 1 / 1 页")
        self.page_size_combo = QComboBox()
        for size in ("20", "30", "50", "100"):
            self.page_size_combo.addItem(size, int(size))

        pagination_row = QHBoxLayout()
        pagination_row.addStretch(1)
        pagination_row.addWidget(self.prev_page_button)
        pagination_row.addWidget(self.page_label)
        pagination_row.addWidget(self.next_page_button)
        pagination_row.addWidget(self.page_size_combo)

        file_layout.addLayout(pagination_row)

        self.prev_page_button.clicked.connect(self.previous_page)
        self.next_page_button.clicked.connect(self.next_page)
        self.page_size_combo.currentIndexChanged.connect(self._change_page_size)

    def load_path(self, path: str) -> None:
        normalized_path = path or "/"
        if normalized_path != self.current_path:
            saved_page, saved_size = self._page_state_by_path.get(normalized_path, (1, self.page_size))
            self.current_page = saved_page
            self.page_size = saved_size
            self._sync_page_size_combo()
        self.current_path = normalized_path
        items, total = self.controller.load_folder(self.current_path, page=self.current_page, size=self.page_size)
        self.total_items = total
        self._page_state_by_path[self.current_path] = (self.current_page, self.page_size)
        self._populate_table(items)
        self._update_pagination_controls()

    def previous_page(self) -> None:
        if self.current_page <= 1:
            return
        self.current_page -= 1
        self.load_path(self.current_path)

    def next_page(self) -> None:
        if self.current_page >= self._total_pages():
            return
        self.current_page += 1
        self.load_path(self.current_path)

    def _change_page_size(self) -> None:
        self.page_size = int(self.page_size_combo.currentData())
        self.current_page = 1
        self.load_path(self.current_path)

    def _total_pages(self) -> int:
        return max(1, (self.total_items + self.page_size - 1) // self.page_size)

    def _update_pagination_controls(self) -> None:
        total_pages = self._total_pages()
        self.page_label.setText(f"第 {self.current_page} / {total_pages} 页")
        self.prev_page_button.setEnabled(self.current_page > 1)
        self.next_page_button.setEnabled(self.current_page < total_pages)
```

- [ ] **Step 4: Run the browse pagination tests to verify they pass**

Run: `uv run pytest tests/test_browse_page_ui.py -q`
Expected: PASS for the new browse pagination tests

- [ ] **Step 5: Commit the browse pagination slice**

```bash
git add tests/test_browse_page_ui.py src/atv_player/ui/browse_page.py
git commit -m "feat: add browse pagination controls"
```

## Task 2: Add History Page Pagination

**Files:**
- Modify: `tests/test_browse_page_ui.py`
- Modify: `src/atv_player/ui/history_page.py`

- [ ] **Step 1: Write the failing history pagination tests**

```python
# tests/test_browse_page_ui.py
def test_history_page_loads_selected_page_and_page_size(qtbot) -> None:
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
    controller.calls.clear()

    page.next_page()

    assert controller.calls[-1] == (2, 30)
    assert page.page_label.text() == "第 2 / 4 页"


def test_history_page_disables_prev_and_next_when_unavailable(qtbot) -> None:
    class Controller:
        def load_page(self, page: int, size: int):
            return [], 20

    page = HistoryPage(Controller())
    qtbot.addWidget(page)

    page.load_history()

    assert page.prev_page_button.isEnabled() is False
    assert page.next_page_button.isEnabled() is False


def test_history_page_delete_reloads_previous_page_when_last_page_becomes_empty(qtbot) -> None:
    class Controller:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []
            self.records = {
                2: [HistoryRecord(id=9, key="movie-1", vod_name="Movie", vod_pic="", vod_remarks="Ep", episode=0, episode_url="", position=0, opening=0, ending=0, speed=1.0, create_time=1)],
                1: [],
            }

        def load_page(self, page: int, size: int):
            self.calls.append((page, size))
            total = 51 if page == 2 else 50
            return self.records.get(page, []), total

        def delete_one(self, history_id: int) -> None:
            self.records[2] = []

    controller = Controller()
    page = HistoryPage(controller)
    qtbot.addWidget(page)
    page.current_page = 2
    page.page_size = 50

    page.load_history()
    page.table.selectRow(0)
    page.delete_selected()

    assert controller.calls[-1] == (1, 50)
    assert page.current_page == 1
```

- [ ] **Step 2: Run the targeted history tests to verify they fail**

Run: `uv run pytest tests/test_browse_page_ui.py -q`
Expected: FAIL with missing pagination controls or history reloading wrong page

- [ ] **Step 3: Implement minimal history pagination state and reload behavior**

```python
# src/atv_player/ui/history_page.py
class HistoryPage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.current_page = 1
        self.page_size = 100
        self.total_items = 0
        self.prev_page_button = QPushButton("上一页")
        self.next_page_button = QPushButton("下一页")
        self.page_label = QLabel("第 1 / 1 页")
        self.page_size_combo = QComboBox()
        for size in ("20", "30", "50", "100"):
            self.page_size_combo.addItem(size, int(size))

        pagination_row = QHBoxLayout()
        pagination_row.addStretch(1)
        pagination_row.addWidget(self.prev_page_button)
        pagination_row.addWidget(self.page_label)
        pagination_row.addWidget(self.next_page_button)
        pagination_row.addWidget(self.page_size_combo)

        layout.addLayout(pagination_row)

        self.prev_page_button.clicked.connect(self.previous_page)
        self.next_page_button.clicked.connect(self.next_page)
        self.page_size_combo.currentIndexChanged.connect(self._change_page_size)

    def load_history(self) -> None:
        records, total = self.controller.load_page(page=self.current_page, size=self.page_size)
        self.total_items = total
        if self.current_page > self._total_pages():
            self.current_page = self._total_pages()
            records, total = self.controller.load_page(page=self.current_page, size=self.page_size)
            self.total_items = total
        self.records = records
        self._populate_rows(records)
        self._update_pagination_controls()

    def previous_page(self) -> None:
        if self.current_page <= 1:
            return
        self.current_page -= 1
        self.load_history()

    def next_page(self) -> None:
        if self.current_page >= self._total_pages():
            return
        self.current_page += 1
        self.load_history()

    def _change_page_size(self) -> None:
        self.page_size = int(self.page_size_combo.currentData())
        self.current_page = 1
        self.load_history()

    def _total_pages(self) -> int:
        return max(1, (self.total_items + self.page_size - 1) // self.page_size)

    def _update_pagination_controls(self) -> None:
        total_pages = self._total_pages()
        self.page_label.setText(f"第 {self.current_page} / {total_pages} 页")
        self.prev_page_button.setEnabled(self.current_page > 1)
        self.next_page_button.setEnabled(self.current_page < total_pages)

    def delete_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows:
            return
        ids = [self.records[row].id for row in rows]
        if len(ids) == 1:
            self.controller.delete_one(ids[0])
        else:
            self.controller.delete_many(ids)
        if len(ids) == len(self.records) and self.current_page > 1:
            self.current_page -= 1
        self.load_history()
```

- [ ] **Step 4: Run the history pagination tests to verify they pass**

Run: `uv run pytest tests/test_browse_page_ui.py -q`
Expected: PASS for the new history pagination tests

- [ ] **Step 5: Commit the history pagination slice**

```bash
git add tests/test_browse_page_ui.py src/atv_player/ui/history_page.py
git commit -m "feat: add history pagination controls"
```

## Task 3: Verify The Combined Pagination Behavior

**Files:**
- Modify: `tests/test_browse_page_ui.py`
- Modify: `src/atv_player/ui/browse_page.py`
- Modify: `src/atv_player/ui/history_page.py`

- [ ] **Step 1: Add one combined regression test for current-page refresh behavior**

```python
# tests/test_browse_page_ui.py
def test_browse_page_refresh_reuses_current_page_state(qtbot) -> None:
    controller = FakeBrowseController(total=120)
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page.page_size_combo.setCurrentText("30")
    page.load_path("/电影")
    page.next_page()
    controller.load_calls.clear()

    page.reload()

    assert controller.load_calls == [("/电影", 2, 30)]
```

- [ ] **Step 2: Run the focused pagination UI tests**

Run: `uv run pytest tests/test_browse_page_ui.py -q`
Expected: PASS

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS with all existing tests green

- [ ] **Step 4: Commit the final pagination verification**

```bash
git add tests/test_browse_page_ui.py src/atv_player/ui/browse_page.py src/atv_player/ui/history_page.py
git commit -m "test: cover browse and history pagination flows"
```
