# Player Context Menu Subtitle Size Design

## Summary

Add primary and secondary subtitle size controls to the existing video-surface right-click menu in the player window.

The feature is intentionally scoped to `PlayerWindow` and the mpv wrapper. It extends the existing context-menu subtitle controls without changing the bottom playback bar.

## Goals

- Add `主字幕大小` and `次字幕大小` submenus to the existing video context menu.
- Let users adjust primary and secondary subtitle sizes independently.
- Support both fixed presets and step-based size adjustments.
- Keep subtitle-size settings within the current `PlayerWindow` session and carry them forward when switching episodes in the same session.
- Keep failures non-fatal by logging them and disabling unsupported controls.

## Non-Goals

- Persist subtitle-size settings to `AppConfig`, playback history, or global mpv configuration.
- Add subtitle-size controls to the bottom playback bar.
- Redesign the existing context-menu layout beyond inserting two new submenus.
- Add arbitrary numeric input dialogs or slider popups.
- Couple primary and secondary subtitle sizes together.

## Scope

Primary implementation lives in `src/atv_player/player/mpv_widget.py` and `src/atv_player/ui/player_window.py`.

Primary verification lives in `tests/test_mpv_widget.py` and `tests/test_player_window_ui.py`.

No controller or API changes are required.

## Design

### Menu Placement

Extend the existing video-surface right-click menu. The menu should now contain:

- `主字幕`
- `次字幕`
- `主字幕位置`
- `次字幕位置`
- `主字幕大小`
- `次字幕大小`
- `音轨`

The new size submenus follow the same on-demand rebuild behavior as the existing context menu. They must reflect current session state each time the menu opens.

### Size Controls

Primary and secondary subtitle sizes are controlled independently.

Both `主字幕大小` and `次字幕大小` submenus expose:

- fixed presets: `很小`, `小`, `默认`, `大`, `很大`
- step actions: `缩小 5%`, `放大 5%`, `重置`

Preset values map to stable percentages:

- `很小` -> `70`
- `小` -> `85`
- `默认` -> `100`
- `大` -> `115`
- `很大` -> `130`

Step actions move the current size by `5` and clamp to a safe range of `50..200`.

`重置` returns the size to `100`.

The menu should show the active preset as checked when the current session value matches one of the fixed preset values exactly. Step actions are never checked.

### Player Integration

Extend `MpvWidget` with narrow mpv-facing helpers rather than letting `PlayerWindow` set raw subtitle-size properties directly.

The wrapper should provide:

- primary subtitle size read/write
- secondary subtitle size read/write
- primary subtitle size support detection
- secondary subtitle size support detection

The wrapper remains responsible for translating between Qt-facing percentage values and mpv-specific properties.

### Support Detection And Degradation

Primary and secondary subtitle size support must be detected independently.

If the underlying mpv build or libmpv surface does not support a subtitle-size property:

- disable the corresponding size submenu
- skip automatic reapply during refresh and episode changes
- do not emit repeated error logs for unsupported capability during normal refresh

This mirrors the existing degradation pattern already used for unsupported secondary subtitle position.

### Session-Level State

Size state is remembered only within the active `PlayerWindow` session.

The window stores:

- primary subtitle size
- secondary subtitle size
- primary subtitle size support flag
- secondary subtitle size support flag

Suggested field names:

- `self._main_subtitle_scale`
- `self._secondary_subtitle_scale`
- `self._main_subtitle_scale_supported`
- `self._secondary_subtitle_scale_supported`

Default values start at `100`.

### Episode Changes

Whenever the playlist advances to another item in the same `PlayerWindow` session, the player should:

1. load the new media item
2. refresh subtitle and audio track metadata
3. reapply primary subtitle preference
4. reapply secondary subtitle preference
5. reapply audio preference
6. reapply primary and secondary subtitle positions
7. reapply primary and secondary subtitle sizes when supported

If a subtitle-size property is unsupported, leave the session value unchanged but skip application silently.

If a supported subtitle-size write fails, keep playback running and log a concise error line.

### Error Handling

Subtitle-size failures must not interrupt playback.

If reading or writing subtitle size fails:

- keep playback running
- append a concise error line to the playback log
- regenerate menu state from current session state on next open
- avoid leaving a permanently enabled menu for a capability that was detected as unsupported

Example log lines:

- `主字幕大小设置失败: ...`
- `次字幕大小设置失败: ...`

## Testing Strategy

Add focused tests in `tests/test_mpv_widget.py` for:

- reading and writing primary subtitle size
- reading and writing secondary subtitle size
- reporting unsupported primary subtitle size
- reporting unsupported secondary subtitle size

Add focused tests in `tests/test_player_window_ui.py` for:

- context-menu structure including `主字幕大小` and `次字幕大小`
- primary size preset selection
- secondary size preset selection
- size step actions and clamping
- carrying primary and secondary subtitle size across episode changes
- disabling unsupported primary or secondary size menus
- logging supported-property write failures without interrupting playback

## Implementation Order

1. Add failing mpv wrapper tests for subtitle-size read/write and support detection.
2. Implement the minimal `MpvWidget` subtitle-size APIs required for those tests.
3. Add failing player window tests for size submenu structure and action wiring.
4. Implement size menu construction and action handling in `PlayerWindow`.
5. Add failing tests for session reuse, capability degradation, and error recovery.
6. Implement size-state reuse and unsupported-capability handling.
