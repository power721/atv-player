# Live EPG Multiple URLs Design

## Summary

Extend the existing global custom-live `EPG URL` setting to support multiple XMLTV URLs instead of a single URL.

The setting remains global for all custom live sources, but the stored value may now contain multiple URLs, one per line. Refresh should fetch every configured URL in user-defined order, merge the successful XMLTV payloads into one effective guide dataset, and continue using the merged result for channel matching and current/upcoming programme lookup.

When duplicate programme windows exist across multiple EPG sources, earlier URLs win over later URLs.

## Goals

- Let users configure multiple global EPG URLs
- Keep the configuration flow inside `直播源管理`
- Preserve backward compatibility with existing single-URL data
- Merge multiple XMLTV payloads into one effective cached guide
- Keep refresh resilient when some URLs fail but others succeed
- Keep current custom live playback EPG behavior unchanged apart from broader source coverage

## Non-Goals

- Per-live-source EPG URLs
- A separate table for one-row-per-URL management
- Per-URL enable or disable toggles
- Drag-and-drop URL sorting UI
- Scheduled periodic refresh beyond the existing manual and startup refresh entry points
- Showing per-URL status rows in the dialog

## User Experience

[`LiveSourceManagerDialog`](/home/harold/workspace/atv-player/src/atv_player/ui/live_source_manager_dialog.py) should replace the single-line EPG input with a multi-line text input.

- Label: `EPG URL（每行一个）`
- Each non-empty line is treated as one URL
- `保存` stores the full multi-line value after trimming whitespace and removing empty lines
- `立即更新` still runs in the background and does not block the dialog

Compatibility behavior:

- Existing databases that already contain one URL keep working without migration prompts
- A previously saved single URL simply appears as one line in the new multi-line input

Status behavior:

- Full success: show the latest refresh marker with no error text
- Partial success: still count the refresh as successful, but the status text should make it clear that some URLs failed
- Full failure: keep the old cache and show the aggregated failure summary

This change should not alter player-side rendering semantics. The player still shows EPG rows only when a matching schedule exists.

## Storage

Keep the existing single-row `live_epg_config` table and reuse the existing `epg_url TEXT` column.

The column meaning changes from:

- one URL string

to:

- a newline-delimited URL list stored as one text blob

This keeps persistence simple and avoids a schema migration to a child table.

Repository responsibilities remain narrow:

- load the current config row
- save the raw multi-line URL text
- save cache text, refresh stamp, and error text after refresh attempts

The repository should not own URL parsing or deduplication logic.

## Architecture

### `LiveEpgConfig`

Keep the existing [`LiveEpgConfig`](/home/harold/workspace/atv-player/src/atv_player/models.py) model shape for compatibility.

- `epg_url` remains a `str`
- its content is now interpreted as a multi-line URL configuration

This avoids widening the storage model into a list-shaped API at this layer.

### `LiveEpgRepository`

[`LiveEpgRepository`](/home/harold/workspace/atv-player/src/atv_player/live_epg_repository.py) remains the persistence boundary.

It should continue to:

- create and seed the single config row
- round-trip the raw `epg_url` text without clearing the cache
- persist the latest cache text, refresh marker, and error state

### `LiveEpgService`

[`LiveEpgService`](/home/harold/workspace/atv-player/src/atv_player/live_epg_service.py) owns the new multi-URL behavior.

Responsibilities:

- parse the stored multi-line text into an ordered URL list
- trim whitespace, remove empty lines, and ignore duplicate URLs after first occurrence
- fetch every configured URL in order
- support both plain XMLTV and gzip-compressed XMLTV payloads
- parse each successful payload into channel and programme structures
- merge all successful payloads into one effective dataset
- expose current and upcoming schedule lookup against the merged cache

### `AppCoordinator`

[`AppCoordinator`](/home/harold/workspace/atv-player/src/atv_player/app.py) should keep the same startup behavior.

- startup refresh still runs in the background
- startup remains non-blocking even if several EPG URLs are configured
- no new startup scheduling rules are introduced

## Refresh And Merge Strategy

Refresh flow:

1. Load the stored config row
2. Parse `epg_url` into an ordered URL list
3. If the list is empty, skip refresh
4. For each URL in order:
   - download bytes
   - detect gzip by payload bytes, not filename alone
   - decode to UTF-8 text
   - parse XMLTV into channels and programmes
5. Merge all successful payloads into one dataset
6. Persist the merged cache when at least one URL succeeded
7. Preserve the previous cache when every URL failed

Error handling during refresh:

- A failure for one URL must not stop the remaining URLs from being processed
- Per-URL failures should be accumulated into an aggregated message
- If at least one URL succeeds, the refresh is considered successful overall
- On partial success, `last_error` may still contain a compact summary such as which URLs failed
- On full success with no failures, `last_error` should be cleared
- On full failure, `last_refreshed_at` and `cache_text` should remain unchanged

## Merge Rules

Merge in memory before serializing the effective cache for persistence or reuse.

Channel merge:

- Key channels by XMLTV `channel id`
- For duplicate channel ids, union the `display-name` values
- Preserve first-seen order for display names

Programme merge:

- Use `(channel, start, stop)` as the duplicate key
- If multiple sources provide the same key, keep the first one encountered based on URL order
- Later URLs may still contribute additional programme rows that do not collide on that key

Post-merge ordering:

- sort programmes by `(channel, start)`
- preserve deterministic output so cache reuse and tests stay stable

## Cache Representation

The rest of the app should continue to treat the EPG cache as one logical guide snapshot.

Implementation can choose either of these internal forms:

- store one merged XMLTV text blob in `cache_text`
- store another deterministic serialized representation that [`LiveEpgService`](/home/harold/workspace/atv-player/src/atv_player/live_epg_service.py) can parse consistently

Recommendation:

- keep `cache_text` as XMLTV text and serialize the merged dataset back into one deterministic XMLTV document

This keeps the repository contract unchanged and preserves the current "parse from cached text" flow.

## Lookup Behavior

Current lookup behavior should stay functionally the same after refresh.

- Channel matching still runs against one effective cached dataset
- Exact match still takes priority
- Existing normalization and alias handling remain in place
- `current` and `upcoming` programme extraction rules remain unchanged

The multi-URL change should expand guide coverage, not redefine matching semantics.

## UI Details

[`LiveSourceManagerDialog`](/home/harold/workspace/atv-player/src/atv_player/ui/live_source_manager_dialog.py) should switch from `QLineEdit` to a multi-line text widget such as `QPlainTextEdit`.

Expected behavior:

- loading config fills the widget with the raw saved multi-line text
- saving reads the full text and normalizes it into newline-delimited URLs
- background refresh completion reloads the saved config and status label

The dialog should remain compact enough for the existing source table layout.

## Failure Handling

- Empty config: skip refresh and treat EPG as disabled
- Duplicate URLs: ignore repeated entries after the first
- Partial fetch failure with at least one success: write merged cache and expose a partial-failure status
- Full fetch failure: keep the previous cache and expose the aggregated error
- Invalid XMLTV in one URL: treat it as that URL failing, continue with the rest
- Missing cache plus full failure: playback remains available without EPG rows

No modal dialogs are required for refresh failures.

## Testing

Add or update tests in the existing EPG coverage files.

Repository:

- multi-line `epg_url` text round-trips unchanged
- saving URLs still does not clear cached guide text
- full-failure refresh result preserves previous cache text and refresh marker

Service:

- parsing multi-line config yields ordered, deduplicated URLs
- multiple successful URLs merge channels and programmes
- conflicting programme rows use earlier URL priority
- one failing URL does not block other successful URLs
- full failure preserves the previous cache
- gzip payloads still decompress correctly in multi-URL refresh
- schedule lookup still works from merged cached data

UI:

- dialog renders a multi-line EPG input
- saving passes normalized multi-line text to the manager
- refresh completion reloads the status text for success, partial success, and full failure

Application startup:

- startup background refresh still triggers when at least one EPG URL is configured
- startup does not block while multiple URLs are processed

## Risks

- Risk: merged XMLTV serialization introduces nondeterministic output.
  Mitigation: sort channels and programmes consistently before serialization.

- Risk: partial failures become hard to understand if the aggregated message is too verbose.
  Mitigation: keep status text compact and include only URL-level summaries.

- Risk: keeping the field name `epg_url` while storing several URLs may be mildly confusing in code.
  Mitigation: document the multi-line contract clearly in service and UI code comments where needed.

## Acceptance Criteria

- `直播源管理` accepts multiple EPG URLs in one global multi-line input
- existing single-URL configs continue to work without manual migration
- manual and startup refresh process all configured URLs in order
- successful URLs are merged into one effective cached guide
- duplicate programme windows prefer the earlier configured URL
- partial refresh failure still updates the cache when at least one URL succeeds
- full refresh failure keeps the previous cache intact
- custom live playback continues to show matched current and upcoming programmes from the merged guide
