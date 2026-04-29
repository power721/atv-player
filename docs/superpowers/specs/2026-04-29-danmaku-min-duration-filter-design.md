# Danmaku Minimum Duration Filter Design

## Summary

Exclude danmaku search candidates whose known runtime is shorter than 5 minutes. Apply the filter once in the shared danmaku service so automatic resolution, manual refresh, cached reranking, and provider fallback all operate on the same candidate set.

## Goals

- Remove candidates with `0 < duration_seconds < 300` from danmaku search results.
- Keep candidates with unknown duration (`duration_seconds <= 0`) eligible.
- Preserve the existing provider search flow, episode matching, and duration-based reranking for the remaining candidates.
- Keep the change contained to the shared danmaku search pipeline.

## Non-Goals

- Changing provider search request parameters.
- Adding a user-facing preference or toggle for the minimum duration threshold.
- Filtering candidates in the UI layer only.
- Changing danmaku XML resolution behavior.

## Scope

Primary implementation should live in:

- `src/atv_player/danmaku/service.py`

Primary verification should live in:

- `tests/test_danmaku_service.py`

## Behavior

After provider results are collected and normalized into `DanmakuSearchItem` values, the service should remove candidates whose duration is known and shorter than 5 minutes.

Filter rule:

- exclude when `duration_seconds > 0 and duration_seconds < 300`
- keep when `duration_seconds >= 300`
- keep when `duration_seconds <= 0`

The filter is unconditional. It applies to explicit episode requests, implicit episode requests, movie-like title-only requests, and cross-provider fallback results.

## Placement

The filter should run inside `DanmakuService.search_danmu()` after `_collect_search_results(...)` returns the flattened result set and before later episode-specific filtering and sorting decisions finalize the list.

Reasoning:

- provider implementations already expose `duration_seconds` through a shared model
- one service-level filter keeps behavior consistent across all providers
- the controller and UI should not need separate filtering rules

## Interaction With Existing Ranking

The new minimum-duration filter is stricter than ranking. Short known-duration candidates should be removed before:

- explicit episode matching fallback
- implicit long-form heuristics
- final similarity and provider-order sorting
- duration-aware reranking of grouped source options

This means a 2-minute candidate that would previously win on title similarity or duration proximity should no longer appear at all.

## Unknown Duration Handling

Unknown-duration candidates stay in the result set. The service already needs to tolerate providers that do not expose duration metadata reliably, so the filter must not treat missing duration as a rejection.

## Error Handling

- If every candidate is filtered out, return the remaining result according to existing empty-list behavior.
- Do not raise a new exception for filtered results.
- Treat malformed or non-positive durations the same way current code does: as unknown duration.

## Test Plan

Add service coverage proving:

- a known 299-second candidate is removed
- a known 300-second candidate is retained
- an unknown-duration candidate is retained
- the filter applies before final ordering, so only surviving candidates appear in the returned list
