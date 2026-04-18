# Live EPG Channel Alias Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a narrow EPG-only alias mapping so custom live channels named `CCTV-1综合高清` can match XMLTV channels named `CCTV1综合` without changing any visible channel names.

**Architecture:** Keep all alias behavior inside `LiveEpgService` so `CustomLiveService` and the UI continue to pass and display the original channel name. Extend `_match_channel_id()` with a small in-memory alias map and alias fallback that only runs after the existing exact and normalized matching paths fail.

**Tech Stack:** Python 3.13, pytest, XMLTV parsing in `LiveEpgService`

---

## File Map

- Modify: `src/atv_player/live_epg_service.py`
  Responsibility: hold the alias table, derive alias candidates from the incoming channel name, and keep direct-match precedence ahead of alias fallback.
- Modify: `tests/test_live_epg_service.py`
  Responsibility: prove the new alias-only match fails before implementation, then verify the alias behavior and direct-match precedence after implementation.
- Create: `docs/superpowers/plans/2026-04-18-live-epg-channel-alias.md`
  Responsibility: capture the execution plan for the approved design.

### Task 1: Add Alias-Only EPG Regression Test

**Files:**
- Modify: `tests/test_live_epg_service.py`
- Test: `tests/test_live_epg_service.py::test_live_epg_service_matches_channel_names_via_alias_map`

- [ ] **Step 1: Write the failing test**

Add this test below `test_live_epg_service_matches_cctv_names_after_normalization` in `tests/test_live_epg_service.py`:

```python
def test_live_epg_service_matches_channel_names_via_alias_map(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_refresh_result(
        cache_text=(
            "<tv>"
            '<channel id="c1"><display-name>CCTV1综合</display-name></channel>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="c1"><title>朝闻天下</title></programme>'
            "</tv>"
        ),
        last_refreshed_at=1,
        last_error="",
    )
    service = LiveEpgService(repo, FakeHttpBytesClient())

    schedule = service.get_schedule("CCTV-1综合高清", now_text="2026-04-18T09:30:00+08:00")

    assert schedule is not None
    assert schedule.current == "09:00-10:00 朝闻天下"
    assert schedule.upcoming == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/test_live_epg_service.py::test_live_epg_service_matches_channel_names_via_alias_map -v
```

Expected: `FAIL` because `schedule` is `None` under the current implementation.

- [ ] **Step 3: Write the minimal implementation**

Update `src/atv_player/live_epg_service.py` with the smallest alias-aware match path:

```python
class LiveEpgService:
    _RESOLUTION_SUFFIX_PATTERN = re.compile(r"(hd|uhd|fhd|高清|超清|标清)+$", re.IGNORECASE)
    _CHANNEL_ALIASES = {
        "CCTV-1综合高清": "CCTV1综合",
    }

    def _match_channel_id(self, channel_name: str, channel_names_by_id: dict[str, list[str]]) -> str:
        target = channel_name.strip()
        if not target:
            return ""
        for channel_id, names in channel_names_by_id.items():
            if target in names:
                return channel_id
        normalized_target = self._normalize_name(target)
        for channel_id, names in channel_names_by_id.items():
            if any(self._normalize_name(name) == normalized_target for name in names):
                return channel_id
        for alias in self._alias_candidates(target, normalized_target):
            for channel_id, names in channel_names_by_id.items():
                if alias in names:
                    return channel_id
            normalized_alias = self._normalize_name(alias)
            for channel_id, names in channel_names_by_id.items():
                if any(self._normalize_name(name) == normalized_alias for name in names):
                    return channel_id
        return ""

    def _alias_candidates(self, target: str, normalized_target: str) -> list[str]:
        candidates: list[str] = []
        direct_alias = self._CHANNEL_ALIASES.get(target)
        if direct_alias:
            candidates.append(direct_alias)
        for source_name, alias_name in self._CHANNEL_ALIASES.items():
            if self._normalize_name(source_name) == normalized_target and alias_name not in candidates:
                candidates.append(alias_name)
        return candidates
```

Implementation constraints:

- keep `_CHANNEL_ALIASES` small and explicit
- do not change `_normalize_name()`
- do not touch `CustomLiveService`
- do not rewrite the incoming `channel_name`; only use aliases during lookup

- [ ] **Step 4: Run the focused test to verify it passes**

Run:

```bash
uv run pytest tests/test_live_epg_service.py::test_live_epg_service_matches_channel_names_via_alias_map -v
```

Expected: `PASS`

- [ ] **Step 5: Commit**

Run:

```bash
git add tests/test_live_epg_service.py src/atv_player/live_epg_service.py
git commit -m "feat: add live epg channel aliases"
```

### Task 2: Guard Direct-Match Precedence And Run Focused Regression

**Files:**
- Modify: `tests/test_live_epg_service.py`
- Test: `tests/test_live_epg_service.py::test_live_epg_service_prefers_direct_match_before_alias_lookup`

- [ ] **Step 1: Write the regression test**

Add this test below the alias-map test in `tests/test_live_epg_service.py`:

```python
def test_live_epg_service_prefers_direct_match_before_alias_lookup(tmp_path: Path) -> None:
    repo = LiveEpgRepository(tmp_path / "app.db")
    repo.save_refresh_result(
        cache_text=(
            "<tv>"
            '<channel id="direct"><display-name>CCTV-1综合高清</display-name></channel>'
            '<channel id="alias"><display-name>CCTV1综合</display-name></channel>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="direct"><title>直接命中节目</title></programme>'
            '<programme start="20260418090000 +0800" stop="20260418100000 +0800" channel="alias"><title>别名节目</title></programme>'
            "</tv>"
        ),
        last_refreshed_at=1,
        last_error="",
    )
    service = LiveEpgService(repo, FakeHttpBytesClient())

    schedule = service.get_schedule("CCTV-1综合高清", now_text="2026-04-18T09:30:00+08:00")

    assert schedule is not None
    assert schedule.current == "09:00-10:00 直接命中节目"
    assert schedule.upcoming == []
```

- [ ] **Step 2: Run the focused regression slice**

Run:

```bash
uv run pytest \
  tests/test_live_epg_service.py::test_live_epg_service_matches_cctv_names_after_normalization \
  tests/test_live_epg_service.py::test_live_epg_service_matches_channel_names_via_alias_map \
  tests/test_live_epg_service.py::test_live_epg_service_prefers_direct_match_before_alias_lookup -v
```

Expected:

- all three tests `PASS`
- the existing normalization-only behavior still matches `CCTV-1综合` to `CCTV1综合`
- the new alias-only behavior matches `CCTV-1综合高清` to `CCTV1综合`
- the direct XMLTV name still wins over the alias fallback

- [ ] **Step 3: Run the full EPG test file**

Run:

```bash
uv run pytest tests/test_live_epg_service.py -v
```

Expected: `PASS` for the whole file with no changes required outside `LiveEpgService`

- [ ] **Step 4: Commit**

Run:

```bash
git add tests/test_live_epg_service.py
git commit -m "test: cover live epg alias precedence"
```

## Self-Review

- Spec coverage: the plan adds the explicit alias table, keeps the change scoped to `LiveEpgService`, preserves visible channel names by avoiding `CustomLiveService` edits, and verifies both alias-only matching and direct-match precedence.
- Placeholder scan: no `TODO`, `TBD`, or undefined “handle appropriately” style steps remain; every code-changing step includes concrete code or commands.
- Type consistency: the plan only introduces `_CHANNEL_ALIASES` and `_alias_candidates()` inside `LiveEpgService`, and every test continues to call the existing `get_schedule(channel_name, now_text=...)` API.
