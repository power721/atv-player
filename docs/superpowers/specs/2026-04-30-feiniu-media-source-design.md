# Feiniu Media Source Design

## Summary

Add a new standalone `Feiniu` media tab that mirrors the existing `Emby` integration while using the backend routes `/feiniu/{token}` and `/feiniu-play/{token}`.

The new source should be independently discoverable through `capabilities["feiniu"]`, independently visible in the main navigation, and independently tracked in local playback history as `source_kind="feiniu"`.

## Goals

- Add a dedicated `Feiniu` tab to the main window.
- Call `/feiniu` and `/feiniu-play` through `ApiClient` with the same request and response contract as `Emby`.
- Reuse the existing poster-grid behavior for category browsing, search, folder navigation, and playback opening.
- Track Feiniu playback history separately from `Emby` and `Jellyfin`.
- Keep the change narrowly scoped to the new source integration.

## Non-Goals

- Refactoring `Emby`, `Jellyfin`, and `Feiniu` into a shared generic controller in this change.
- Changing backend contracts beyond consuming the new `feiniu` capability and route prefixes.
- Altering existing `Emby` or `Jellyfin` behavior.
- Adding new playback UI or new history storage schema.

## Existing Context

- `ApiClient` already exposes mirrored `Emby` and `Jellyfin` endpoints for categories, list loading, detail loading, playback URL resolution, progress reporting, and stop reporting.
- `EmbyController` and `JellyfinController` are nearly identical and already define the expected controller boundary for this type of backend source.
- `App` creates source-specific controllers, wires local playback history hooks, and passes feature flags such as `show_emby_tab` and `show_jellyfin_tab` into `MainWindow`.
- `MainWindow` creates one `PosterGridPage` per enabled remote media source and wires search, folder navigation, playback opening, breadcrumb navigation, and unauthorized logout handling.
- Playback history storage already supports multiple `source_kind` values and the history page formats source labels for known built-in sources.

## Design

### API Client Surface

`ApiClient` should add a Feiniu method set that mirrors the existing Emby method names and semantics:

- `list_feiniu_categories()`
- `list_feiniu_items(category_id, page, filters=None)`
- `search_feiniu_items(keyword, page)`
- `get_feiniu_detail(vod_id)`
- `get_feiniu_playback_source(vod_id)`
- `report_feiniu_playback_progress(vod_id, position_ms)`
- `stop_feiniu_playback(vod_id)`

These methods should only change the route prefix:

- list and detail requests use `/feiniu/{vod_token}`
- playback and progress requests use `/feiniu-play/{vod_token}`

The request parameter names and response parsing rules remain identical to `Emby`.

### Controller Boundary

Add `src/atv_player/controllers/feiniu_controller.py` as a dedicated controller that mirrors `EmbyController`.

Its responsibilities:

- load Feiniu categories
- load paginated category items
- search items
- load folder items inside the same tab
- resolve playlist item detail by `vod_id`
- resolve the real playback URL through `/feiniu-play`
- report playback progress
- report playback stop
- build `OpenPlayerRequest` with Feiniu-specific source metadata

Feiniu playback parsing rules stay aligned with Emby:

- parse `vod_play_url` through the existing playlist parser
- if the playback payload returns `url` as alternating label/URL entries, use the first non-empty playable URL
- if `header` is a JSON string, parse it into a dictionary
- if no playable URL is found, raise the same user-facing error style as Emby

### Source Metadata And History

`FeiniuController.build_request()` should produce:

- `source_kind="feiniu"`
- `source_mode="detail"`
- `source_vod_id=<detail.vod_id>`
- `use_local_history=False`

The request should expose local playback history hooks in the same way as Emby and Jellyfin so Feiniu playback can restore local progress without depending on backend history.

`App` should wire Feiniu local history persistence through the existing playback history repository:

- loader: `get_history("feiniu", vod_id)`
- saver: `save_history("feiniu", vod_id, payload, source_name="飞牛影视")`

No storage schema changes are required. Feiniu is only a new logical source kind.

### Capability Gating

The backend capability response now includes:

- `feiniu: feiniuRepository.count() > 0`

The desktop app should consume that flag and treat Feiniu exactly like existing optional media sources:

- build the controller regardless of visibility, using the same startup pattern as Emby and Jellyfin
- pass `show_feiniu_tab=bool(capabilities.get("feiniu"))` into `MainWindow`
- default missing capability values to `True` in the same defensive way used for existing media-source flags

### Main Window Integration

`MainWindow` should add a new optional `Feiniu` tab backed by `PosterGridPage`.

The page behavior should match `Emby`:

- `click_action="open"`
- `search_enabled=True`
- `folder_navigation_enabled=True`

The window should wire:

- item-open handling to distinguish folder items from playable items
- breadcrumb navigation callbacks
- unauthorized logout handling

The tab title should be `Feiniu`.

### History Page Labeling

History UI should recognize `source_kind="feiniu"` and format it as a built-in source label rather than falling back to a generic label.

The displayed source name should align with the saved metadata, using `飞牛影视` for user-facing history rows.

## Testing Strategy

Add coverage in the same layers already used for Emby and Jellyfin:

### API Client Tests

- capability parsing includes `feiniu`
- category requests hit `/feiniu/{token}`
- list and search requests use the same parameter conventions as Emby
- detail requests hit `/feiniu/{token}?ids=...`
- playback URL resolution hits `/feiniu-play/{token}?t=0&id=...`
- progress and stop reporting hit `/feiniu-play/{token}` with `t=<position>` and `t=-1`

### Controller Tests

Create `tests/test_feiniu_controller.py` mirroring the current Emby controller coverage:

- recommendation category is inserted first
- filters are mapped correctly
- search payload maps to `VodItem`
- folder loading uses the list endpoint with page `1`
- build request parses playlists correctly
- build request exposes local history hooks
- playback loader resolves the first playable stream URL and parses stringified headers
- progress reporting and stop reporting call the correct API methods

### App And Main Window Tests

- `MainWindow` hides the Feiniu tab when `show_feiniu_tab=False`
- Feiniu page search controls are enabled
- clicking a Feiniu playable item opens the player
- clicking a Feiniu folder item loads the folder in the current tab
- Feiniu breadcrumb clicks navigate back within the tab
- unauthorized signals from the Feiniu page trigger logout

### History And Storage Tests

- local playback history repository can round-trip `source_kind="feiniu"`
- history controller delete and clear flows include Feiniu records
- history page formats Feiniu source labels correctly

## Risks And Mitigations

- Risk: copying Emby behavior could introduce inconsistent naming between saved history metadata and UI labels.
  Mitigation: standardize on `source_kind="feiniu"` internally and `飞牛影视` for user-facing names.

- Risk: adding a new optional tab could miss one of the existing signal hookups and produce partial behavior.
  Mitigation: mirror the exact `Emby` integration points in `MainWindow` and cover them with UI tests.

- Risk: capability handling could leave the Feiniu tab permanently hidden on older backends.
  Mitigation: keep the same defensive default behavior already used for existing built-in media capabilities.
