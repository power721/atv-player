# Tencent Page Data Episodes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch Tencent episode expansion to the structured `GetPageData` API so episode searches can fetch full paginated episode lists without changing the public `search()` API.

**Architecture:** Keep `TencentDanmakuProvider.search()` and the existing search payload unchanged. Replace the candidate-page expansion path with a `GetPageData` fetch keyed by `cid`, parse every tab page, filter out previews and non-episode extras, then fall back to the existing detail-page HTML parsing only when the API path cannot produce usable episode items.

**Tech Stack:** Python, `httpx`, pytest

---

### Task 1: Add regression tests for page-data episode expansion

**Files:**
- Modify: `tests/test_danmaku_tencent_provider.py`
- Test: `tests/test_danmaku_tencent_provider.py`

- [ ] **Step 1: Write the failing test**

```python
def test_tencent_provider_search_expands_episode_list_from_page_data_api() -> None:
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_tencent_provider.py -k page_data_api -v`
Expected: FAIL because the provider still expands episode lists from detail-page HTML and never calls `GetPageData`.

- [ ] **Step 3: Write minimal implementation**

```python
def _expand_items_from_candidate_pages(...):
    expanded.extend(self._fetch_episode_items_from_page_data(...))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_tencent_provider.py -k page_data_api -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_tencent_provider.py src/atv_player/danmaku/providers/tencent.py
git commit -m "refactor: use page data api for tencent episode expansion"
```

### Task 2: Preserve fallback behavior

**Files:**
- Modify: `tests/test_danmaku_tencent_provider.py`
- Modify: `src/atv_player/danmaku/providers/tencent.py`
- Test: `tests/test_danmaku_tencent_provider.py`

- [ ] **Step 1: Write the failing test**

```python
def test_tencent_provider_search_falls_back_to_detail_html_when_page_data_is_unusable() -> None:
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_danmaku_tencent_provider.py -k "page_data or falls_back_to_detail_html" -v`
Expected: FAIL because the provider has no explicit API fallback path.

- [ ] **Step 3: Write minimal implementation**

```python
if not expanded:
    expanded.extend(self._extract_detail_episode_items(...))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_danmaku_tencent_provider.py -k "page_data or falls_back_to_detail_html" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_danmaku_tencent_provider.py src/atv_player/danmaku/providers/tencent.py
git commit -m "test: cover tencent page data fallback"
```
