# Douban And History Centering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Center the Douban and History page content blocks horizontally using the same bounded-container layout pattern as Browse.

**Architecture:** Keep each page's existing internal controls intact and move them into a `content_container` with a maximum width. Add a stretch-based outer layout at the page boundary so only page positioning changes.

**Tech Stack:** Python, PySide6, pytest-qt

---

### Task 1: Add centering coverage for Douban and History pages

**Files:**
- Modify: `tests/test_douban_page_ui.py`
- Modify: `tests/test_browse_page_ui.py`
- Test: `tests/test_douban_page_ui.py`
- Test: `tests/test_browse_page_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_douban_page_centers_content_container(qtbot) -> None:
    page = DoubanPage(FakeDoubanController())
    qtbot.addWidget(page)
    page.resize(2200, 1000)
    page.show()
    qtbot.waitUntil(lambda: page.category_list.count() == 2)

    container_center = page.content_container.geometry().center().x()
    page_center = page.rect().center().x()

    assert abs(container_center - page_center) <= 5


def test_history_page_centers_content_container(qtbot) -> None:
    page = HistoryPage(FakeHistoryController())
    qtbot.addWidget(page)
    page.resize(2200, 1000)
    page.show()
    qtbot.wait(50)

    container_center = page.content_container.geometry().center().x()
    page_center = page.rect().center().x()

    assert abs(container_center - page_center) <= 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_douban_page_ui.py tests/test_browse_page_ui.py -q`
Expected: FAIL because `DoubanPage` and `HistoryPage` do not yet expose `content_container`

- [ ] **Step 3: Write minimal implementation**

```python
self.content_container = QWidget()
self.content_container.setMaximumWidth(...)
...
outer_layout = QHBoxLayout(self)
outer_layout.addStretch(1)
outer_layout.addWidget(self.content_container, 100)
outer_layout.addStretch(1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_douban_page_ui.py tests/test_browse_page_ui.py -q`
Expected: PASS

### Task 2: Verify no regression in related page UI coverage

**Files:**
- Test: `tests/test_douban_page_ui.py`
- Test: `tests/test_browse_page_ui.py`

- [ ] **Step 1: Run targeted regression tests**

Run: `uv run pytest tests/test_douban_page_ui.py tests/test_browse_page_ui.py -q`
Expected: PASS
