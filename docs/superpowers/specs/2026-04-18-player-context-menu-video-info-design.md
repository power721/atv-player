# Player Context Menu Video Info Design

## Summary

Add a top-level `视频信息` action to the existing video-surface right-click menu in the player window.

The action provides a mouse-accessible entry point for mpv's built-in playback information overlay without changing the existing keyboard shortcuts or the rest of the context-menu structure.

## Goals

- Add a top-level `视频信息` action to the existing video context menu.
- Make the action toggle mpv's built-in playback information overlay, matching the persistent behavior of the `I` key.
- Keep the implementation scoped to `PlayerWindow` and the mpv wrapper.
- Keep failures non-fatal by logging them without interrupting playback.

## Non-Goals

- Add a submenu for multiple information pages.
- Replicate mpv statistics formatting in Qt widgets maintained by the application.
- Change or replace existing mpv keyboard shortcuts such as `i` or `I`.
- Persist overlay visibility state to `AppConfig`, playback history, or global mpv configuration.
- Redesign the existing right-click menu beyond inserting one new top-level action.

## Scope

Primary implementation lives in `src/atv_player/player/mpv_widget.py` and `src/atv_player/ui/player_window.py`.

Primary verification lives in `tests/test_mpv_widget.py` and `tests/test_player_window_ui.py`.

No controller, API, or storage changes are required.

## Design

### Menu Placement

Extend the existing video-surface right-click menu with one additional top-level action:

- `主字幕`
- `次字幕`
- `主字幕位置`
- `次字幕位置`
- `主字幕大小`
- `次字幕大小`
- `音轨`
- `视频信息`

`视频信息` is a plain action, not a submenu and not a checkable menu item.

This keeps the menu aligned with the user's request and avoids introducing a stateful checked item that would require extra mpv overlay state tracking.

### Action Behavior

Selecting `视频信息` toggles mpv's built-in stats overlay in the same persistent way as the `I` key:

- if the overlay is hidden, show it
- if the overlay is visible, hide it

The action should target mpv's stats overlay page that corresponds to the usual playback information view. No additional page-switching UI is added to the menu.

The application should not attempt to keep its own overlay-visible boolean in sync with mpv. Each menu click simply delegates the toggle request to mpv.

### Player Integration

Extend `MpvWidget` with a narrow helper dedicated to toggling video information display rather than issuing raw mpv commands from `PlayerWindow`.

The wrapper should provide:

- a method that requests mpv to toggle the built-in stats overlay

`PlayerWindow` remains responsible for wiring the menu action and logging failures. `MpvWidget` remains responsible for the mpv-facing command shape.

### Error Handling

If mpv rejects the overlay toggle request:

- keep playback running
- append a concise error line to the playback log
- leave the menu structure unchanged

Suggested log line:

- `视频信息显示失败: ...`

### Compatibility

Existing context-menu behavior for subtitles, subtitle position, subtitle size, and audio remains unchanged.

Existing keyboard behavior remains unchanged:

- `i` continues to show the temporary stats overlay
- `I` continues to toggle the persistent stats overlay

The new menu action is an additional control surface only.

## Testing Strategy

Add focused tests in `tests/test_mpv_widget.py` for:

- issuing the mpv command used to toggle the built-in video information overlay
- tolerating shutdown-player conditions consistently with other wrapper helpers

Add focused tests in `tests/test_player_window_ui.py` for:

- context-menu structure including the new top-level `视频信息` action
- invoking the `视频信息` action through the video layer
- logging failures when the video layer rejects the action

## Implementation Order

1. Add a failing mpv wrapper test for the video-info toggle helper.
2. Implement the minimal `MpvWidget` helper required for that test.
3. Add failing player window tests for menu structure and action wiring.
4. Implement the `视频信息` menu action in `PlayerWindow`.
5. Add a failing player window test for error logging.
6. Implement non-fatal error handling for menu-triggered toggle failures.
