# Danmaku Global Preference Design

## Summary

Persist the player's danmaku selection into the existing global app settings table so the last user choice is reused in later playback sessions. The preference should behave like the existing preferred parse setting: a manual user selection updates `AppConfig`, survives restart, and becomes the default behavior for any future play item that has danmaku available.

## Goals

- Save the player's danmaku selection into the global `app_config` row.
- Reapply the saved danmaku preference when a new playback session opens or when danmaku finishes resolving asynchronously.
- Keep the preference semantic and stable even if the combo-box item order changes later.
- Preserve the current default behavior for users with no saved preference: danmaku enabled with `1` line.

## Non-Goals

- Storing danmaku preference per video, per source, or per plugin.
- Adding a dedicated danmaku settings dialog.
- Changing danmaku search, resolution, caching, or subtitle rendering logic.
- Changing the visible danmaku combo-box labels.

## Scope

Primary implementation lives in:

- `src/atv_player/models.py`
- `src/atv_player/storage.py`
- `src/atv_player/ui/player_window.py`

Primary verification lives in:

- `tests/test_storage.py`
- `tests/test_player_window_ui.py`

## Preference Model

Do not store the raw combo-box index.

Persist the danmaku preference as two fields in `AppConfig` and `app_config`:

- `preferred_danmaku_enabled: bool`
- `preferred_danmaku_line_count: int`

Interpretation:

- `preferred_danmaku_enabled = False` means danmaku should stay off by default.
- `preferred_danmaku_enabled = True` means danmaku should auto-load when danmaku XML exists.
- `preferred_danmaku_line_count` should be clamped to the supported range `1..5`.

Default values for fresh or migrated databases should be:

- `preferred_danmaku_enabled = True`
- `preferred_danmaku_line_count = 1`

This preserves today's behavior for existing users who have never changed the setting.

## Storage Design

Extend the single-row `app_config` table with two new columns:

- `preferred_danmaku_enabled INTEGER NOT NULL DEFAULT 1`
- `preferred_danmaku_line_count INTEGER NOT NULL DEFAULT 1`

`SettingsRepository` should:

- add the columns during initial table creation
- migrate older databases with `ALTER TABLE` when the columns are missing
- load the enabled flag as `bool`
- save the enabled flag as `0` or `1`

No new settings table should be added.

## Player Behavior

When danmaku XML is available for the current play item, `PlayerWindow` should read the saved global preference instead of always enabling `1` line.

Behavior:

- saved off preference: keep danmaku disabled and set the combo box to `关闭`
- saved on preference with line count `N`: enable danmaku with `N` lines and set the combo box to the matching item
- invalid saved line counts: clamp into `1..5`

When danmaku is not available:

- keep the current disabled combo-box behavior
- do not overwrite the saved global preference

When danmaku is still pending asynchronously:

- keep the current waiting behavior
- once danmaku XML arrives, apply the saved global preference immediately

## User Interaction

When the user changes the danmaku combo box manually and danmaku XML exists for the current play item:

- selecting `关闭` sets `preferred_danmaku_enabled = False`
- selecting `弹幕` or `1行..5行` sets `preferred_danmaku_enabled = True`
- `弹幕` maps to `1` line
- `2行..5行` map directly to their numeric line count
- save the updated preference through the existing `save_config` callback immediately

If enabling danmaku fails for the current item, keep the current failure handling in the UI. The saved preference should still reflect the user's explicit selection, because the failure is runtime-specific rather than a preference validation problem.

## Testing

Tests should cover:

- settings round-trip for the new danmaku preference fields
- migration of old databases that do not have the new columns
- player window using the saved off preference to keep danmaku disabled
- player window using the saved line-count preference on session open
- player window persisting the user's manual danmaku selection into config
- asynchronous danmaku completion applying the saved global preference instead of hardcoded `1` line

## Result

After this change, danmaku selection becomes a global remembered preference. Users can turn danmaku off once or choose a preferred line count once, and later playback sessions will reuse that choice automatically whenever danmaku is available.
