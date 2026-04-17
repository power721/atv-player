# Manual Live Source Logo Design

## Summary

Add an optional `logo_url` field to manual live-source channels so users can supply a poster image URL for channels they create by hand.

When `logo_url` is present, manual live channels should reuse the existing custom-live poster flow and display the image in channel cards and playback metadata. When `logo_url` is empty, the current no-image behavior should remain unchanged.

## Goals

- Let users enter an optional logo URL for manual live-source channels
- Persist manual channel logo URLs in local storage
- Reuse the existing `vod_pic` and `logo_url` flow already used by parsed `m3u` channels
- Keep logo input optional so existing manual entries continue to work unchanged
- Migrate existing databases without breaking or dropping existing manual channel data

## Non-Goals

- Supporting local file selection for logos
- Downloading, caching, or validating logo images during channel editing
- Adding inline image preview to the manual-channel management dialog
- Backfilling logos for existing manual channels
- Changing remote or local `m3u` source parsing behavior

## Storage Design

`LiveSourceEntry` should gain a new field:

- `logo_url: str = ""`

The `live_source_entry` table should gain a new column:

- `logo_url TEXT NOT NULL DEFAULT ''`

`LiveSourceRepository._init_db()` should handle both cases:

- new databases created with the full column present
- existing databases upgraded in place by adding the missing `logo_url` column when absent

This migration should be additive only. Existing manual entries should keep working and should read back with `logo_url=""` until the user edits them.

Repository methods that create or hydrate manual entries should include `logo_url`:

- `add_manual_entry()`
- `get_manual_entry()`
- `list_manual_entries()`
- `update_manual_entry()`

`delete_manual_entry()` and `move_manual_entry()` do not need behavior changes beyond continuing to work with the expanded row shape.

## Service Design

`CustomLiveService` should treat manual-channel logos exactly like parsed `m3u` logos.

When `_load_manual_playlist()` converts `LiveSourceEntry` rows into `ParsedChannel` objects, it should copy `entry.logo_url` into `ParsedChannel.logo_url`.

That keeps the rest of the flow unchanged:

- `load_items()` continues to populate `VodItem.vod_pic` from `channel.logo_url`
- `load_folder_items()` continues to populate `VodItem.vod_pic` from `channel.logo_url`
- `build_request()` continues to populate `OpenPlayerRequest.vod.vod_pic` from `view.logo_url`

No new branching should be added in the page-facing API. Manual channels should simply start participating in the existing image path.

## UI Design

### Channel Form

`ManualLiveSourceDialog`'s add and edit form should gain one additional input:

- `Logo URL`

The field should be optional and should default to the current saved value when editing an existing channel.

Validation remains intentionally narrow:

- `频道名` is required
- `地址` is required
- `Logo URL` is optional

No network validation, URL normalization, or image preview is required in this first pass.

### Channel Table

The manual-channel table should gain one extra column:

- `Logo`

The table should display the raw URL text for visibility and edit confirmation. No thumbnail rendering is required.

## Data Flow

1. User opens `管理频道` for a manual live source
2. User adds or edits a channel
3. Dialog collects `分组`, `频道名`, `地址`, and optional `Logo URL`
4. Dialog calls the existing manager methods with the expanded data
5. Repository persists `logo_url`
6. `CustomLiveService` maps stored `logo_url` into `ParsedChannel.logo_url`
7. Existing custom-live browse and playback flows use that value as `vod_pic`

## Testing

Add focused coverage in:

- `tests/test_live_source_repository.py`
- `tests/test_custom_live_service.py`
- `tests/test_live_source_manager_dialog.py`

Repository coverage should verify:

- old databases without `logo_url` are upgraded in place
- new manual entries persist `logo_url`
- updated manual entries replace `logo_url`
- listed and fetched manual entries expose `logo_url`

Service coverage should verify:

- manual-source channels produce `VodItem.vod_pic` from stored `logo_url`
- manual-source playback requests produce `vod.vod_pic` from stored `logo_url`

UI coverage should verify:

- add-channel flow forwards `logo_url`
- edit-channel flow forwards `logo_url`
- manual-channel table renders the logo column text

## Risks And Mitigations

- Risk: existing user databases fail to load the expanded schema.
  Mitigation: make `_init_db()` perform an additive column check and migration.
- Risk: logo support forks manual-channel behavior from parsed `m3u` behavior.
  Mitigation: map manual entries into `ParsedChannel.logo_url` and reuse the current downstream flow.
- Risk: extra UI complexity grows beyond the narrow request.
  Mitigation: keep the new field as a plain optional text input with no preview or validation side features.
