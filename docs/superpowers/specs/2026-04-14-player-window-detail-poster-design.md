# Player Window Detail Poster Design

## Summary

Add a static poster area at the top of the player window detail pane so title artwork appears above the existing metadata and playback log sections.

This change is intentionally narrow. It builds on the existing player detail layout without changing the overall player shell or adding new interaction modes.

## Goals

- Show the current title poster at the very top of the player detail pane.
- Keep the existing metadata section below the poster and the log section below the metadata.
- Use a fixed-size, centered poster presentation that reads like a cover card rather than a full-width banner.
- Preserve layout stability when `vod_pic` is missing by keeping the poster area reserved.
- Keep the poster static; no click-to-preview or fullscreen image behavior.

## Non-Goals

- Add poster preview, zoom, click handling, or navigation.
- Redesign the metadata field order or the playback log behavior.
- Add custom loading spinners, placeholder text, or fallback artwork.
- Introduce a network image loading subsystem unless existing Qt loading is already sufficient.
- Change fullscreen behavior for the sidebar.

## Scope

Primary implementation lives in `src/atv_player/ui/player_window.py`.

Primary verification lives in `tests/test_player_window_ui.py`.

If a small helper is needed to normalize poster input into a local pixmap or resource path, keep it inside `player_window.py` unless the logic grows beyond a few focused functions.

## Design

### Layout Placement

The detail pane becomes a three-part vertical stack:

1. Poster area
2. Metadata area
3. Playback log area

The poster area sits above the existing metadata view and remains part of the same toggle-controlled detail container.

Existing detail toggle, wide mode, and fullscreen behavior remain unchanged:

- Hiding the detail pane hides the poster together with metadata and logs.
- Entering fullscreen still hides the entire detail pane.

### Poster Presentation

Use a fixed-size, centered poster presentation:

- The poster is shown inside a dedicated widget at the top of the detail pane.
- The poster widget keeps a fixed visual footprint even when no poster exists.
- The image is centered horizontally.
- The image behaves like a cover card rather than stretching edge-to-edge.

The poster area should feel intentionally reserved, not collapsed away.

### Missing Poster Behavior

When `session.vod.vod_pic` is empty or cannot be rendered:

- Keep the poster area visible at the same size.
- Do not display placeholder text such as "暂无海报".
- Do not replace it with fallback art.
- Leave the area visually empty.

This keeps the layout stable while matching the requested no-placeholder behavior.

### Image Loading Boundary

The first implementation should prefer the narrowest viable loading path:

- If `vod_pic` already resolves to something Qt can load directly, render it as-is.
- If lightweight normalization is needed, keep it local to `PlayerWindow`.
- Do not add a broad asynchronous image-fetching system as part of this change.

If a poster value cannot be rendered through the narrow path, the area remains empty rather than expanding scope.

### UI Approach

Use existing Qt primitives:

- A dedicated poster `QLabel` or similarly small widget for image display.
- Existing text widgets for metadata and logs.
- Vertical layout sizing to keep metadata dominant and logs secondary.

This change should remain layout-driven and maintainable, not style-heavy.

## Error Handling

- Missing `vod_pic` must not raise errors.
- Unrenderable poster input must fail closed to an empty reserved area.
- Metadata and playback logs must continue to render even if the poster cannot be shown.

## Testing Strategy

Add or update focused UI tests to cover:

- the detail pane includes a poster widget above the metadata and log views
- opening a session with `vod_pic` updates the poster area
- opening a session without `vod_pic` preserves the reserved poster area without placeholder text
- metadata and log behavior remain unchanged when the poster area is added

## Implementation Order

1. Add failing player-window layout tests for the new poster area.
2. Add failing player-window behavior tests for poster-present and poster-missing sessions.
3. Implement the poster widget and top-of-pane layout.
4. Render poster state from `session.vod.vod_pic` with empty-area fallback.
5. Run the focused player-window test suite and verify existing detail tests still pass.
