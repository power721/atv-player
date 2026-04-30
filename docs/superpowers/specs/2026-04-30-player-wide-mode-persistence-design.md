# Player Wide Mode Persistence Design

## Summary

Remember the player window's wide-mode preference across application restarts. If the most recent player window state was wide mode, opening any later player window after a restart should default to wide mode as well.

This is a narrow persistence change. It applies only to the player window's global wide-mode preference and the storage needed to remember it.

## Goals

- Persist whether the player was last left in wide mode.
- Restore that wide-mode preference when a new `PlayerWindow` is created after restarting the app.
- Keep the preference global to the player window, not tied to one playback session.
- Preserve the existing splitter-size persistence behavior for the non-wide layout.

## Non-Goals

- Persist fullscreen mode across restarts.
- Restore different wide-mode values per source, per route, or per media item.
- Infer wide mode from saved splitter geometry instead of storing it explicitly.
- Redesign the player layout or sidebar structure.

## Scope

Primary implementation lives in:

- `src/atv_player/models.py`
- `src/atv_player/storage.py`
- `src/atv_player/ui/player_window.py`

Primary verification lives in:

- `tests/test_player_window_ui.py`
- `tests/test_storage.py`

## Design

### Persistence Model

Add a dedicated boolean config field, `player_wide_mode`, to `AppConfig`.

This field represents one global player preference: whether the player window should start in wide mode when it is next created. It should behave like existing player-level preferences such as volume, mute state, and paused-state restoration.

This value must not be derived from `player_main_splitter_state`. Splitter state answers a different question: what size distribution should the player use when the sidebar is visible. Wide mode is a higher-level view-mode preference and should have its own explicit storage.

### Storage

Add a `player_wide_mode` column to the `app_config` table with a default value of `0`.

Migration behavior for existing databases:

- If the column is missing, add it with the default non-wide value.
- Existing users should therefore continue to start in normal mode until they explicitly enable wide mode.

The repository load/save path should round-trip this value between SQLite and `AppConfig` as a boolean.

### Player Window Initialization

When `PlayerWindow` is constructed with a config object:

- Read `config.player_wide_mode`.
- Apply that value to `wide_button`'s checked state before the initial visibility/layout state is finalized.
- If the stored preference is wide mode, initialize the window so the sidebar is hidden and the main splitter is placed into wide mode immediately.

This restore path should happen during window setup, not only after a playback session is opened, so every player window instance starts with the correct view preference regardless of what content is loaded next.

### Updating The Preference

Whenever wide mode is toggled:

- Update `config.player_wide_mode` to match `wide_button.isChecked()`.
- Persist the config immediately through the existing save callback.

Persisting at toggle time keeps the preference stable even if the user returns to the main window or closes the app without any later geometry-changing event.

### Splitter Behavior

Keep the current responsibility split:

- `player_wide_mode` decides whether the sidebar should be shown at startup.
- `player_main_splitter_state` continues to store the last non-wide splitter layout that should be restored when leaving wide mode.

Entering wide mode must continue to preserve a restoreable non-collapsed sidebar size, so exiting wide mode after a restart or after fullscreen transitions still brings the sidebar back with a usable width.

## Testing Strategy

Follow TDD for implementation.

Add focused storage tests for:

- persisting `player_wide_mode=True` and reading it back as `True`
- loading an older schema without `player_wide_mode` and migrating it with the default `False`

Add focused player-window UI tests for:

- creating a `PlayerWindow` with `config.player_wide_mode=True` starts in wide mode
- toggling wide mode updates `config.player_wide_mode`
- persisting geometry while wide mode is active still preserves the pre-wide splitter state for later restore

Verification should cover both the preference bit and the existing sidebar-width restore behavior so the new persistence does not regress the recent fullscreen/wide-mode fix.

## Implementation Order

1. Add failing storage tests for `player_wide_mode` persistence and migration.
2. Add failing player-window tests for restoring and updating the wide-mode preference.
3. Add the new `AppConfig` field and SQLite column migration.
4. Wire `PlayerWindow` initialization and toggle handling to the persisted preference.
5. Run focused storage and player-window verification, then the broader relevant suites.
