# Danmaku Source Duration Display Design

## Summary

Show each danmaku source search result's duration in the player window's danmaku source dialog so users can distinguish full-length matches from short clips and previews more quickly.

## Goals

- Display source duration directly in the danmaku source option list.
- Reuse the existing `duration_seconds` data already carried by `DanmakuSourceOption`.
- Keep the change display-only inside the player dialog.
- Hide duration when it is unknown or non-positive.

## Non-Goals

- Changing provider search behavior or ranking.
- Adding new duration fields to models or caches.
- Changing the provider-group list on the left side of the dialog.
- Adding extra columns, tooltips, or secondary metadata rows.

## Scope

Primary implementation should live in:

- `src/atv_player/ui/player_window.py`

Primary verification should live in:

- `tests/test_player_window_ui.py`

## UI Decision

Render each candidate option in the right-side list as:

- `<title> · <formatted duration>`

Examples:

- `红果短剧 第1集 · 24:18`
- `疯狂动物城2 · 1:38:55`

If `duration_seconds <= 0`, keep the current title-only display.

## Formatting Rules

- Use `MM:SS` when duration is below one hour.
- Use `H:MM:SS` when duration is one hour or longer.
- Do not show leading hour zeros.
- Do not round up; use integer seconds formatting.

## Implementation Notes

- Add a small private formatting helper in `PlayerWindow` near the danmaku source dialog helpers.
- Keep the underlying selected URL logic unchanged by continuing to store `option.url` in `UserRole`.
- Only the visible `QListWidgetItem` text changes.

## Test Plan

Add UI coverage proving:

- options with a positive duration render the formatted duration suffix
- options with an unknown duration keep the old title-only text
- the selected URL stored in `UserRole` still matches the chosen option
