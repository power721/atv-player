# Danmaku Minimum Duration Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exclude danmaku search candidates whose known duration is shorter than 5 minutes while keeping unknown-duration candidates available.

**Architecture:** Add a single service-level filter in `DanmakuService.search_danmu()` so all provider results pass through one minimum-duration rule before episode filtering and ranking. Cover the behavior with focused service tests that prove the exact threshold and unknown-duration handling.

**Tech Stack:** Python, pytest

---

### Task 1: Add failing service tests for the minimum-duration rule

**Files:**
- Modify: `tests/test_danmaku_service.py`
- Test: `tests/test_danmaku_service.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_search_danmu_filters_out_known_candidates_shorter_than_five_minutes() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(provider="tencent", name="疯狂动物城2", url="https://v.qq.com/short", ratio=0.99, simi=0.99, duration_seconds=299),
            DanmakuSearchItem(provider="tencent", name="疯狂动物城2", url="https://v.qq.com/keep", ratio=0.95, simi=0.95, duration_seconds=5935),
            DanmakuSearchItem(provider="tencent", name="疯狂动物城2", url="https://v.qq.com/unknown", ratio=0.90, simi=0.90, duration_seconds=0),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    results = service.search_danmu("疯狂动物城2")

    assert [item.url for item in results] == ["https://v.qq.com/keep", "https://v.qq.com/unknown"]


def test_search_danmu_keeps_candidates_at_five_minutes_or_longer() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(provider="tencent", name="疯狂动物城2", url="https://v.qq.com/exact", ratio=0.99, simi=0.99, duration_seconds=300),
            DanmakuSearchItem(provider="tencent", name="疯狂动物城2", url="https://v.qq.com/long", ratio=0.95, simi=0.95, duration_seconds=600),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    results = service.search_danmu("疯狂动物城2")

    assert [item.url for item in results] == ["https://v.qq.com/long", "https://v.qq.com/exact"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_danmaku_service.py -k "five_minutes or shorter_than_five_minutes" -v`
Expected: `FAIL` because the service still returns the 299-second candidate.

- [ ] **Step 3: Commit the red test state only if your workflow requires it**

```bash
git diff -- tests/test_danmaku_service.py
```

### Task 2: Implement the shared minimum-duration filter

**Files:**
- Modify: `src/atv_player/danmaku/service.py`
- Test: `tests/test_danmaku_service.py`

- [ ] **Step 1: Add the minimum-duration constant and helper**

```python
_MIN_DANMAKU_CANDIDATE_DURATION_SECONDS = 300


def _filter_too_short_duration_candidates(items: list[DanmakuSearchItem]) -> list[DanmakuSearchItem]:
    return [
        item
        for item in items
        if item.duration_seconds <= 0 or item.duration_seconds >= _MIN_DANMAKU_CANDIDATE_DURATION_SECONDS
    ]
```

- [ ] **Step 2: Apply the helper in `search_danmu()` immediately after collecting provider results**

```python
results = self._collect_search_results(provider_keys, primary_query, normalized)
results = _filter_too_short_duration_candidates(results)
```

- [ ] **Step 3: Run the focused tests to verify they pass**

Run: `uv run pytest tests/test_danmaku_service.py -k "five_minutes or shorter_than_five_minutes" -v`
Expected: `PASS`

### Task 3: Verify existing danmaku service behavior still passes

**Files:**
- Modify: `tests/test_danmaku_service.py` if test expectations need tightening
- Test: `tests/test_danmaku_service.py`

- [ ] **Step 1: Run the full danmaku service test file**

Run: `uv run pytest tests/test_danmaku_service.py -v`
Expected: `PASS`

- [ ] **Step 2: Review for regressions**

Check that:

- explicit episode matching tests still pass
- duration-aware reranking tests still pass
- provider fallback tests still pass

- [ ] **Step 3: Commit the implementation**

```bash
git add docs/superpowers/specs/2026-04-29-danmaku-min-duration-filter-design.md docs/superpowers/plans/2026-04-29-danmaku-min-duration-filter.md tests/test_danmaku_service.py src/atv_player/danmaku/service.py
git commit -m "feat: filter short danmaku search candidates"
```
