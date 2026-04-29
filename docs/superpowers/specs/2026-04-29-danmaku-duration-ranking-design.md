# Danmaku Duration Ranking Design

## Summary

Use the current media duration as an optional ranking signal when presenting danmaku search candidates. The service should reorder the full candidate list and the default selected source by proximity to the current media duration, while preserving existing title and episode matching behavior.

## Goals

- Accept an optional media duration input for danmaku source search.
- Reorder the entire candidate list by duration proximity when the duration is known.
- Keep the default selected danmaku source aligned with the reordered list.
- Preserve current provider search behavior and current fallback behavior when duration is unknown.
- Prefer existing metadata sources before reading runtime player state.

## Non-Goals

- Changing provider-specific search requests or parsing logic.
- Adding a new global config for duration ranking.
- Persisting media duration in danmaku cache payloads.
- Reworking danmaku XML resolution or subtitle rendering.

## Scope

Primary implementation should live in:

- `src/atv_player/danmaku/service.py`
- `src/atv_player/plugins/controller.py`
- `src/atv_player/models.py`
- `src/atv_player/ui/player_window.py`

Primary verification should live in:

- `tests/test_danmaku_service.py`
- `tests/test_spider_plugin_controller.py`
- `tests/test_player_window_ui.py`

## Input Contract

Add an optional `media_duration_seconds: int = 0` parameter to:

- `DanmakuService.search_danmu_sources(...)`

`0` means unknown duration and must preserve the current ranking behavior exactly.

No provider API changes are required. Provider `search()` methods continue returning candidate durations through `DanmakuSearchItem.duration_seconds`.

## Source Of Media Duration

Duration should be supplied from the highest-confidence source already available at the call site.

Priority:

1. `PlayItem.duration_seconds` when the playback item already carries metadata duration.
2. Current player runtime duration when the user manually refreshes danmaku sources from the player window and the playback engine has a positive duration.
3. `0` when neither source is available.

To support this, add:

- `duration_seconds: int = 0` to `PlayItem`

This keeps the service API simple and avoids hard-coupling the danmaku layer to the player object.

## Controller Flow

`SpiderPluginController._populate_danmaku_candidates(...)` should accept or derive `media_duration_seconds` and pass it through to `search_danmu_sources(...)`.

Default path:

- use `item.duration_seconds` if positive

Manual refresh path from the player window:

- pass the current playback duration if positive
- otherwise fall back to `item.duration_seconds`

The controller remains the aggregation layer responsible for combining item metadata, user preference history, and danmaku service ranking.

## Ranking Rules

Duration ranking is a refinement step, not a replacement for existing matching logic.

Existing rules that must remain stronger than duration:

- explicit episode matching for explicit episode requests
- title filtering and sequel mismatch rejection
- provider history and exact historical page URL preference

When `media_duration_seconds > 0`, candidates that also have `duration_seconds > 0` should be ranked by absolute duration difference:

- smaller `abs(candidate.duration_seconds - media_duration_seconds)` is better

Candidates with unknown or non-positive duration should rank after candidates with known duration when all higher-priority matching signals are equal.

### Explicit Episode Requests

For queries like `遮天 88集`:

1. keep exact requested-episode matches ahead of non-matching results
2. among exact episode matches, prefer the candidate closest to `media_duration_seconds`
3. only then fall back to existing similarity and provider order tie-breakers

### Title-Only Or Implicit Requests

For movie-style or implicit-index requests:

1. keep current title and long-form heuristics intact
2. among candidates at the same relevance tier, prefer the candidate closest to `media_duration_seconds`
3. if no target duration is available, preserve the current order

## Group Ordering And Default Selection

`search_danmu_sources(...)` currently groups already-sorted flat results. After adding duration-aware ranking:

- the order of options inside each provider group should reflect the new duration-aware sort
- the order of provider groups should continue to be driven by the first surviving option for each provider
- `default_option_url` and `default_provider` should be selected from the reordered results so the default choice matches the visible list

## Caching Behavior

Do not add `media_duration_seconds` to the danmaku source cache key or cache payload.

Reasoning:

- media duration is local playback context, not provider search identity
- the same query should still reuse cached source groups
- duration-aware reordering should be recomputed when applying fresh search results in memory

This design intentionally avoids cache invalidation complexity.

## Error Handling

- If `media_duration_seconds <= 0`, skip duration-aware ranking entirely.
- If a candidate has invalid or missing `duration_seconds`, treat it as unknown and keep it eligible.
- Duration ranking must never remove a candidate by itself; it only reorders candidates that already passed filtering.

## Test Plan

Add coverage proving:

- `search_danmu_sources()` reorders candidates by duration proximity when a target duration is provided
- explicit episode requests still keep the correct episode ahead of title-only candidates, with duration deciding ties within the matching tier
- unknown target duration preserves the existing order
- controller passes `PlayItem.duration_seconds` into the danmaku service
- player-window manual refresh passes runtime player duration when available

