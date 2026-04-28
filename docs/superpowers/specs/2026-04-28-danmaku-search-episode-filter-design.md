# Danmaku Search Episode Filter Design

## Summary

Adjust danmaku candidate lookup so provider search requests use the series title without the episode suffix. The episode number should be parsed from the original playback title and used only to rank and filter returned candidates.

## Goals

- Stop sending `第N集` style suffixes to provider search endpoints.
- Keep using the full playback title as the external caller contract so plugin code and cache keys do not change.
- Prefer candidates whose titles match the requested episode number.
- Fall back to the existing similarity-based ranking when no candidate exposes the requested episode.

## Non-Goals

- Changing danmaku cache key behavior.
- Reworking playback-side danmaku trigger logic.
- Adding provider-specific metadata fields or a new provider API.

## Design

The change stays inside the danmaku package.

- `src/atv_player/danmaku/utils.py`
  - add helpers to extract an episode number from names like `第10集`, `10集`, `S1E10`, `EP10`
  - add a helper to strip the episode suffix from a search keyword while preserving season text such as `第二季`
- `src/atv_player/danmaku/service.py`
  - normalize the incoming name as today
  - derive a provider search keyword without episode suffix
  - call all providers with the stripped keyword
  - if the original title contains an episode number, prefer candidates with the same parsed episode number
  - otherwise keep existing similarity sorting
- `src/atv_player/danmaku/providers/tencent.py`
  - keep numeric-title expansion for display names
  - base the expansion on the stripped query keyword instead of the full original title
- `src/atv_player/danmaku/providers/youku.py`
  - no behavior change beyond receiving the stripped keyword from the service

## Testing

- service test proving provider `search()` receives `剑来 第二季` rather than `剑来 第二季 10集`
- service test proving matching-episode candidates rank ahead of mismatched episodes
- service test proving non-matching episodes still fall back to similarity ordering
- Tencent provider test updates for the new stripped search keyword contract
