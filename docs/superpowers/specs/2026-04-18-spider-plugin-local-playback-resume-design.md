# Spider Plugin Local Playback Resume Design

## Summary

Add local playback-progress persistence for Python spider plugins so a plugin video can reopen at the previous episode and timestamp after app restart.

This feature only applies to spider plugins. It must not call the backend `/history` API and must not change playback-history behavior for live, browse, Emby, Jellyfin, or other existing sources.

## Goals

- Persist spider-plugin playback progress locally in `app.db`.
- Restore the previous episode, timestamp, speed, opening, and ending values for spider-plugin playback.
- Keep existing "restore last player session" behavior, but extend spider-plugin sessions to resume from their local saved progress.
- Avoid any `/history` API reads or writes for spider-plugin playback.
- Keep the change isolated to the spider-plugin flow and the player-controller history integration points.

## Non-Goals

- Replacing backend `/history` usage for non-plugin sources.
- Changing live playback behavior beyond its existing "reopen last session" flow.
- Adding new UI for managing plugin playback history.
- Synchronizing plugin progress across devices or user accounts.
- Generalizing all history storage behind a new abstraction for every source type.

## Current Behavior

The app already persists enough local state to reopen the last player session after restart:

- playback source kind
- playback source key
- playback mode
- source path
- source vod id
- clicked vod id
- paused state

For spider plugins, `MainWindow` can rebuild the request by locating the saved plugin id and calling the plugin controller again. That restores the same plugin detail page and playlist construction path.

However, spider-plugin requests explicitly set `use_local_history=False`, so `PlayerController` skips the existing backend history load/save path. As a result, plugin playback reopens from the beginning instead of resuming from the previous progress.

## Proposed Design

### Persistence Model

Extend `SpiderPluginRepository` with a dedicated playback-progress table:

- table name: `spider_plugin_playback_history`
- one logical record per `plugin_id + vod_id`

Columns:

- `plugin_id INTEGER NOT NULL`
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
- `updated_at INTEGER NOT NULL DEFAULT 0`
- `PRIMARY KEY (plugin_id, vod_id)`

Repository API additions:

- `get_playback_history(plugin_id: int, vod_id: str) -> HistoryRecord | None`
- `save_playback_history(plugin_id: int, vod_id: str, payload: dict[str, object]) -> None`
- deletion of a plugin also deletes that plugin's playback-history rows

The returned `HistoryRecord` should match the existing player resume contract so resume-index logic can be reused without a plugin-specific model.

### Request Wiring

Add optional plugin-local history hooks to `OpenPlayerRequest`:

- `playback_history_loader: Callable[[], HistoryRecord | None] | None`
- `playback_history_saver: Callable[[dict[str, object]], None] | None`

These hooks are only for sources that need custom local progress handling outside the backend history API. Existing request producers can leave them unset.

`SpiderPluginController` should receive repository-backed callbacks when it is created, and `build_request()` should attach them to the returned request:

- loader reads `plugin_id + source_vod_id` from `spider_plugin_playback_history`
- saver writes the current playback payload back to the same logical key
- `use_local_history` remains `False` for spider-plugin requests so plugin playback never uses `/history`

### Player Controller Integration

`PlayerController.create_session()` should load history in this order:

1. `request.playback_history_loader()` when provided
2. existing backend `get_history()` flow when allowed by `use_local_history` or `restore_history`
3. no history

`PlayerController.report_progress()` should save progress in this order:

1. invoke `session.playback_progress_reporter` when present
2. invoke `request`-derived plugin local saver when present
3. continue to existing backend `save_history()` only when `use_local_history=True`

This preserves current behavior for all non-plugin sources while allowing plugin sessions to store progress locally without touching the backend.

### Restore Semantics

Spider-plugin restore continues to use the existing local "restore last player" flow:

1. app restart detects `last_active_window == "player"`
2. `MainWindow` rebuilds the spider-plugin request from saved plugin id and vod id
3. request includes plugin-local history loader
4. `PlayerController.create_session()` consumes that history and computes the resume index and timestamp
5. `PlayerWindow` opens the restored session at that saved point

Resume matching uses the existing `resolve_resume_index()` behavior:

- prefer matching by `episode_url`
- fall back to saved `episode`
- fall back again to `clicked_index`

This avoids inventing spider-specific resume rules and preserves tolerance for small playlist ordering changes.

## Error Handling

- Missing plugin playback history is not an error; playback starts from the default clicked item.
- If saved `episode_url` no longer matches any current playlist item, fall back to saved `episode`.
- If saved `episode` is out of range, fall back to `clicked_index`.
- If local history save fails, do not interrupt playback or surface a blocking error to the user.
- If a plugin is deleted, its associated playback history must be deleted with it.

## Testing

Add or update tests to cover:

- repository create/read/update behavior for spider-plugin playback history
- repository deletion cascade for plugin playback history when a plugin is deleted
- spider-plugin requests attach local playback-history hooks and still keep `use_local_history=False`
- `PlayerController.create_session()` prefers plugin-local history when available
- `PlayerController.report_progress()` writes plugin-local history and does not call API `save_history()` for spider sessions
- plugin restore path rebuilds the correct plugin request and resumes from the saved local plugin progress

## Implementation Notes

- Keep repository ownership in the spider-plugin layer rather than adding a new global storage component.
- Keep `HistoryRecord` as the resume payload format to minimize player-side branching.
- Do not change live-controller behavior; live still reopens the last session without timestamp resume.
- Do not change browse, Emby, Jellyfin, or Telegram history behavior in this feature.
