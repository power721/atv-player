# Danmaku Provider Original Name Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pass both the stripped provider search keyword and the original playback title through danmaku search so Tencent can still expand to the requested episode.

**Architecture:** Keep `DanmakuService` responsible for normalizing the title, extracting the requested episode, and deriving the stripped provider query. Extend the provider search interface to optionally accept `original_name`, then use that extra context only in `TencentDanmakuProvider` when deciding whether candidate pages need episode expansion.

**Tech Stack:** Python, pytest, existing danmaku service/provider modules

---

### Task 1: Lock The Interface In Tests

**Files:**
- Modify: `tests/test_danmaku_service.py`
- Modify: `tests/test_danmaku_tencent_provider.py`

- [ ] **Step 1: Write failing tests**

Add a service-level regression test that asserts the provider receives `query_name="白日提灯"` and `original_name="白日提灯 20集"`.

Add a Tencent regression test that asserts a stripped query still expands candidates until `20集` is present when the original title carries the episode suffix.

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `uv run pytest tests/test_danmaku_service.py tests/test_danmaku_tencent_provider.py -k "original_name or stripped_query" -v`
Expected: FAIL because providers still accept only one positional title and Tencent cannot see the requested episode after stripping.

### Task 2: Extend The Search Interface

**Files:**
- Modify: `src/atv_player/danmaku/providers/base.py`
- Modify: `src/atv_player/danmaku/service.py`
- Modify: `src/atv_player/danmaku/providers/tencent.py`
- Modify: `src/atv_player/danmaku/providers/youku.py`
- Modify: `src/atv_player/danmaku/providers/bilibili.py`
- Modify: `src/atv_player/danmaku/providers/iqiyi.py`
- Modify: `src/atv_player/danmaku/providers/mgtv.py`

- [ ] **Step 1: Update provider signatures**

Make every provider accept `search(query_name: str, original_name: str | None = None)`.

- [ ] **Step 2: Update service dispatch**

Pass `search_keyword` as `query_name` and the normalized playback title as `original_name`.

- [ ] **Step 3: Update Tencent expansion logic**

Use `original_name or query_name` when deriving the requested episode for candidate-page expansion, while still using `query_name` for keyword-based name rebuilding.

### Task 3: Verify The Regression

**Files:**
- Test: `tests/test_danmaku_service.py`
- Test: `tests/test_danmaku_tencent_provider.py`

- [ ] **Step 1: Run targeted tests**

Run: `uv run pytest tests/test_danmaku_service.py tests/test_danmaku_tencent_provider.py -v`
Expected: PASS

- [ ] **Step 2: Run a broader danmaku slice**

Run: `uv run pytest tests/test_danmaku_service.py tests/test_danmaku_tencent_provider.py tests/test_danmaku_youku_provider.py tests/test_danmaku_iqiyi_provider.py tests/test_danmaku_mgtv_provider.py tests/test_danmaku_bilibili_provider.py -q`
Expected: PASS
