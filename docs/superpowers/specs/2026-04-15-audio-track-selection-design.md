# Audio Track Selection Design

## Summary

Add an in-player audio track selection control for embedded audio tracks so users can switch dubbing or language tracks without leaving the playback UI.

The feature is intentionally scoped to the player window and the mpv wrapper. It does not add external audio support or a persistent cross-video audio preference.

## Goals

- Add an audio selector to the bottom playback controls in the player window.
- Show `自动选择` and the detected embedded audio tracks for the current media item.
- Make `自动选择` defer directly to mpv's default audio selection behavior.
- Remember the current audio choice for the active playback session and try to carry it forward when switching episodes in the same session.
- Keep failures non-fatal by logging them and falling back to a safe UI state.

## Non-Goals

- Add support for external audio files or alternate media sources.
- Persist audio preference to `AppConfig` or playback history.
- Add an audio-off option in the selector.
- Redesign the player window layout beyond inserting one compact bottom-bar control.
- Expose every mpv track attribute in the UI.

## Scope

Primary implementation lives in `src/atv_player/player/mpv_widget.py` and `src/atv_player/ui/player_window.py`.

Primary verification lives in `tests/test_mpv_widget.py` and `tests/test_player_window_ui.py`.

No controller or API changes are required for the initial implementation.

## Design

### UI Placement

Add a `QComboBox` to the bottom playback controls near the existing speed and subtitle selectors. The control remains visible in normal windowed playback and follows the same visibility rules as the rest of the bottom bar in fullscreen.

The selector starts with a minimal safe state:

- before media track information is available, show `自动选择`
- if the current media item has no alternate embedded audio tracks, keep only `自动选择` and disable the control
- once track information is available, populate `自动选择` and one item per embedded audio track

The control does not include `关闭音轨`. Playback without an active audio track is outside this feature scope, and keeping the selector to choice-based switching avoids conflating language selection with mute behavior.

### Track Labels

Track labels should be readable without exposing raw mpv structures:

- prefer the track title when available
- otherwise use a human-friendly language label when the language code is known
- append short markers for useful flags such as default or forced when present
- if the track has no usable metadata, fall back to `音轨 <n>`

Examples of intended output:

- `国语`
- `中文 (默认)`
- `English`
- `日语`
- `音轨 3`

### Player Integration

Extend `MpvWidget` with a small audio-facing API rather than leaking generic mpv command usage into the UI layer.

The wrapper should provide:

- a way to read embedded audio track metadata from the current player
- a way to set audio mode to auto or a specific track

The wrapper remains responsible for translating between Qt-facing values and mpv-specific details such as `aid`, `track-list`, and audio-related properties.

### Session-Level Preference

Audio state is remembered only within the active `PlayerWindow` session.

The remembered preference is mode-oriented:

- `auto`
- `track preference`

Track preference must not store only the current mpv track id because embedded track ids can change between episodes. Instead, keep enough metadata to re-match the user's intent on the next item, in priority order:

1. exact title match when both tracks have titles
2. language match
3. default and forced flag match as a tiebreaker

If no reasonable match exists for the next episode, fall back to `auto`.

### Auto Selection Behavior

`自动选择` means:

1. do not inspect or rank embedded audio tracks in application code
2. set the current audio mode back to mpv's normal automatic audio selection behavior

The player should treat mpv as the source of truth for automatic audio selection. The desktop app keeps the `自动选择` label in the UI, but that label is now only a request to hand control back to mpv rather than an application-defined preference strategy.

This behavior is limited to the current item's embedded audio state. It does not change global mpv configuration.

### Episode Changes

Whenever the current playlist item changes, the player should:

1. load the new media item
2. refresh audio track metadata for that item
3. reapply the remembered audio preference
4. update the combo box selection to reflect the actual applied state

If reapplying a specific prior track fails because the track no longer exists, the window should use `自动选择` rather than silently leaving a stale track-specific selection in the UI.

### Error Handling

Audio track failures must not interrupt playback.

If reading tracks or applying audio state fails:

- keep playback running
- append a concise error line to the playback log
- reset the audio selector to a safe state
- prefer `自动选择` as the fallback selection when the actual state cannot be confirmed

The UI should never display track-specific choices from a previous media item after refresh fails for the current item.

## Testing Strategy

Add focused tests in `tests/test_mpv_widget.py` for:

- parsing embedded audio tracks from mocked mpv track metadata
- falling back to mpv default behavior for auto mode without preferring a specific language in application code
- selecting a specific audio track
- tolerating mpv exceptions without crashing callers

Add focused tests in `tests/test_player_window_ui.py` for:

- audio combo box existence in the bottom control area
- disabled state when no alternate embedded audio tracks are available
- track option population after opening a session
- immediate video-layer calls when the user changes audio selection
- carrying audio preference forward across episode changes in the same session
- falling back to `自动选择` when a prior track preference cannot be matched
- logging and safe UI fallback when audio refresh or selection fails

## Implementation Order

1. Add failing mpv wrapper tests for audio track parsing and audio mode application.
2. Implement the minimal `MpvWidget` audio API to satisfy those tests.
3. Add failing player window UI tests for the audio combo box and selection behavior.
4. Implement bottom-bar audio UI wiring and current-item refresh.
5. Add failing tests for cross-episode preference reuse and fallback behavior.
6. Implement session-level audio preference matching and recovery paths.
