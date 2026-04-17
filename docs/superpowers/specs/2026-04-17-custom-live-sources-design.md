# Custom Live Sources Design

## Summary

Add user-managed custom live sources to the existing `网络直播` tab. Users should be able to manage three kinds of sources:

- remote `m3u` playlists
- local `m3u` files
- manual channel lists made of `group name + channel name + stream URL`

The main window header should gain a new `直播源管理` button immediately after `插件管理`. That button opens a dedicated management dialog. Enabled custom live sources should appear as first-class categories in the existing `网络直播` left-side category list, ahead of server-provided live categories.

Custom sources should reuse the current poster-grid live browsing flow:

- source selection in the left category list
- optional group browsing through folder cards
- channel playback through direct stream URLs

Remote source loading should prefer cached content for immediate display and expose a manual refresh action. The app should ship with one removable example remote source preloaded from:

- `https://raw.githubusercontent.com/Rivens7/Livelist/refs/heads/main/IPTV.m3u`

## Goals

- Add a `直播源管理` button after `插件管理` in the main window header.
- Support three custom source types: remote `m3u`, local `m3u`, and manual channels.
- Show enabled custom sources as categories inside `网络直播`.
- Reuse the existing `PosterGridPage` folder-navigation and playback flow.
- Cache remote playlist text locally and prefer cached content on initial browse.
- Allow manual refresh for remote sources and manual reload for local-file sources.
- Preload one removable example remote source on first database initialization.

## Non-Goals

- Changing the existing backend `/live/{token}` API contract.
- Creating a new dedicated browsing tab for custom live sources.
- Adding background auto-refresh or scheduled refresh for remote sources.
- Supporting every possible extended `m3u` attribute beyond the common fields needed here.
- Refactoring unrelated playback or poster-grid behavior outside what this feature needs.

## User Experience

### Entry Point

The main window header should become:

- `插件管理`
- `直播源管理`
- `退出登录`

Clicking `直播源管理` opens `LiveSourceManagerDialog`.

### Network Live Categories

The `网络直播` tab should continue to use the existing poster-grid page. Its left category list should merge two category sources in this order:

1. enabled custom live sources from local storage
2. existing categories from the backend `/live/{token}` API

Each enabled custom source should appear as one category item using its display name.

### Browsing Behavior

When the user opens a custom source category:

- if cached parsed content exists, show it immediately
- if the source is remote and no cache exists, load it on demand
- if the source contains groups, show group cards first
- if the source has no groups, show channel cards directly

Group cards should use the existing folder flow:

- `vod_tag="folder"` for groups
- breadcrumb navigation for returning to the source root

Channel cards should use:

- `vod_tag="file"`
- direct player open on click

Playback for custom live channels should behave like existing live playback and should not use local playback history.

### Management Dialog

`LiveSourceManagerDialog` should provide source-level actions:

- add remote source
- add local source
- add manual source
- edit display name
- enable or disable
- move up
- move down
- refresh selected source
- delete selected source

When a manual source is selected, the dialog should also expose channel management for that source through a child editor dialog.

### Manual Source Editing

The first version should keep manual channel editing intentionally narrow. Each manual channel record should contain:

- group name
- channel name
- stream URL

Users should be able to:

- add a channel
- edit a channel
- delete a channel
- reorder channels

## Architecture

### Storage Layer

Add a dedicated local repository for custom live sources instead of storing them inside `AppConfig`.

Create a `LiveSourceRepository` backed by SQLite with two tables.

`live_source` stores source definitions and cache metadata:

- `id`
- `source_type` (`remote`, `local`, `manual`)
- `display_name`
- `source_value`
- `enabled`
- `sort_order`
- `is_default`
- `last_refreshed_at`
- `last_error`
- `cache_text`

`live_source_entry` stores manual channels:

- `id`
- `source_id`
- `group_name`
- `channel_name`
- `stream_url`
- `sort_order`

Repository responsibilities:

- initialize tables
- insert the default example source once on first setup
- list sources in sort order
- add, rename, enable/disable, move, refresh metadata, and delete sources
- manage manual channel rows
- persist remote or local parsed cache text

### Parsing Layer

Add an `M3uParser` responsible only for converting `m3u` text into a normalized in-memory structure.

The parser should support the common shape found in the provided reference playlist:

- `#EXTM3U`
- `#EXTINF`
- `group-title`
- channel display name after the comma
- the following non-comment line as the stream URL

The parser may read optional metadata such as `tvg-logo`, but the feature must not depend on it. Missing logos should simply fall back to the existing poster-card rendering without a poster.

The normalized result should represent:

- source metadata
- groups
- channels

This keeps parsing isolated from UI and storage concerns.

### Custom Source Service

Add a `CustomLiveService` that sits between repository and controller. It should own:

- loading source definitions
- resolving source content from cache, local file, remote URL, or manual entries
- refreshing remote sources
- reloading local-file sources
- translating parsed results into page-facing `VodItem` and `OpenPlayerRequest` data

This service should preserve old cached text when refresh or reload fails.

### Live Controller Integration

Do not push custom-source semantics into `ApiClient`.

The recommended integration is to keep remote server live requests unchanged and extend the live browsing controller path so it can unify:

- server categories and items from `/live/{token}`
- custom source categories and items from local storage

This can be implemented either by enhancing `LiveController` directly or by introducing a thin aggregate controller above it. The important requirement is that the page-facing contract remains unchanged:

- `load_categories()`
- `load_items(category_id, page)`
- `load_folder_items(vod_id)`
- `build_request(vod_id)`

## Identifiers and Navigation

Custom source identifiers must be distinct from backend live identifiers.

Use stable prefixes:

- category id: `custom:<source_id>`
- folder item id: `custom-folder:<source_id>:<group_key>`
- channel item id: `custom-channel:<source_id>:<channel_id>`

This preserves compatibility with the existing poster-grid event flow and avoids collisions with backend values such as `bili` or `bili$1785607569`.

Browsing rules:

- selecting `custom:<source_id>` loads the source root
- clicking `custom-folder:...` loads the channels for that group
- clicking `custom-channel:...` opens playback directly

Breadcrumb navigation should continue to use the current folder-navigation support already present in `PosterGridPage`.

## Refresh and Cache Behavior

### Remote Sources

Remote sources should use the following behavior:

- when browsing a source, prefer cached content if available
- if no cache exists, fetch on demand
- provide a manual refresh action in the management dialog
- when refresh succeeds, replace cache text and clear the last error
- when refresh fails, preserve old cache text and update the last error

### Local File Sources

Local file sources should:

- read the configured file path on demand
- allow manual reload from the management dialog
- preserve any existing cached content if the file later becomes unreadable
- record the error state without breaking already-cached browsing when possible

### Manual Sources

Manual sources do not need text caching. Their content comes directly from `live_source_entry`.

## Playback Behavior

Custom live channels should build `OpenPlayerRequest` objects directly from stored channel metadata:

- `vod_name` should use the channel display name
- playlist should contain one `PlayItem` using the channel stream URL
- `source_kind` should remain `live`
- `source_mode` should identify custom live playback
- `use_local_history` must be `False`

No extra detail round trip is needed for custom channels because the stream URL is already known locally.

## Default Example Source

The repository should create one default, removable example source during first initialization only:

- display name: `示例直播源`
- source type: `remote`
- source value: `https://raw.githubusercontent.com/Rivens7/Livelist/refs/heads/main/IPTV.m3u`
- enabled: `True`
- is_default: `True`

If the user deletes it, the app must not recreate it later.

## Error Handling

- If a remote source fails to load and has no cache, browsing should show a user-facing error state instead of empty fake content.
- If a remote refresh fails but cached content exists, browsing should continue to work from cache.
- If a local file path is missing or unreadable, the dialog should show the latest error and browsing should use cached content if available.
- If parsing produces no playable channels, the source root should show `暂无内容`.
- Invalid manual URLs should be rejected at edit time when obviously empty, but the first version does not need deep network validation.

## Scope of UI Changes

Primary implementation is expected in:

- `src/atv_player/storage.py`
- `src/atv_player/models.py`
- `src/atv_player/controllers/live_controller.py`
- `src/atv_player/app.py`
- `src/atv_player/ui/main_window.py`
- `src/atv_player/ui/live_source_manager_dialog.py`

Supporting new modules are expected for:

- live source repository
- `m3u` parsing
- custom live service
- manual channel editor dialog

## Testing Strategy

Add focused repository tests for:

- table initialization
- default source insertion
- CRUD operations for sources
- move and ordering behavior
- manual channel CRUD and ordering
- cache persistence and error updates

Add parser tests for:

- standard `#EXTINF` plus URL pairs
- grouped channels via `group-title`
- ungrouped channels
- optional attributes that should be ignored safely

Add controller or service tests for:

- custom sources appearing ahead of backend live categories
- source root mapping into folder and channel `VodItem` values
- group folder navigation
- channel playback request construction
- remote refresh failure preserving old cache
- playback disabling local history

Add UI and app tests for:

- main window header includes `直播源管理` after `插件管理`
- management dialog actions invoke the manager or repository layer correctly
- enabled custom sources appear in the `网络直播` category list
- clicking a custom group opens nested items
- clicking a custom channel opens the player

## Implementation Notes

- Keep `PosterGridPage` unchanged unless a small hook is needed for refresh behavior already exposed elsewhere.
- Prefer matching the existing plugin-management dialog style for the new source-management dialog.
- Keep source ordering explicit through `sort_order`; do not infer order from timestamps or ids.
- Do not add background threads that continuously refresh remote playlists in this first version.

## Risks and Mitigations

- Risk: custom and backend live identifiers collide.
  Mitigation: reserve explicit `custom:` prefixed ids for all custom nodes.
- Risk: remote playlists become temporarily unavailable.
  Mitigation: browse from cache first and preserve old cache on refresh failure.
- Risk: adding all source logic into `LiveController` makes it too broad.
  Mitigation: isolate storage and parsing in dedicated units and keep controller focused on page-facing contracts.
- Risk: manual source editing becomes too large in the first pass.
  Mitigation: keep the schema and dialog limited to basic grouped channel rows only.

## Acceptance Criteria

- The main window shows a `直播源管理` button after `插件管理`.
- Users can add remote `m3u` sources, local `m3u` sources, and manual channel sources.
- One removable example remote source is preloaded on first setup.
- Enabled custom sources appear as categories in `网络直播`.
- Custom sources support source root browsing, grouped browsing, and direct channel playback.
- Remote sources show cached content first and support manual refresh.
- Failed refreshes preserve previous cache when available.
- Custom live playback does not use local playback history.
