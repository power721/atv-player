# Custom Live EPG Design

## Summary

Add a single global `EPG URL` configuration for custom live sources. The app should cache XMLTV data locally, refresh that cache asynchronously on every app startup, and also asynchronously refresh all remote custom live sources on startup. When the user plays a custom live channel, the player-side metadata panel should immediately show the matched program guide summary for that channel:

- current program
- next program

This change is scoped to custom live playback only. It does not change server-provided live categories or live-card subtitles in the browsing UI.

## Goals

- Add one global `EPG URL` setting shared by all custom live sources
- Expose that setting inside `直播源管理`
- Cache EPG data locally and reuse the latest successful cache when refresh fails
- Trigger non-blocking startup refresh for:
  - the global EPG cache
  - every remote custom live source
- Show `当前节目` and `下一节目` in the player metadata panel for custom live playback when EPG data matches the channel

## Non-Goals

- Per-source EPG configuration
- Scheduled background refresh after startup
- EPG display for backend live sources
- EPG display in live browsing cards or category lists
- Full-day timeline UI inside the player
- Aggressive fuzzy matching across unrelated channel names

## User Experience

`LiveSourceManagerDialog` should gain a compact global EPG section above the source table:

- `EPG URL` text input
- `保存` button
- `立即更新` button
- a small status label for the latest refresh result

Behavior:

- Saving only updates the stored global URL. It does not have to fetch immediately.
- Clicking `立即更新` fetches and parses the configured EPG in the background and updates the status label when finished.
- Opening the app should not wait for EPG refresh or remote live-source refresh to finish.
- If startup refresh fails, the app should remain usable and keep the previous cache.

For custom live playback:

- the player metadata panel should continue using the existing `detail_style="live"` layout
- when EPG data matches the current custom live channel, append:
  - `当前节目: HH:MM-HH:MM 节目名`
  - `下一节目: HH:MM-HH:MM 节目名`
- if no matching EPG data exists, omit those fields instead of showing placeholder errors

## Storage

Keep global EPG state outside `live_source` because it is shared across all custom live sources.

Add a dedicated table such as `live_epg_config` with a single logical row:

- `id INTEGER PRIMARY KEY CHECK (id = 1)`
- `epg_url TEXT NOT NULL DEFAULT ''`
- `cache_text TEXT NOT NULL DEFAULT ''`
- `last_refreshed_at INTEGER NOT NULL DEFAULT 0`
- `last_error TEXT NOT NULL DEFAULT ''`

Repository responsibilities:

- create the table if missing
- seed row `id = 1` on first initialization
- load current config
- update URL without clearing existing cache
- update cache text, refresh stamp, and last error after refresh attempts

The existing `live_source` table remains responsible only for source definitions and source playlist caches.

## Architecture

Introduce a focused EPG service layer instead of mixing XMLTV logic into `CustomLiveService`.

### `LiveEpgRepository`

Owns persistence for the single global EPG config row.

Responsibilities:

- load the current config
- save URL changes
- persist refresh results

### `LiveEpgService`

Owns EPG refresh, parsing, lookup, and schedule extraction.

Responsibilities:

- fetch XMLTV text from the configured URL through the existing HTTP text client abstraction
- parse XMLTV into in-memory channel/program structures
- reuse cached XMLTV text when available
- return current/next program summary for a given custom live channel name
- expose background-safe refresh entry points for startup and manual UI refresh

### `AppCoordinator`

After constructing `CustomLiveService` and `LiveEpgService`, start non-blocking background work:

- refresh EPG cache if `epg_url` is configured
- refresh every remote live source through `CustomLiveService.refresh_source()`

These startup tasks must:

- run on background threads
- swallow errors after recording them in repositories
- never block main-window creation

### `CustomLiveService`

Stay responsible for custom live browsing and playback requests. Do not move XMLTV parsing into it.

Add one integration point:

- when building a custom live `OpenPlayerRequest`, enrich the returned `VodItem` with EPG-derived text for the current channel if available

The service should ask `LiveEpgService` for schedule info using the merged custom live channel name that is already used for playback.

### `VodItem`

Add two optional fields for player-facing EPG display:

- `epg_current: str = ''`
- `epg_next: str = ''`

These fields travel through the existing request/session flow and avoid inventing a separate UI-only store.

## EPG Parsing And Matching

Parse only the XMLTV fields needed for this feature:

- channel id
- channel display name(s)
- programme start
- programme stop
- programme title

Ignore richer XMLTV metadata for now.

Matching priority:

1. Exact match between custom live channel name and any XMLTV display name
2. Normalized match between the live channel name and XMLTV display names

Normalization should stay deliberately small and deterministic:

- trim whitespace
- lowercase for comparison
- remove spaces
- remove `-` and `_`
- normalize full-width and half-width parentheses
- normalize `cctv1` and `cctv-1` style naming by removing separators between letters and digits

If no match is found, return no schedule.

Current/next program selection:

- use local current time when evaluating programme windows
- `current` is the programme whose `[start, stop)` range contains now
- `next` is the earliest programme whose start is at or after the current programme stop
- if there is no current programme, do not synthesize one from future items
- if there is a current programme but no later entry today, omit `下一节目`

## Player Metadata Rendering

Keep the existing live metadata block and extend it only for custom live sessions with available EPG data.

Recommended rendered rows:

- `标题`
- `平台`
- `类型`
- `主播`
- `人气`
- `当前节目`
- `下一节目`

Implementation detail:

- store the formatted EPG lines on `VodItem.epg_current` and `VodItem.epg_next`
- the player metadata renderer should append the two EPG rows only when values are non-empty

This keeps the current player data flow intact and avoids background UI fetches after playback starts.

## Failure Handling

- Empty `EPG URL`: skip refresh and treat EPG as disabled
- Network failure during refresh: keep old cache, update `last_error`
- XML parse failure during refresh: keep old cache, update `last_error`
- Missing cache and failed first refresh: playback remains available without EPG rows
- Channel name unmatched in EPG: playback remains available without EPG rows

No modal error dialogs are required for startup refresh failures.

## Testing

Add tests for the following behavior.

Repository:

- `LiveEpgRepository` creates and seeds the config table
- URL changes round-trip without clearing cache text
- refresh results persist cache text, `last_refreshed_at`, and `last_error`
- migration behavior works on existing databases that lack the new table

Service:

- XMLTV parsing extracts channels and programmes needed for lookup
- exact-name match returns current and next programme
- normalized-name match handles `CCTV-1` and `CCTV1` style differences
- unmatched channel returns no schedule
- refresh failure preserves old cache and records error

Application startup:

- `AppCoordinator` schedules startup refresh for configured EPG plus remote custom live sources
- startup returns the main window immediately without waiting for refresh completion

UI:

- `LiveSourceManagerDialog` renders the global EPG controls
- saving the URL calls the EPG manager service correctly
- manual EPG refresh updates status after completion
- player metadata for custom live sessions includes `当前节目` and `下一节目` when populated
- player metadata for other live sessions remains unchanged

## Risks

- Risk: channel-name mismatch leaves users with no visible EPG even when the guide is valid.
  Mitigation: keep normalization deterministic and cover common CCTV naming differences first.

- Risk: startup background refresh races with immediate playback.
  Mitigation: playback should always use the most recent cached XMLTV and never depend on refresh completion.

- Risk: mixing EPG concerns into live-source persistence grows the repository into a grab bag.
  Mitigation: keep EPG storage in its own repository and table.

## Acceptance Criteria

- `直播源管理` shows a global `EPG URL` configuration section above the source list
- the global EPG URL persists across restarts
- app startup asynchronously refreshes the configured EPG and all remote custom live sources
- startup refresh failures do not block UI and do not discard old cache data
- custom live playback shows `当前节目` and `下一节目` in the player details when a channel matches the cached EPG
- backend live playback remains unchanged
