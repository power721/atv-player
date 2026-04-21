# Spider Plugin Local History And Merged History Tab Design

## Goal

Adjust spider plugin playback so its progress is stored only in the local SQLite database, then merge spider-plugin local history with remote history inside the playback-history tab.

The merged history UI must preserve source boundaries:

- Opening a remote history item still uses the regular detail flow.
- Opening a spider-plugin history item must route back through the owning plugin controller.
- Deleting selected records only deletes each selected record from its own source.
- Clearing the current page only deletes the currently displayed records from their own sources.

## Current State

Spider plugin playback already has most of the local-history plumbing:

- `SpiderPluginRepository` has `spider_plugin_playback_history`.
- `SpiderPluginManager` wires repository-backed load/save callbacks into each plugin controller.
- `SpiderPluginController.build_request()` sets `use_local_history=False` and injects local playback-history callbacks.
- `PlayerController.report_progress()` calls the injected history saver before checking `use_local_history`, so plugin playback can save locally without posting to backend `/api/history`.

The missing behavior is history aggregation and source-aware actions:

- `HistoryController` only reads remote `/api/history`.
- `HistoryPage` assumes every record can be opened via the regular browse controller and deleted via backend history ids.
- Plugin-local records are invisible in the playback-history tab.

## Non-Goals

- No backend API changes.
- No attempt to unify remote and local ids into a single persistent id space.
- No global destructive "clear all remote and all local history" operation beyond the currently displayed records.
- No change to plugin-local resume semantics beyond preserving the existing local-only playback progress flow.

## Data Model Changes

Extend `HistoryRecord` with source metadata:

- `source_kind: str = "remote"`
- `source_plugin_id: int = 0`
- `source_plugin_name: str = ""`

Expected values:

- Remote records use `source_kind="remote"`.
- Spider plugin records use `source_kind="spider_plugin"` and fill both plugin fields.

The existing `id` field remains:

- For remote records, it stays the backend history id.
- For spider-plugin records, it is not relied on for deletion and may remain a synthetic or zero value.

The stable delete/open key for plugin history is `(source_plugin_id, key)`.

## Repository Changes

`SpiderPluginRepository` must expose history data for aggregation and deletion:

- `list_playback_histories()` returning all plugin-local playback records joined with plugin metadata needed by the history tab.
- `delete_playback_history(plugin_id: int, vod_id: str)` deleting one plugin-local record.

Returned records should map directly into `HistoryRecord` with:

- `source_kind="spider_plugin"`
- `source_plugin_id=<plugin id>`
- `source_plugin_name=<plugin display_name>`

Sorting is not handled in SQL as a contract requirement; controller-side sorting is acceptable and keeps logic simple.

## Controller Changes

`HistoryController` becomes a merged-history aggregator with two dependencies:

- existing API client for remote history
- spider-plugin repository for local plugin history

Behavior:

1. Load remote page data from `/api/history`.
2. Load all plugin-local playback histories from the repository.
3. Map both into `HistoryRecord`.
4. Sort the merged list by `create_time` descending.
5. Apply in-memory pagination and return `(records, total)`.

Reasoning:

- Remote API pagination cannot be trusted as the final page boundary once local records are merged in.
- The merged tab needs a single global chronology, so pagination must happen after merging.

Mutation behavior:

- `delete_one(record)` deletes according to `record.source_kind`.
- `delete_many(records)` groups selected records by source and deletes each group through its own backend/repository path.
- `clear_page(records)` deletes exactly the currently displayed records, each through its own source.

Unauthorized handling remains remote-only:

- API auth failures still surface as unauthorized.
- Local repository access should not emit unauthorized.

## UI Changes

`HistoryPage` keeps a single merged table but operates on full `HistoryRecord` objects rather than raw ids.

Table changes:

- Add a `来源` column to distinguish remote history from plugin-local history.
- Remote rows display `远程`.
- Spider-plugin rows display plugin name when available, otherwise `插件`.

Interaction changes:

- Double-click on a row emits the full `HistoryRecord` instead of only the key.
- Delete uses the selected `HistoryRecord` objects.
- Clear deletes all currently displayed `HistoryRecord` objects.

The rest of the page behavior stays the same:

- page size selector
- previous/next page
- refresh
- async loading and mutation execution

## Main Window Routing

`MainWindow` must open history rows based on their source:

- `remote` -> existing `browse_controller.build_request_from_detail(record.key)`
- `spider_plugin` -> resolve the plugin controller by `source_plugin_id` from loaded plugin definitions, then call `controller.build_request(record.key)`

If a plugin record references a plugin that is no longer loaded, opening should fail with a user-visible error rather than silently falling back to remote detail logic.

## Playback Progress Requirement

Spider plugin playback progress must remain local-only:

- injected plugin `playback_history_saver` persists into `spider_plugin_playback_history`
- `PlayerController.report_progress()` must call the injected saver before the `use_local_history` guard
- because spider-plugin requests set `use_local_history=False`, no backend `/api/history` save occurs for plugin playback

This behavior must be locked by tests so later refactors do not accidentally re-enable remote history writes for plugin playback.

## Testing Strategy

Add or update tests in these layers:

### History controller

- merged load returns remote and plugin-local rows sorted by time
- delete one dispatches by source
- delete many with mixed sources dispatches correctly
- clear current page deletes exactly the displayed rows

### Repository

- list plugin-local histories returns plugin metadata and mapped record fields
- delete plugin-local history removes the correct `(plugin_id, vod_id)` row

### History page

- `来源` column renders expected labels
- delete passes selected `HistoryRecord` objects to controller
- clear passes current page records to controller
- double-click emits the selected `HistoryRecord`

### Main window

- opening a remote history row still routes through browse detail
- opening a plugin history row routes through the owning plugin controller
- missing plugin id for a plugin history row surfaces an error

### Player controller

- plugin sessions with `playback_history_saver` still save locally while skipping backend `save_history`

## Risks And Mitigations

### Merged pagination may load incomplete remote data

Mitigation:

- For correctness, merged pagination must be derived from the full merged set visible to the client. If needed, repeatedly fetch backend pages until exhaustion during a merged load. The implementation should prefer correctness over minimizing history-tab API calls.

### Plugin display names can change

Mitigation:

- Use current plugin metadata at read time from `spider_plugins`, not a duplicated name stored inside the playback-history row.

### Deleted or disabled plugins leave orphaned history rows

Mitigation:

- Deleted plugins already cascade-remove plugin-local history in repository logic.
- Disabled plugins should still allow history display and deletion only if their metadata remains in `spider_plugins`.
- Opening a history row for a plugin that is not currently loaded must fail cleanly.

## Implementation Notes

- Keep the aggregation logic in `HistoryController`, not `HistoryPage`.
- Keep plugin-history SQL and delete helpers in `SpiderPluginRepository`.
- Keep source-based open routing in `MainWindow`.
- Avoid changing remote API contracts or backend history payload shapes.

## Success Criteria

- Spider plugin playback does not call backend `/api/history` save.
- Spider plugin progress resumes from local SQLite history.
- Playback-history tab shows remote and plugin-local records together in descending time order.
- The tab clearly labels the source of each record.
- Deleting records only deletes them from their own source.
- Clearing the current page removes exactly the currently displayed records from their own sources.
- Double-clicking a merged history row opens the correct playback source.
