# Live Source Enable Toggle Design

## Summary

Allow users to disable the default `IPTV` source without deleting it. The change should add an explicit `启用/禁用` action to the `直播源管理` dialog. Disabled sources must remain visible in the manager dialog but must no longer appear in the `网络直播` category list.

## Goals

- Add a `启用/禁用` button to `直播源管理`
- Let users toggle any source, including the default `IPTV` source
- Keep disabled sources listed in the manager dialog
- Hide disabled sources from `网络直播` categories

## Non-Goals

- Converting the enabled column into an inline editable checkbox
- Changing source ordering
- Changing default source creation or deletion behavior

## Design

### Repository And Service Behavior

The repository already stores `enabled` on `live_source`. Reuse that field and add a small helper for toggling the flag on one source record.

`CustomLiveService.load_categories()` should keep its current filter behavior:

- only enabled sources are exposed as `custom:<source_id>` categories

No schema change is required.

### Dialog Behavior

Add a `启用/禁用` button to `LiveSourceManagerDialog`.

Behavior:

- if no row is selected, do nothing
- if the selected source is enabled, clicking the button disables it
- if the selected source is disabled, clicking the button enables it
- after toggling, reload the table so the `启用` column reflects the new state

The button should work for all source types, including the default `IPTV` row.

### Main Window Effect

No new wiring is required beyond existing behavior. The main window already reloads live categories after the dialog closes, so once a source is disabled it should disappear from `网络直播` automatically on the next reload.

## Testing

Update focused dialog tests to cover:

- toggling an enabled source calls the manager with `enabled=False`
- toggling a disabled source calls the manager with `enabled=True`

Update focused service or repository tests only if needed to cover the new toggle helper.

## Risks And Mitigations

- Risk: disabled sources still appear in `网络直播`
  Mitigation: rely on the existing `enabled` filter in `CustomLiveService.load_categories()` and keep the focused test coverage around category loading.
- Risk: the dialog shows stale enabled text after a toggle
  Mitigation: reload the table immediately after toggling.
