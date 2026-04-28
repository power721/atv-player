# Danmaku Search Episode Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Search danmaku providers without episode suffixes and use the episode number only to prioritize returned candidates.

**Architecture:** Keep the change inside the danmaku package. Add small title-parsing helpers in `utils.py`, let `DanmakuService` split search keyword and episode preference, and keep provider interfaces unchanged so plugin code and cache keys remain stable.

**Tech Stack:** Python 3.14, pytest, existing danmaku service/provider modules

---

## File Structure

- Modify: `src/atv_player/danmaku/utils.py`
  Responsibility: parse and strip episode markers from titles.
- Modify: `src/atv_player/danmaku/service.py`
  Responsibility: send stripped keywords to providers and prefer matching-episode candidates.
- Modify: `src/atv_player/danmaku/providers/tencent.py`
  Responsibility: keep Tencent numeric-title expansion aligned with the stripped query keyword.
- Modify: `tests/test_danmaku_service.py`
  Responsibility: define the service-level contract for stripped search keywords and episode-aware ranking.
- Modify: `tests/test_danmaku_tencent_provider.py`
  Responsibility: define Tencent search keyword expectations under the new contract.

### Task 1: Define The New Search Contract In Tests

**Files:**
- Modify: `tests/test_danmaku_service.py`
- Modify: `tests/test_danmaku_tencent_provider.py`

- [ ] **Step 1: Write failing service tests**

Add one test that verifies providers receive `剑来 第二季` when the caller asks for `剑来 第二季 10集`.

Add one test that verifies candidates named `第10集` or `10集` rank ahead of `第9集` when the target title requests episode 10.

Add one test that verifies the service still returns candidates when none of them expose episode 10.

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `uv run pytest tests/test_danmaku_service.py tests/test_danmaku_tencent_provider.py -q`

Expected: failures showing providers still receive the full title and results are not yet episode-aware.

### Task 2: Implement Keyword Splitting And Episode Preference

**Files:**
- Modify: `src/atv_player/danmaku/utils.py`
- Modify: `src/atv_player/danmaku/service.py`
- Modify: `src/atv_player/danmaku/providers/tencent.py`

- [ ] **Step 1: Add episode parsing helpers in `utils.py`**

Implement helpers that:
- extract episode numbers from `第10集`, `10集`, `S1E10`, `EP10`, `E10`
- strip only the trailing episode marker from a normalized search title

- [ ] **Step 2: Update `DanmakuService.search_danmu()`**

Call providers with the stripped keyword, compute similarity against the stripped keyword, and prefer items whose parsed episode number matches the requested one before applying the existing ratio/provider ordering.

- [ ] **Step 3: Update Tencent numeric-title expansion**

Use the stripped query keyword as the base when converting raw numeric titles like `10` into `剑来 第二季 10集`.

### Task 3: Verify The Regression

**Files:**
- Test: `tests/test_danmaku_service.py`
- Test: `tests/test_danmaku_tencent_provider.py`

- [ ] **Step 1: Run targeted tests**

Run: `uv run pytest tests/test_danmaku_service.py tests/test_danmaku_tencent_provider.py -q`

Expected: all targeted tests pass.

- [ ] **Step 2: Run a broader danmaku regression slice**

Run: `uv run pytest tests/test_danmaku_service.py tests/test_danmaku_tencent_provider.py tests/test_danmaku_youku_provider.py -q`

Expected: all selected danmaku tests pass.
