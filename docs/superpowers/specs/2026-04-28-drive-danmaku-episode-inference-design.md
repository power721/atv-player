# Drive Danmaku Episode Inference Design

## Summary

Improve danmaku search for drive-backed playback by inferring the requested episode from multiple signals instead of relying only on the current item title. For drive replacement playlists, the current item index and the full ordered episode list should act as the fallback source of truth when filenames do not expose an episode number.

## Goals

- Keep title-based episode extraction for normal named episodes such as `第2集`, `S01E02`, or `0002 剑来-笼中雀`.
- Infer the target episode for drive replacement playlists when the current title alone is ambiguous or non-episodic.
- Prefer playlist position as the fallback for drive scenarios where ordered file lists are more reliable than messy filenames.
- Keep the external danmaku search API unchanged: callers still pass a single search name plus `reg_src`.

## Non-Goals

- Changing provider-specific danmaku search APIs.
- Reworking danmaku ranking inside providers.
- Applying playlist-based episode inference to non-drive playback paths in this change.
- Building a fuzzy parser for every possible filename convention.

## Current Problem

`SpiderPluginController` currently builds the danmaku search name from `PlayItem.media_title` plus an episode label derived only from `PlayItem.title`. That works for titles like `25集` or `S02E25.2025.2160P`, but it fails for ordered drive playlists whose items are named like `正片`, `国语`, or `超清`. In those cases the app drops the episode suffix entirely and searches only by series title, which weakens candidate filtering and can select the wrong danmaku episode.

## Design

The change should stay focused on drive replacement playlists produced by `SpiderPluginController`.

- `src/atv_player/danmaku/utils.py`
  - expand episode extraction to recognize:
    - Chinese numbered episodes such as `第十二集`
    - leading zero-padded numbers such as `0002 剑来-笼中雀`
    - existing forms such as `S01E02`, `EP02`, `E02`, and bare numeric titles
  - add playlist-aware helpers:
    - annotate playlist items with parsed episode candidates
    - infer a target episode from:
      - current item title
      - matching playlist item title by index
      - sequence validation against playlist order
      - final `index + 1` fallback
- `src/atv_player/plugins/controller.py`
  - replace the current single-item `_extract_episode_label()` flow with a helper that receives both the current `PlayItem` and the full playlist when available
  - use the inferred target episode to build the danmaku search name for drive replacement playlist items
  - keep existing non-drive single-item behavior unchanged when no playlist context is available

## Episode Inference Priority

For drive replacement playlist items, infer the requested episode in this order:

1. Parse the current item title directly.
2. Parse the current playlist slot title using the item's `index`.
3. If known playlist episode numbers broadly match their positions, use `current.index + 1`.
4. Otherwise still fall back to `current.index + 1`.

This deliberately favors ordered playlist position over unreliable drive filenames.

## Search Name Construction

- If an inferred episode exists, build the search name as `<media title> <episode>集`.
- If no episode can be inferred, keep the existing `<media title>` fallback.
- `reg_src` behavior stays the same:
  - initial drive replacement lookup uses the drive share URL
  - later item-level lookups keep using the playable URL when that is the current source key

## Error Handling

- If playlist context is missing or inconsistent, do not fail playback or danmaku lookup.
- If episode parsing fails everywhere, return the current media-title-only search name.
- Sequence validation should be lightweight and deterministic; no heuristic should override an explicit episode number parsed from the current title.

## Testing

- utility tests for Chinese numerals and leading zero-padded numbers
- utility tests for playlist-based inference:
  - direct title hit
  - title miss with sequential playlist fallback
  - unordered or non-episodic titles falling back to `index + 1`
- spider plugin controller tests proving drive danmaku search uses:
  - parsed title episode when available
  - playlist index fallback when the current title is non-episodic
  - ordered replacement playlist context rather than media title alone
