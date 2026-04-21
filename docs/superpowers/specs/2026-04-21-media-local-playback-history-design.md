# Media Local Playback History Design

## Goal

Move Emby and Jellyfin playback history off the remote `/history` API and into the local SQLite database, using the same merged playback-history tab that already combines remote and spider-plugin local history.

After this change:

- Emby and Jellyfin resume state comes from local SQLite.
- Emby and Jellyfin no longer save or restore through backend `/history`.
- Emby and Jellyfin keep their existing playback-progress and stop-reporting APIs (`/emby-play`, `/jellyfin-play`).
- The playback-history tab merges four sources: remote, spider plugin, Emby, and Jellyfin.

## Current State

Spider plugin local history already exists and is wired through:

- `playback_history_loader`
- `playback_history_saver`
- `use_local_history=False`

This lets plugin playback save locally while skipping backend `/history`.

Emby and Jellyfin do not use that path yet:

- `EmbyController.build_request()` and `JellyfinController.build_request()` do not inject local history callbacks.
- Their requests currently leave `use_local_history=True`, so `PlayerController` falls back to backend `/history`.
- Emby still reports playback progress through `/emby-play`.
- Jellyfin intentionally does not report progress, but it still uses backend `/history` for resume and history.

## Non-Goals

- No changes to Emby `/emby-play` APIs.
- No changes to Jellyfin `/jellyfin-play` stop semantics.
- No backend schema or API changes.
- No attempt to remove remote history support for normal browse/detail playback.

## Architectural Direction

Introduce a general local media playback-history repository rather than extending spider-plugin-specific storage forever.

That repository is responsible for:

- saving local playback history by source kind
- loading one local playback record for resume
- listing all local playback records for merged history
- deleting local playback records by source

It becomes the local-history backend for:

- `spider_plugin`
- `emby`
- `jellyfin`

## Data Model

Reuse `HistoryRecord` as the in-memory shape shown in the playback-history UI.

Required source metadata already exists:

- `source_kind`
- `source_plugin_id`
- `source_plugin_name`

To support Emby/Jellyfin local storage cleanly, add generic source metadata:

- `source_key: str = ""`
- `source_name: str = ""`

Expected usage:

- Remote history:
  - `source_kind="remote"`
  - generic source fields empty
- Spider plugin history:
  - `source_kind="spider_plugin"`
  - `source_plugin_id` and `source_plugin_name` retained for plugin routing
  - generic source fields optional
- Emby history:
  - `source_kind="emby"`
  - `source_name="Emby"`
- Jellyfin history:
  - `source_kind="jellyfin"`
  - `source_name="Jellyfin"`

The main stable open/delete key for local records is:

- `(source_kind, source_key, vod_id)`

For the first implementation:

- `source_key` can be empty for Emby/Jellyfin if there is no current need for per-server disambiguation.
- `vod_id` remains the item key used to rebuild the playback request.

## Storage Design

Add a new local table for media playback history, separate from `spider_plugin_playback_history`.

Recommended table name:

- `media_playback_history`

Columns:

- `source_kind TEXT NOT NULL`
- `source_key TEXT NOT NULL DEFAULT ''`
- `source_name TEXT NOT NULL DEFAULT ''`
- `vod_id TEXT NOT NULL`
- `vod_name TEXT NOT NULL DEFAULT ''`
- `vod_pic TEXT NOT NULL DEFAULT ''`
- `vod_remarks TEXT NOT NULL DEFAULT ''`
- `episode INTEGER NOT NULL DEFAULT 0`
- `episode_url TEXT NOT NULL DEFAULT ''`
- `position INTEGER NOT NULL DEFAULT 0`
- `opening INTEGER NOT NULL DEFAULT 0`
- `ending INTEGER NOT NULL DEFAULT 0`
- `speed REAL NOT NULL DEFAULT 1.0`
- `playlist_index INTEGER NOT NULL DEFAULT 0`
- `updated_at INTEGER NOT NULL DEFAULT 0`

Primary key:

- `(source_kind, source_key, vod_id)`

This replaces spider-plugin-specific local playback storage as the canonical local-history table.

Migration approach:

1. Create `media_playback_history` if missing.
2. If `spider_plugin_playback_history` exists, copy its rows into `media_playback_history` as `source_kind="spider_plugin"`.
3. Use plugin display name as the copied `source_name`.
4. Leave the old table in place for safety during migration, but stop reading/writing it after the new repository path is live.

## Repository Design

Create a new repository, for example:

- `LocalPlaybackHistoryRepository`

Responsibilities:

- `get_history(source_kind: str, vod_id: str, source_key: str = "") -> HistoryRecord | None`
- `save_history(source_kind: str, vod_id: str, payload: dict[str, object], source_key: str = "", source_name: str = "") -> None`
- `list_histories() -> list[HistoryRecord]`
- `delete_history(source_kind: str, vod_id: str, source_key: str = "") -> None`

This repository should not know about controller logic or UI routing.

Spider-plugin-specific concerns stay outside the repository:

- plugin id and plugin controller lookup remain in plugin manager / main window
- plugin row labels can still come from `source_plugin_name`

For spider plugins, the plugin manager can adapt repository calls so the controller still gets simple `Callable[[str], ...]` hooks.

## Controller Changes

### Emby

`EmbyController` should accept optional local-history callbacks:

- loader: `Callable[[str], HistoryRecord | None] | None`
- saver: `Callable[[str, dict[str, object]], None] | None`

`build_request()` should:

- set `use_local_history=False`
- set `restore_history=False`
- inject `playback_history_loader`
- inject `playback_history_saver`

Playback-progress reporting through `/emby-play` stays unchanged.

### Jellyfin

`JellyfinController` should accept the same local-history callbacks.

`build_request()` should:

- set `use_local_history=False`
- set `restore_history=False`
- inject `playback_history_loader`
- inject `playback_history_saver`

Jellyfin still does not report progress remotely through `/jellyfin-play`, but local saver still records playback progress through `PlayerController.report_progress()`.

### PlayerController

No behavior change should be needed beyond preserving the current order:

1. call `playback_history_loader()` first
2. if none, optionally fall back to remote `/history`
3. during progress reporting, call `playback_history_saver(payload)` before checking `use_local_history`
4. skip backend `save_history()` when `use_local_history=False`

That ordering is what makes plugin local history work today and should be reused for Emby/Jellyfin.

## App Wiring

`AppCoordinator` should instantiate the new local playback-history repository and inject it into:

- spider plugin manager
- Emby controller
- Jellyfin controller
- history controller

Spider plugin manager should stop using the old spider-plugin-specific playback-history table after migration and instead write through the new repository with `source_kind="spider_plugin"`.

## Playback History Tab

The playback-history tab keeps the current merged approach.

Displayed source labels:

- `远程` for backend history
- plugin display name for spider plugin history
- `Emby` for Emby local history
- `Jellyfin` for Jellyfin local history

Merged sort order remains:

- `create_time` descending across all sources

Open routing:

- `remote` -> `browse_controller.build_request_from_detail(record.key)`
- `spider_plugin` -> plugin controller `build_request(record.key)`
- `emby` -> `emby_controller.build_request(record.key)`
- `jellyfin` -> `jellyfin_controller.build_request(record.key)`

Delete routing:

- remote records delete via backend `/history`
- local records delete through `LocalPlaybackHistoryRepository.delete_history(...)`

Clear-current-page behavior remains source-aware and record-based.

## Testing Strategy

### Repository tests

- create and read Emby local record
- create and read Jellyfin local record
- list merged local records with correct source labels
- delete local records by `(source_kind, source_key, vod_id)`
- migrate existing `spider_plugin_playback_history` rows into the new table

### Emby/Jellyfin controller tests

- `build_request()` disables remote `/history`
- local playback-history loader/saver callbacks are attached
- existing remote playback hooks remain intact

### PlayerController tests

- Emby session prefers injected local history loader over backend `/history`
- Emby session saves through local saver without backend `save_history`
- Jellyfin session prefers injected local history loader over backend `/history`
- Jellyfin session saves through local saver without backend `save_history`

### History controller tests

- merged load includes remote + spider plugin + Emby + Jellyfin sorted by timestamp
- delete-many dispatches remote ids and local records correctly
- clear-page deletes only the visible records

### Main window tests

- opening Emby history row routes to `emby_controller.build_request`
- opening Jellyfin history row routes to `jellyfin_controller.build_request`

## Risks And Mitigations

### Risk: duplicated storage logic between old and new tables

Mitigation:

- centralize all future local playback reads/writes in the new repository
- treat old spider plugin table as migration input only

### Risk: source identity for Emby/Jellyfin may need per-server separation later

Mitigation:

- keep `source_key` in schema now even if empty for the first version
- this avoids a later schema redesign

### Risk: playback-history tab routing becomes more conditional

Mitigation:

- keep all source dispatch inside `MainWindow.open_history_detail()` and `HistoryController`, not spread across the UI

## Success Criteria

- Emby no longer reads/writes backend `/history`
- Jellyfin no longer reads/writes backend `/history`
- Emby resume comes from local SQLite
- Jellyfin resume comes from local SQLite
- Spider plugin local history still works after migration
- playback-history tab shows remote, spider plugin, Emby, and Jellyfin records together
- deleting and clearing records only touches the selected records' own sources
