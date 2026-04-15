# Search Layout Centering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Center the overall content blocks for the standalone search page and browse page without changing search behavior.

**Architecture:** Wrap each page's current content in a dedicated inner container widget with a maximum width, then center that container using an outer layout with stretch on both sides.

**Tech Stack:** Python, PySide6, pytest, pytest-qt

---

## File Structure

- Modify: `tests/test_browse_page_ui.py`
  - Add centered-layout assertions for both pages.
- Modify: `src/atv_player/ui/search_page.py`
  - Add an inner content container and center it in the page layout.
- Modify: `src/atv_player/ui/browse_page.py`
  - Add an inner content container around the top search controls and splitter, then center it.

### Task 1: Add Failing Layout Tests

**Files:**
- Modify: `tests/test_browse_page_ui.py`
- Test: `tests/test_browse_page_ui.py`

- [ ] **Step 1: Write the failing test**

```python
def test_search_page_centers_content_container(qtbot) -> None:
    ...


def test_browse_page_centers_content_container(qtbot) -> None:
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_browse_page_ui.py -k "centers_content_container" -v`
Expected: FAIL because the pages do not yet expose a centered content container.

- [ ] **Step 3: Write minimal implementation**

```python
self.content_container = QWidget()
self.content_container.setMaximumWidth(1800)

outer_layout = QHBoxLayout()
outer_layout.addStretch(1)
outer_layout.addWidget(self.content_container)
outer_layout.addStretch(1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_browse_page_ui.py -k "centers_content_container" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_browse_page_ui.py src/atv_player/ui/search_page.py src/atv_player/ui/browse_page.py
git commit -m "style: center desktop search layouts"
```
