# Player Window Detail Design

## Summary

Add a metadata-focused detail view to the player window sidebar so the current title's video details are readable while preserving a separate area for playback status and error logs.

This change is intentionally narrow. It applies only to the player window sidebar and the data already available when playback starts.

## Goals

- Make the player window sidebar show title-level metadata as the primary detail content.
- Keep playback logs available without letting runtime messages overwrite the title metadata.
- Reuse the existing sidebar structure and fullscreen behavior.
- Render metadata from backend detail fields that are already returned through the existing detail flow.
- Tolerate missing metadata fields without breaking playback or producing fake values.

## Non-Goals

- Add new backend endpoints or change the existing API contract.
- Redesign the overall player window shell or bottom playback controls.
- Make current-episode or current-file metadata the main detail presentation.
- Add rich poster artwork or a custom card UI beyond the existing Qt sidebar widgets.
- Invent fallback values for fields the backend does not return.

## Scope

Primary implementation lives in `src/atv_player/ui/player_window.py`.

Supporting data-model changes live in `src/atv_player/models.py` and `src/atv_player/controllers/browse_controller.py`.

Primary verification lives in `tests/test_player_window_ui.py` and `tests/test_browse_controller.py`.

## Design

### Sidebar Structure

Keep the existing right sidebar and its toggle behavior:

- The sidebar still contains the playlist area and a details area.
- The details area becomes a container with two logical sections.
- The upper section is the title metadata view and is the dominant area.
- The lower section is a playback log view for status messages and failures.

Existing fullscreen and wide-mode behavior stays the same:

- Entering fullscreen hides the sidebar and both detail sub-sections.
- Toggling the details pane still hides or shows the entire detail container.

### Metadata Content

The metadata section presents title-level fields in this order:

1. Name
2. Type
3. Year
4. Region
5. Language
6. Rating
7. Director
8. Cast
9. Douban ID
10. Synopsis

Rendering rules:

- Use backend-returned values only.
- If a field is missing or empty, leave it blank or omit its value text without fabricating content.
- Synopsis supports multi-line display and scrolling.
- The metadata section should remain readable for long text, especially synopsis and cast values.

### Data Mapping

The player window should render metadata from `session.vod`, not from hardcoded sample text.

To support that, extend `VodItem` and the browse/detail mapping so title metadata fields are preserved when detail payloads are converted into application models.

The design assumes the backend detail payload may include fields equivalent to:

- type / category label
- year
- area / region
- language
- rating / remarks
- director
- actor / cast
- dbid
- content / synopsis

Field names in Python models should follow the project's existing `VodItem` style and remain narrowly scoped to the metadata needed by the player window.

### Log Separation

Playback logs must no longer share the same text area as title metadata.

Required behavior:

- Opening a session refreshes the metadata section for the current title.
- Runtime messages such as playback failures, resume failures, seek failures, mute failures, speed failures, volume failures, and progress-report failures append only to the log section.
- Loading another playlist item within the same session does not replace the title metadata with episode-level information.
- Starting a new session replaces the metadata with the new title's details and keeps log handling intact.

### UI Approach

Use simple Qt widgets that fit the current player window style:

- Keep the current splitter-based sidebar layout.
- Replace the single `QTextEdit` detail view with a small composite container, such as read-only text widgets or labels arranged vertically.
- The metadata section should visually dominate the log section through layout size allocation rather than custom styling complexity.

The design favors clarity and maintainability over a highly customized widget tree.

## Error Handling

- Missing metadata fields must not raise errors when opening playback.
- If metadata is incomplete, the player still opens and the metadata view shows whatever fields are available.
- Existing playback/log errors remain visible in the log section.

## Testing Strategy

Add or update focused tests to cover:

- detail payload mapping preserves the metadata fields needed by the player window
- player window metadata renders title-level fields in the expected order
- missing metadata fields are tolerated without crashes
- opening a new session refreshes metadata for the new title
- playback/log failures append to the log section and do not overwrite the metadata section

## Implementation Order

1. Extend `VodItem` and detail mapping to preserve player metadata fields.
2. Split the player details area into metadata and log sub-sections.
3. Render title metadata from `session.vod` using the agreed field order.
4. Route existing runtime log messages to the dedicated log view.
5. Add or update focused controller and player-window tests.
