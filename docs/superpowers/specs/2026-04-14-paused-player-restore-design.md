# Paused Player Restore Design

## Summary

Preserve the player's paused state across app restarts.

If the user pauses playback, exits or hides the player, and later restores the last player session, the app should reopen the same item at the same resume position without starting playback automatically.

This behavior is local to the desktop app. It does not change backend history payloads.

## Goals

- Restore the last player session at the same item and resume position.
- Keep the player paused after app restart when the prior session was paused.
- Preserve current behavior for normal "open player" flows that start new playback.
- Keep the change local to app config, player restore flow, and UI tests.

## Non-Goals

- Change backend history schema or API payloads.
- Add a new visible UI control for restore mode.
- Force all restored sessions to start paused regardless of prior state.
- Change playback progress reporting semantics.

## Current Behavior

The app already restores the last player session when `last_active_window` is `"player"`.

The restore flow currently preserves:

- playback source selection
- episode index selection
- resume position
- playback speed

It does not preserve whether the player was paused. `PlayerWindow.open_session()` resets playback state to playing, and `PlayerWindow._load_current_item()` loads media without passing a paused flag to the video layer.

## Proposed Design

### Persisted State

Add `last_player_paused: bool` to `AppConfig`.

Persist the value in `SettingsRepository`:

- create the column for new databases
- add a migration for existing databases
- include the field in load/save round trips

This flag represents the local UI playback state for the most recent player session. It is independent of backend history.

### Restore Semantics

Only the "restore last player" path should consume persisted paused state.

When the app restores the last player session:

- restore the previously selected item
- restore resume position
- restore playback speed
- restore paused state from `config.last_player_paused`

When the user opens a new player session from browse/history navigation, playback should continue to start normally unless that flow explicitly opts into paused restore in the future.

### Player Window Flow

Extend `PlayerWindow.open_session()` so callers can specify whether the session should open paused.

The paused state should flow through:

- `open_session(..., start_paused=...)`
- `_load_current_item(..., pause=...)`
- `video.load(url, pause=..., start_seconds=...)`

When a session opens paused:

- `self.is_playing` must be `False`
- the play button icon must show the play icon
- the media must be loaded with `pause=True`

When a session opens playing:

- preserve current behavior

### State Updates

Update `config.last_player_paused` whenever the local player state becomes authoritative:

- after toggling playback
- before returning to main from the player
- before quitting the app from the player
- when opening a new non-restore session, reset it to `False`

Returning to main already pauses playback. That path should also persist `last_player_paused = True` so the later restore remains paused.

Quitting from the player should persist the current pause/play state without forcing it to `True`.

## Data Flow

1. User pauses playback.
2. `PlayerWindow.toggle_playback()` updates `is_playing` and persists `last_player_paused = True`.
3. User quits the app or returns to main.
4. Existing config fields continue to identify the last playable session; `last_player_paused` records the playback state.
5. On next launch, `AppCoordinator._show_main()` calls `MainWindow.restore_last_player()` when `last_active_window == "player"`.
6. `MainWindow.restore_last_player()` rebuilds the request and opens the player in restore mode.
7. `PlayerWindow.open_session(..., start_paused=True)` reloads the item at the saved position and remains paused.

## Error Handling

- If player restore fails for existing reasons, continue falling back to the main window and reset `last_active_window` to `"main"`.
- If the paused-state column is absent in an older database, migration should create it with a default of `0` so older installs continue restoring as playing until a paused state is saved.
- No special recovery path is needed for invalid paused-state values beyond normal SQLite boolean coercion to Python truthiness.

## Testing

Add or update tests to cover:

- `SettingsRepository` round-trips `last_player_paused`
- repository migration/load behavior for older configs defaults the field to `False`
- `PlayerWindow.open_session(start_paused=True)` passes `pause=True` into the video layer
- opening paused updates `is_playing` and the play button icon consistently
- app/player restore path opens the restored session with paused state from config
- normal `open_player()` flow still opens sessions as playing

## Implementation Notes

- Keep the change localized to `AppConfig`, `SettingsRepository`, `MainWindow`, `PlayerWindow`, and focused tests.
- Avoid inferring paused restore from other fields such as `last_active_window`; the paused flag should be explicit.
- Do not add backend API changes for this feature.
