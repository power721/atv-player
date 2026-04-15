# Async Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move both desktop search entry points to background-thread execution while preserving empty-keyword search and visible loading state.

**Architecture:** Keep `BrowseController.search()` synchronous and move threading into the widgets. Each page starts a background `threading.Thread`, emits Qt signals back to the UI thread, and ignores stale completions with a monotonically increasing request id.

**Tech Stack:** Python, PySide6, pytest, pytest-qt, threading

---

## File Structure

- Modify: `tests/test_browse_page_ui.py`
  - Add UI tests for asynchronous browse-page and standalone-search-page search flows.
- Modify: `src/atv_player/ui/search_page.py`
  - Start search in a worker thread, manage loading state, allow empty keywords, and apply results on the UI thread.
- Modify: `src/atv_player/ui/browse_page.py`
  - Start embedded search in a worker thread, manage loading state, allow empty keywords, and ignore stale completions.

### Task 1: Add Failing Async Search UI Tests

**Files:**
- Modify: `tests/test_browse_page_ui.py`
- Test: `tests/test_browse_page_ui.py`

- [ ] **Step 1: Write the failing test**

```python
def test_search_page_allows_empty_keyword_and_shows_loading_until_worker_finishes(qtbot) -> None:
    controller = BlockingSearchController()
    page = SearchPage(controller)
    qtbot.addWidget(page)
    page.keyword_edit.setText("")
    page.search()

    assert controller.calls == [""]
    assert page.status_label.text() == "搜索中..."
    assert page.search_button.isEnabled() is False


def test_browse_page_uses_latest_async_search_result(qtbot) -> None:
    controller = SequencedSearchController()
    page = BrowsePage(controller)
    qtbot.addWidget(page)
    page.keyword_edit.setText("first")
    page.search()
    page.keyword_edit.setText("second")
    page.search()

    controller.finish("second", [VodItem(vod_id="2", vod_name="Second")])
    controller.finish("first", [VodItem(vod_id="1", vod_name="First")])

    assert page.results_table.item(0, 1).text() == "Second"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_browse_page_ui.py -k "async_search or loading_until_worker_finishes or latest_async_search_result" -v`
Expected: FAIL because search still runs synchronously, blocks the UI, or rejects empty keywords.

- [ ] **Step 3: Write minimal implementation**

```python
def search(self) -> None:
    self._search_request_id += 1
    request_id = self._search_request_id
    keyword = self.keyword_edit.text().strip()
    self._set_search_loading(True)

    def run() -> None:
        try:
            results = self.controller.search(keyword)
        except UnauthorizedError:
            self._search_signals.unauthorized.emit(request_id)
            return
        except ApiError as exc:
            self._search_signals.failed.emit(request_id, str(exc))
            return
        self._search_signals.succeeded.emit(request_id, results)

    threading.Thread(target=run, daemon=True).start()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_browse_page_ui.py -k "async_search or loading_until_worker_finishes or latest_async_search_result" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_browse_page_ui.py src/atv_player/ui/search_page.py src/atv_player/ui/browse_page.py
git commit -m "feat: move desktop search to background threads"
```

### Task 2: Verify Broader UI Coverage

**Files:**
- Modify: `tests/test_browse_page_ui.py`
- Test: `tests/test_browse_page_ui.py`

- [ ] **Step 1: Write the failing test**

```python
def test_browse_page_disables_controls_during_async_search(qtbot) -> None:
    controller = BlockingSearchController()
    page = BrowsePage(controller)
    qtbot.addWidget(page)
    page.search()

    assert page.keyword_edit.isEnabled() is False
    assert page.search_button.isEnabled() is False
    assert page.filter_combo.isEnabled() is False
    assert page.clear_button.isEnabled() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_browse_page_ui.py::test_browse_page_disables_controls_during_async_search -v`
Expected: FAIL because the current widget leaves controls enabled or does not expose loading state during async work.

- [ ] **Step 3: Write minimal implementation**

```python
def _set_search_loading(self, loading: bool) -> None:
    self.keyword_edit.setEnabled(not loading)
    self.search_button.setEnabled(not loading)
    self.filter_combo.setEnabled(not loading)
    self.clear_button.setEnabled(not loading)
    if loading:
        self.status_label.setText("搜索中...")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_browse_page_ui.py::test_browse_page_disables_controls_during_async_search -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_browse_page_ui.py src/atv_player/ui/search_page.py src/atv_player/ui/browse_page.py
git commit -m "test: cover desktop async search loading state"
```
