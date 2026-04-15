# Subtitle Selection Design

## Summary

Add an in-player subtitle selection control for embedded subtitle tracks so users can choose between automatic selection, disabling subtitles, and specific internal tracks without leaving the playback UI.

The feature is intentionally scoped to the player window and the mpv wrapper. It does not add external subtitle file support or a persistent cross-video subtitle preference.

## Goals

- Add a subtitle selector to the bottom playback controls in the player window.
- Show `自动选择`, `关闭字幕`, and the detected embedded subtitle tracks for the current media item.
- Make `自动选择` prefer Chinese subtitles when available, then fall back to mpv's default behavior.
- Remember the current subtitle choice for the active playback session and try to carry it forward when switching episodes.
- Keep failures non-fatal by logging them and falling back to a safe UI state.

## Non-Goals

- Add support for loading external subtitle files.
- Add audio track selection.
- Persist subtitle preference to `AppConfig` or playback history.
- Redesign the player window layout beyond inserting one compact bottom-bar control.
- Expose every mpv track attribute in the UI.

## Scope

Primary implementation lives in `src/atv_player/player/mpv_widget.py` and `src/atv_player/ui/player_window.py`.

Primary verification lives in `tests/test_mpv_widget.py` and `tests/test_player_window_ui.py`.

No controller or API changes are required for the initial implementation.

## Design

### UI Placement

Add a `QComboBox` to the bottom playback controls near the existing speed selector. The control remains visible in normal windowed playback and follows the same visibility rules as the rest of the bottom bar in fullscreen.

The selector starts with a minimal safe state:

- before media track information is available, show `自动选择`
- if the current media item has no embedded subtitle tracks, keep only `自动选择` and disable the control
- once track information is available, populate `自动选择`, `关闭字幕`, and one item per embedded subtitle track

### Track Labels

Track labels should be readable without exposing raw mpv structures:

- prefer the track title when available
- otherwise use a human-friendly language label when the language code is known
- append short markers for useful flags such as default or forced when present
- if the track has no usable metadata, fall back to `字幕 <n>`

Examples of intended output:

- `中文`
- `中文 (默认)`
- `English (强制)`
- `字幕 3`

### Player Integration

Extend `MpvWidget` with a small subtitle-facing API rather than leaking generic mpv command usage into the UI layer.

The wrapper should provide:

- a way to read embedded subtitle track metadata from the current player
- a way to set subtitle mode to auto, off, or a specific track
- a deterministic helper that chooses the preferred Chinese track for auto mode when one exists

The wrapper remains responsible for translating between Qt-facing values and mpv-specific details such as `sid`, `track-list`, and subtitle-related properties.

### Session-Level Preference

Subtitle state is remembered only within the active `PlayerWindow` session.

The remembered preference is mode-oriented:

- `auto`
- `off`
- `track preference`

Track preference must not store only the current mpv track id because embedded track ids can change between episodes. Instead, keep enough metadata to re-match the user's intent on the next item, in priority order:

1. exact title match when both tracks have titles
2. language match
3. forced/default flag match as a tiebreaker

If no reasonable match exists for the next episode, fall back to `auto`.

### Auto Selection Behavior

`自动选择` means:

1. inspect the embedded subtitle tracks for Chinese-language candidates
2. if a Chinese candidate exists, select the best available Chinese track
3. otherwise let mpv use its normal automatic subtitle selection behavior

The initial implementation should treat common Chinese codes such as `zh`, `chi`, and `zho` as Chinese. When the language code is absent, it should treat track titles containing `中文`, `简中`, `繁中`, `中字`, or `Chinese` as Chinese candidates.

This behavior is limited to embedded subtitle tracks on the current item. It does not change global mpv configuration.

### Episode Changes

Whenever the current playlist item changes, the player should:

1. load the new media item
2. refresh subtitle track metadata for that item
3. reapply the remembered subtitle preference
4. update the combo box selection to reflect the actual applied state

If reapplying a specific prior track fails because the track no longer exists, the window should use `自动选择` rather than silently leaving a stale track-specific selection in the UI.

### Error Handling

Subtitle failures must not interrupt playback.

If reading tracks or applying subtitle state fails:

- keep playback running
- append a concise error line to the playback log
- reset the subtitle selector to a safe state
- prefer `自动选择` as the fallback selection when the actual state cannot be confirmed

The UI should never display track-specific choices from a previous media item after refresh fails for the current item.

## Testing Strategy

Add focused tests in `tests/test_mpv_widget.py` for:

- parsing embedded subtitle tracks from mocked mpv track metadata
- choosing Chinese subtitles for auto mode when available
- falling back to mpv default behavior for auto mode when Chinese subtitles are absent
- disabling subtitles explicitly
- selecting a specific subtitle track
- tolerating mpv exceptions without crashing callers

Add focused tests in `tests/test_player_window_ui.py` for:

- subtitle combo box existence in the bottom control area
- disabled state when no embedded subtitles are available
- track option population after opening a session
- immediate video-layer calls when the user changes subtitle selection
- carrying subtitle preference forward across episode changes in the same session
- falling back to `自动选择` when a prior track preference cannot be matched
- logging and safe UI fallback when subtitle refresh or selection fails

## Implementation Order

1. Add failing mpv wrapper tests for subtitle track parsing and subtitle mode application.
2. Implement the minimal `MpvWidget` subtitle API to satisfy those tests.
3. Add failing player window UI tests for the subtitle combo box and selection behavior.
4. Implement bottom-bar subtitle UI wiring and current-item refresh.
5. Add failing tests for cross-episode preference reuse and fallback behavior.
6. Implement session-level subtitle preference matching and recovery paths.
