# Player Context Menu Track Controls Design

## Summary

Add a video-surface right-click menu in the player window so users can manage primary subtitles, secondary subtitles, subtitle positions, and audio tracks without leaving playback.

The feature is intentionally scoped to `PlayerWindow` and the mpv wrapper. Existing bottom-bar primary subtitle and audio selectors remain in place. The new right-click menu acts as an additional control surface, not a replacement.

## Goals

- Add a right-click context menu on the video area.
- Let users choose primary subtitle mode from `自动选择`, `关闭字幕`, and embedded subtitle tracks.
- Let users choose a secondary subtitle track independently, defaulting to off.
- Let users adjust primary and secondary subtitle positions independently.
- Let users choose audio track mode from `自动选择` and embedded audio tracks.
- Keep primary subtitle, secondary subtitle, audio, and subtitle-position preferences within the current `PlayerWindow` session and carry them forward when switching episodes in the same session.
- Keep failures non-fatal by logging them and regenerating the menu from actual player state.

## Non-Goals

- Persist subtitle, secondary subtitle, audio, or subtitle-position preferences to `AppConfig`, playback history, or any global mpv configuration.
- Add external subtitle or external audio loading.
- Redesign the bottom playback controls beyond keeping their existing primary subtitle and audio selectors synchronized.
- Add arbitrary numeric input dialogs for subtitle position.
- Support more than one secondary subtitle track at a time.

## Scope

Primary implementation lives in `src/atv_player/player/mpv_widget.py` and `src/atv_player/ui/player_window.py`.

Primary verification lives in `tests/test_mpv_widget.py` and `tests/test_player_window_ui.py`.

No controller or API changes are required.

## Design

### Menu Placement

Attach a Qt context menu to the video surface itself. The menu opens only when the user right-clicks inside the video area.

The menu is rebuilt on each open so it always reflects the current media item's available subtitle tracks, audio tracks, and current session state. The implementation must not cache stale `QAction` state across episode changes.

### Menu Structure

The context menu contains five groups:

- `主字幕`
- `次字幕`
- `主字幕位置`
- `次字幕位置`
- `音轨`

Each group is represented as a submenu.

### Primary Subtitle Controls

The `主字幕` submenu exposes:

- `自动选择`
- `关闭字幕`
- one item per embedded subtitle track

This is the same logical state already controlled by the bottom-bar subtitle combo box. Selecting a primary subtitle in the context menu must update the combo box, and selecting a primary subtitle in the combo box must be reflected the next time the context menu opens.

`自动选择` keeps the existing primary subtitle auto-selection behavior already approved in the player:

1. prefer Simplified Chinese subtitle candidates
2. then prefer other Chinese subtitle candidates
3. then prefer English subtitle candidates
4. otherwise fall back to mpv default subtitle selection

### Secondary Subtitle Controls

The `次字幕` submenu exposes:

- `关闭次字幕`
- one item per embedded subtitle track

Secondary subtitle selection is independent from primary subtitle selection. The application may allow the same embedded track to be chosen in both menus, but it should prefer a simple implementation that does not add extra blocking rules unless mpv behavior requires them.

Secondary subtitle state is session-scoped and defaults to off when a new playback session starts.

The secondary subtitle submenu does not expose an `自动选择` mode. This keeps the model explicit and avoids coupling secondary subtitle behavior to the primary subtitle auto-selection rules.

### Audio Controls

The `音轨` submenu exposes:

- `自动选择`
- one item per embedded audio track

This is the same logical state already controlled by the bottom-bar audio combo box. Selecting an audio track in the context menu must update the combo box, and selecting an audio track in the combo box must be reflected the next time the context menu opens.

`自动选择` for audio keeps the existing player behavior:

1. prefer Chinese or Mandarin audio tracks when available
2. otherwise fall back to mpv default audio selection

### Subtitle Position Controls

Primary and secondary subtitle positions are controlled independently.

Both `主字幕位置` and `次字幕位置` submenus expose:

- fixed presets: `顶部`, `偏上`, `默认`, `偏下`, `底部`
- step actions: `上移 5%`, `下移 5%`, `重置`

Preset values map to stable percentages:

- `顶部` -> `10`
- `偏上` -> `30`
- `默认` -> `50`
- `偏下` -> `70`
- `底部` -> `90`

Step actions move the current position by `5` and clamp to `0..100`.

`重置` returns the position to `50`.

The menu should show the currently active preset as checked when the current position matches one of the fixed preset values exactly. Step actions are never checked.

### Player Integration

Extend `MpvWidget` with narrow mpv-facing helpers rather than setting raw mpv properties directly from `PlayerWindow`.

The wrapper should provide:

- current embedded subtitle tracks
- current embedded audio tracks
- primary subtitle mode application
- secondary subtitle mode application
- primary subtitle position read/write
- secondary subtitle position read/write
- audio mode application

The wrapper remains responsible for translating between Qt-facing concepts and mpv-specific properties such as `sid`, `secondary-sid`, `sub-pos`, `secondary-sub-pos`, `aid`, and `track-list`.

### Session-Level State

Track-selection state is remembered only within the active `PlayerWindow` session.

The window stores:

- primary subtitle preference
- secondary subtitle preference
- audio preference
- primary subtitle position
- secondary subtitle position

Primary subtitle preference remains mode-oriented:

- `auto`
- `off`
- `track preference`

Secondary subtitle preference is mode-oriented:

- `off`
- `track preference`

Audio preference remains mode-oriented:

- `auto`
- `track preference`

Track preference must not rely only on the current mpv track id. To carry choices forward across episodes, the window should reuse the existing matching strategy:

1. exact title match when both tracks have titles
2. language match
3. default and forced flags as a weaker tiebreaker

### Episode Changes

Whenever the playlist advances to another item in the same `PlayerWindow` session, the player should:

1. load the new media item
2. refresh subtitle and audio track metadata for that item
3. reapply the remembered primary subtitle preference
4. reapply the remembered secondary subtitle preference
5. reapply the remembered audio preference
6. reapply remembered primary and secondary subtitle positions

If a prior track-specific preference cannot be matched on the next episode:

- primary subtitle falls back to `自动选择`
- secondary subtitle falls back to `关闭次字幕`
- audio falls back to `自动选择`

If a stored subtitle position cannot be applied, keep playback running, log the failure, and leave the last known session value unchanged so the next successful apply can still use it.

### Error Handling

Track and subtitle-position failures must not interrupt playback.

If reading tracks, applying a track selection, or applying a subtitle position fails:

- keep playback running
- append a concise error line to the playback log
- regenerate menu state from actual player state on the next open
- avoid showing stale checked state in always-visible widgets

The bottom-bar combo boxes should only be updated after successful primary subtitle or audio operations. Secondary subtitle and subtitle-position controls live only in the context menu, so they can recover by rebuilding the menu from current session state each time.

## Testing Strategy

Add focused tests in `tests/test_mpv_widget.py` for:

- applying a secondary subtitle track and disabling it
- reading and writing primary subtitle position
- reading and writing secondary subtitle position
- preserving existing primary subtitle and audio selection behavior
- tolerating mpv exceptions without crashing callers

Add focused tests in `tests/test_player_window_ui.py` for:

- right-click menu structure on the video surface
- primary subtitle actions calling the same video-layer API as the bottom combo box
- secondary subtitle action wiring
- audio action wiring
- primary and secondary subtitle preset selection
- primary and secondary subtitle step actions and clamping
- carrying secondary subtitle, audio, and subtitle-position preferences across episode changes
- falling back safely when a prior secondary subtitle or audio track cannot be matched
- logging and safe recovery when track or subtitle-position application fails
- synchronization between the right-click menu and the existing bottom-bar primary subtitle and audio selectors

## Implementation Order

1. Add failing mpv wrapper tests for secondary subtitle mode and subtitle-position read/write helpers.
2. Implement the minimal `MpvWidget` APIs required for those tests.
3. Add failing player window tests for context-menu structure and action wiring.
4. Implement menu construction and current-item action handling in `PlayerWindow`.
5. Add failing tests for cross-episode reuse and failure recovery.
6. Implement session-level secondary subtitle and subtitle-position state reuse.
