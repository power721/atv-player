# Browse Local Sorting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add click-to-sort behavior for the browse page file list so the current loaded page can be sorted locally by `名称`、`大小`、`豆瓣ID`、`评分`、`时间`.

**Architecture:** Keep the existing `QTableWidget` on the browse page and let Qt handle header sorting. Extend row population so each sortable cell carries a real comparison value instead of relying on the display string. This keeps backend pagination unchanged and scopes sorting to the rows already loaded in memory.

**Tech Stack:** Python 3.12, PySide6 `QTableWidget`, `pytest`, `pytest-qt`

---

## File Map

- Modify: `src/atv_player/ui/browse_page.py`
  - Enable local sorting on the browse file table.
  - Add helpers that build `QTableWidgetItem` instances with stable sort keys for text, numeric, and time columns.
  - Keep `类型` unsorted and preserve the current visible text.
- Modify: `tests/test_browse_page_ui.py`
  - Add UI coverage for local sorting by `名称` and `大小`.
  - Add a regression test that clicking sortable headers does not trigger another folder load.
  - Add an empty-value coverage test for sortable columns.

### Task 1: Lock The Expected Sorting Behavior In Tests

**Files:**
- Modify: `tests/test_browse_page_ui.py`
- Test: `tests/test_browse_page_ui.py`

- [ ] **Step 1: Write the failing test for name sorting**

```python
def test_browse_page_sorts_current_rows_by_name_from_header_click(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)

    page._populate_table(
        [
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Zulu",
                "vod_time": "2026-04-14 12:00",
                "vod_remarks": "2 GB",
                "dbid": 2,
            })(),
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Alpha",
                "vod_time": "2026-04-13 12:00",
                "vod_remarks": "10 GB",
                "dbid": 1,
            })(),
        ]
    )

    page.table.sortItems(1)
    assert [page.table.item(row, 1).text() for row in range(page.table.rowCount())] == ["Alpha", "Zulu"]

    page.table.sortItems(1, Qt.SortOrder.DescendingOrder)
    assert [page.table.item(row, 1).text() for row in range(page.table.rowCount())] == ["Zulu", "Alpha"]
```

- [ ] **Step 2: Write the failing test for numeric size sorting**

```python
def test_browse_page_sorts_size_by_numeric_value_not_text(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)

    page._populate_table(
        [
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Ten",
                "vod_time": "2026-04-14",
                "vod_remarks": "10 GB",
                "dbid": 10,
            })(),
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Two",
                "vod_time": "2026-04-14",
                "vod_remarks": "2 GB",
                "dbid": 20,
            })(),
        ]
    )

    page.table.sortItems(2)

    assert [page.table.item(row, 1).text() for row in range(page.table.rowCount())] == ["Two", "Ten"]
```

- [ ] **Step 3: Write the failing test that sorting stays local**

```python
def test_browse_page_sorting_does_not_reload_folder_data(qtbot) -> None:
    controller = FakeBrowseController()
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page._populate_table(
        [
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Beta",
                "vod_time": "2026-04-14",
                "vod_remarks": "1 GB",
                "dbid": 0,
            })(),
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Alpha",
                "vod_time": "2026-04-14",
                "vod_remarks": "2 GB",
                "dbid": 0,
            })(),
        ]
    )

    controller.load_calls.clear()
    page.table.sortItems(1)

    assert controller.load_calls == []
```

- [ ] **Step 4: Write the failing test for empty sortable values**

```python
def test_browse_page_sorts_rows_with_empty_sortable_values_without_crashing(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)

    page._populate_table(
        [
            type("Item", (), {
                "type": 1,
                "vod_tag": "folder",
                "vod_name": "Folder",
                "vod_time": "",
                "vod_remarks": "8.6",
                "dbid": 0,
            })(),
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Movie",
                "vod_time": "2026-04-14 08:00",
                "vod_remarks": "1.4 GB",
                "dbid": 123456,
            })(),
        ]
    )

    page.table.sortItems(2)
    page.table.sortItems(4)
    page.table.sortItems(5)

    assert page.table.rowCount() == 2
    assert {page.table.item(row, 1).text() for row in range(page.table.rowCount())} == {"Folder", "Movie"}
```

- [ ] **Step 5: Run the targeted tests to verify they fail for the right reason**

Run:

```bash
uv run pytest tests/test_browse_page_ui.py -k "sorts_current_rows_by_name or sorts_size_by_numeric_value_not_text or sorting_does_not_reload_folder_data or sorts_rows_with_empty_sortable_values_without_crashing" -v
```

Expected:

```text
FAIL tests/test_browse_page_ui.py::test_browse_page_sorts_size_by_numeric_value_not_text
FAIL tests/test_browse_page_ui.py::test_browse_page_sorts_rows_with_empty_sortable_values_without_crashing
```

The exact failure text may vary, but the failures must show that the table is still using plain display strings and has no dedicated local sorting behavior yet.

- [ ] **Step 6: Commit the red tests**

```bash
git add tests/test_browse_page_ui.py
git commit -m "test: cover browse page local sorting"
```

### Task 2: Implement Stable Local Sort Keys In The Browse Table

**Files:**
- Modify: `src/atv_player/ui/browse_page.py`
- Test: `tests/test_browse_page_ui.py`

- [ ] **Step 1: Add imports needed for typed sort keys**

Insert near the top of `src/atv_player/ui/browse_page.py`:

```python
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
```

- [ ] **Step 2: Add a sortable table item subclass and parsing helpers**

Insert above `class BrowsePage(QWidget):` in `src/atv_player/ui/browse_page.py`:

```python
class SortableTableWidgetItem(QTableWidgetItem):
    def __init__(self, text: str, sort_value: object) -> None:
        super().__init__(text)
        self.sort_value = sort_value

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SortableTableWidgetItem):
            return super().__lt__(other)
        return self.sort_value < other.sort_value


def _parse_size_value(text: str) -> tuple[int, float, str]:
    cleaned = (text or "").strip()
    if not cleaned or cleaned == "-":
        return (1, 0.0, "")
    parts = cleaned.split()
    if len(parts) != 2:
        return (0, 0.0, cleaned)
    number_text, unit = parts
    try:
        number = float(number_text)
    except ValueError:
        return (0, 0.0, cleaned)
    unit_order = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4}
    return (0, number * (1024 ** unit_order.get(unit.upper(), 0)), cleaned)


def _parse_int_value(text: str) -> tuple[int, int]:
    cleaned = (text or "").strip()
    if not cleaned:
        return (1, 0)
    try:
        return (0, int(cleaned))
    except ValueError:
        return (0, 0)


def _parse_float_value(text: str) -> tuple[int, float]:
    cleaned = (text or "").strip()
    if not cleaned:
        return (1, 0.0)
    try:
        return (0, float(cleaned))
    except ValueError:
        return (0, 0.0)


def _parse_time_value(text: str) -> tuple[int, str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return (1, "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return (0, datetime.strptime(cleaned, fmt).isoformat())
        except ValueError:
            continue
    return (0, cleaned)
```

- [ ] **Step 3: Enable sorting on the browse file table**

Update the browse table setup in `src/atv_player/ui/browse_page.py`:

```python
self.table = QTableWidget(0, 6)
self.table.setHorizontalHeaderLabels(["类型", "名称", "大小", "豆瓣ID", "评分", "时间"])
self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
configure_table_columns(self.table, stretch_column=1)
header = self.table.horizontalHeader()
header.setSectionsClickable(True)
header.sectionClicked.connect(self._sort_file_table)
```

- [ ] **Step 4: Replace raw table items with sortable items for the supported columns**

Replace `_populate_table()` in `src/atv_player/ui/browse_page.py` with:

```python
def _populate_table(self, items: list[VodItem]) -> None:
    self.table.setSortingEnabled(False)
    self.table.setRowCount(len(items))
    for row, item in enumerate(items):
        kind_text = self._item_kind(item)
        name_text = item.vod_name
        size_text = self._item_size(item)
        dbid_text = self._item_dbid(item)
        rating_text = self._item_rating(item)
        time_text = item.vod_time

        self.table.setItem(row, 0, QTableWidgetItem(kind_text))
        self.table.setItem(row, 1, SortableTableWidgetItem(name_text, name_text.casefold()))
        self.table.setItem(row, 2, SortableTableWidgetItem(size_text, _parse_size_value(size_text)))
        self.table.setItem(row, 3, SortableTableWidgetItem(dbid_text, _parse_int_value(dbid_text)))
        self.table.setItem(row, 4, SortableTableWidgetItem(rating_text, _parse_float_value(rating_text)))
        self.table.setItem(row, 5, SortableTableWidgetItem(time_text, _parse_time_value(time_text)))
    self.table.setSortingEnabled(True)
```

- [ ] **Step 5: Restrict header sorting to the intended columns and toggle sort order**

Add these fields in `__init__` after the pagination state fields:

```python
self._sortable_columns = {1, 2, 3, 4, 5}
self._sorted_column: int | None = None
self._sort_order = Qt.SortOrder.AscendingOrder
```

Then add this method inside `BrowsePage`:

```python
def _sort_file_table(self, column: int) -> None:
    if column not in self._sortable_columns:
        return
    if self._sorted_column == column:
        self._sort_order = (
            Qt.SortOrder.DescendingOrder
            if self._sort_order == Qt.SortOrder.AscendingOrder
            else Qt.SortOrder.AscendingOrder
        )
    else:
        self._sorted_column = column
        self._sort_order = Qt.SortOrder.AscendingOrder
    self.table.sortItems(column, self._sort_order)
```

This keeps `类型` inert while preserving click-to-sort behavior for columns `1..5`.

- [ ] **Step 6: Run the targeted tests to verify they now pass**

Run:

```bash
uv run pytest tests/test_browse_page_ui.py -k "sorts_current_rows_by_name or sorts_size_by_numeric_value_not_text or sorting_does_not_reload_folder_data or sorts_rows_with_empty_sortable_values_without_crashing" -v
```

Expected:

```text
4 passed
```

- [ ] **Step 7: Commit the minimal implementation**

```bash
git add src/atv_player/ui/browse_page.py tests/test_browse_page_ui.py
git commit -m "feat: add browse page local sorting"
```

### Task 3: Run The Full Browse Page Regression Check

**Files:**
- Modify: `tests/test_browse_page_ui.py` if any assertions need cleanup after the focused run
- Test: `tests/test_browse_page_ui.py`

- [ ] **Step 1: Run the full browse page UI test module**

Run:

```bash
uv run pytest tests/test_browse_page_ui.py -v
```

Expected:

```text
PASS tests/test_browse_page_ui.py::test_browse_page_sorts_current_rows_by_name_from_header_click
PASS tests/test_browse_page_ui.py::test_browse_page_sorts_size_by_numeric_value_not_text
PASS tests/test_browse_page_ui.py::test_browse_page_sorting_does_not_reload_folder_data
PASS tests/test_browse_page_ui.py::test_browse_page_sorts_rows_with_empty_sortable_values_without_crashing
```

The rest of the browse page UI tests should stay green, showing the sorting change did not break pagination, breadcrumbs, or existing table rendering.

- [ ] **Step 2: If needed, make the smallest assertion cleanup**

If any existing assertions depend on insertion order and become flaky after enabling sorting, adjust the test setup instead of weakening the assertion. Keep the test explicit, for example:

```python
page.table.setSortingEnabled(False)
page._populate_table(items)
```

Only add this if a real regression appears during the full module run.

- [ ] **Step 3: Re-run the full browse page UI test module**

Run:

```bash
uv run pytest tests/test_browse_page_ui.py -v
```

Expected:

```text
all tests passed
```

- [ ] **Step 4: Commit the regression verification adjustments**

```bash
git add src/atv_player/ui/browse_page.py tests/test_browse_page_ui.py
git commit -m "test: verify browse page sorting regressions"
```

## Self-Review

- Spec coverage:
  - Current-page-only sorting is covered by Task 1 Step 3 and Task 2 implementation steps.
  - Supported sortable columns are covered by Task 2 Step 4.
  - Empty and invalid values are covered by Task 1 Step 4 and the parsing helpers in Task 2 Step 2.
  - No backend pagination changes are preserved by only touching `BrowsePage` and UI tests.
- Placeholder scan:
  - No `TBD`, `TODO`, or “similar to above” shortcuts remain.
  - Each code-changing step includes concrete code or an exact command.
- Type consistency:
  - Plan consistently uses `BrowsePage`, `QTableWidgetItem`, `SortableTableWidgetItem`, and the existing table column indexes `0..5`.
