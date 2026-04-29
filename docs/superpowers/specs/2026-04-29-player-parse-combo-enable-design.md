# Player Parse Combo Enable Design

## Summary

Adjust the player window so the parse combo box is disabled by default and only becomes enabled for the current play item when spider-plugin `playerContent()` returns `{"parse": 1}`. Items that do not require parse resolution should keep the combo disabled.

## Goals

- Disable the parse combo box when the player opens and no current play item requires parse resolution.
- Enable the parse combo box only for the current play item when the playback loader identifies `parse=1`.
- Disable the combo box again when playback moves to an item that does not require parse resolution.
- Preserve the existing preferred parser selection behavior without changing parse resolution semantics.

## Non-Goals

- Changing how built-in parsers resolve playback URLs.
- Adding new parser configuration UI or parser-management behavior.
- Changing non-spider playback flows.
- Inferring parse requirements from parser availability instead of `playerContent()`.

## Scope

Primary implementation lives in:

- `src/atv_player/models.py`
- `src/atv_player/plugins/controller.py`
- `src/atv_player/ui/player_window.py`

Primary verification lives in:

- `tests/test_spider_plugin_controller.py`
- `tests/test_player_window_ui.py`

## Design

### Playback State

Add a per-item boolean field on `PlayItem` to represent whether the item currently requires parse resolution. The default should be `False` so existing direct-play items stay disabled unless the playback loader explicitly marks them otherwise.

### Spider Playback Loader

Keep using `playerContent()` as the single source of truth:

- when the payload returns `parse=1`, mark the current `PlayItem` as parse-required before resolving the final media URL through the built-in parser service
- when the payload returns `parse=0`, leave the item marked as not parse-required

The playback loader should not mark sibling items proactively. The state is determined lazily for the current item when its playback is being resolved.

### Player Window Behavior

The parse combo box should still be populated with the existing built-in parser entries, but its enabled state should be controlled independently from its contents.

Behavior:

- window initialization: combo remains populated but disabled
- opening a session: combo reflects the current item state, which is normally disabled before deferred playback resolution runs
- loading or replaying an item: after the playback loader updates the current item, refresh the combo enabled state from that item
- changing playlist items or playlist groups: refresh enabled state for the new current item
- direct-play items and already resolved items without parse requirement: combo stays disabled

The stored preferred parser key remains unchanged. Disabling the combo only prevents interaction for items that do not use parser selection.

### Error Handling

If parse-required playback fails, keep the combo enabled for that current item so the user can retry with a different parser choice. Items that never reached `parse=1` should remain disabled even if playback fails for other reasons.

## Testing

Tests should cover:

- spider plugin playback marks items as parse-required only when `playerContent()` returns `parse=1`
- player window parse combo is disabled by default even when parser entries exist
- player window enables the combo for a current item marked parse-required
- player window disables the combo again when switching to an item without parse requirement
- changing parser preference still saves the preferred parser key when the combo is enabled

## Result

After this change, the parse combo box behaves like a contextual control instead of a globally available setting. Users only see it as actionable when the current spider playback item actually depends on parser selection, which matches the `playerContent()` contract.
