# Custom Live Default Poster Design

## Summary

Custom live channels should fall back to a bundled default poster image when they do not provide a channel logo. This fallback applies only to custom live sources, including remote `m3u`, local `m3u`, and manual live entries.

Existing `tvg-logo` and manual `logo_url` values keep priority. Server-provided `/live/{token}` channels remain unchanged.

The default image should reuse the existing bundled asset:

- `src/atv_player/icons/live.png`

## Goals

- Show a default poster for custom live channels that do not have a logo.
- Reuse the same fallback in both the live card grid and the player details poster area.
- Keep existing custom-channel logos unchanged when present.
- Limit the change to custom live sources only.
- Reuse an existing bundled image instead of introducing a new asset or setting.

## Non-Goals

- Adding default posters to backend live channels returned by `/live/{token}`.
- Changing `parse_m3u()` to synthesize poster values.
- Adding a user setting to customize the fallback image.
- Introducing UI-only fallback logic in `PosterGridPage` or `PlayerWindow`.
- Changing poster behavior for browse, search, Douban, Jellyfin, Emby, or other non-live content.

## User Experience

### Custom Live Cards

When a custom live channel does not provide a logo, its poster card should show the bundled `live.png` image instead of an empty poster area.

If a custom live channel already has a logo from `tvg-logo` or manual entry configuration, that logo should continue to display normally.

### Player Poster

When the user opens a custom live channel with no logo, the player details area and the video poster overlay should use the same bundled default image. This keeps the card view and player view visually consistent.

## Architecture

### Fallback Ownership

The fallback should be applied in `CustomLiveService`, not in the parser and not in the UI widgets.

This keeps the behavior scoped to custom live sources and lets the existing poster rendering paths continue to work without special-case UI branching.

### Asset Path

Add a private constant in `src/atv_player/custom_live_service.py` that resolves the absolute path of:

- `src/atv_player/icons/live.png`

The path should be derived from the service module location so it works in the checked-out source tree and in packaged builds that preserve the app resource layout.

### Data Mapping

Whenever `CustomLiveService` builds `VodItem` or `OpenPlayerRequest` objects for custom channels, it should resolve the channel poster with this rule:

1. use the merged channel logo if present
2. otherwise use the bundled default poster path

This applies to:

- ungrouped custom live channels returned by `load_items()`
- grouped custom live channels returned by `load_folder_items()`
- custom live playback requests returned by `build_request()`

## Data Flow

### M3U And Manual Entry Parsing

`parse_m3u()` and manual entry loading should continue to expose raw logo values only.

- channels with `tvg-logo` keep that logo
- manual entries with `logo_url` keep that logo
- channels without logos remain empty at the parsing layer

No fallback logic should be introduced there.

### Custom Live Service Mapping

`CustomLiveService` already maps parsed and merged channel views into `VodItem` and `OpenPlayerRequest`. Add one private helper to centralize poster resolution, for example:

- return `view.logo_url` when non-empty
- otherwise return the bundled default poster path

This keeps the fallback consistent across list and player paths.

### UI Rendering

No UI-specific changes are needed.

`PosterGridPage` already treats local file paths as poster sources. `PlayerWindow` also already loads local poster files directly. Once `CustomLiveService` supplies the bundled image path as `vod_pic`, both surfaces render it through existing logic.

## Error Handling

- If a channel has a real logo, never overwrite it with the default poster.
- If the bundled fallback file is missing unexpectedly, existing local-file poster loading behavior should continue to fail quietly rather than crashing the app.
- The change must not alter how non-custom live items behave when `vod_pic` is empty.

## Testing

Add focused service-layer tests in `tests/test_custom_live_service.py` for:

- remote or local `m3u` custom channels without `tvg-logo` falling back to the bundled poster path in both `load_items()` or `load_folder_items()` and `build_request()`
- manual custom channels without `logo_url` falling back to the bundled poster path
- custom channels with explicit logos keeping their existing logo instead of the fallback

Parser tests should remain unchanged because parser behavior is intentionally not expanding.

No `PosterGridPage` or `PlayerWindow` tests are required for this change because those widgets already support local-file poster rendering and the new behavior is entirely a data-mapping concern.

## Risks And Mitigations

- Risk: applying the fallback in the UI would accidentally affect backend live channels too.
  Mitigation: keep the fallback exclusively inside `CustomLiveService`.
- Risk: duplicated fallback logic across list and player paths could drift.
  Mitigation: introduce a single private poster-resolution helper in `CustomLiveService`.
- Risk: future parser changes could blur the boundary between raw playlist data and product behavior.
  Mitigation: keep parser output raw and add fallback only when constructing app-facing models.
