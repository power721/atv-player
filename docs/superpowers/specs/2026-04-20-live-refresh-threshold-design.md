# Live Refresh Threshold Design

## Summary

Change live source and EPG refresh metadata from opaque counters to real Unix timestamps, display those values as local datetimes in `直播源管理`, and limit startup background refresh to stale data older than four hours.

Manual refresh actions remain unconditional.

## Goals

- Show readable refresh times for live sources in `直播源管理`.
- Show a readable EPG refresh time in the EPG status area.
- Refresh live sources and EPG automatically on startup only when they are stale.
- Keep the current manual refresh buttons working without a staleness check.
- Preserve the last successful refresh time when a refresh attempt fails.

## Non-Goals

- Changing the source-management workflow or adding refresh settings in the UI.
- Refreshing manual live sources automatically.
- Changing playlist parsing, playback, or category ordering.
- Backfilling historical counter values into exact timestamps.

## Data Semantics

`LiveSourceConfig.last_refreshed_at` and `LiveEpgConfig.last_refreshed_at` should both mean:

- Unix timestamp in seconds for the most recent successful refresh
- `0` when no successful refresh time is known

This replaces the current counter-like usage where successful refreshes increment the stored value by one.

Compatibility rule for existing local data:

- values below a minimum plausible Unix timestamp should be treated as unknown legacy counter data
- unknown legacy values should behave the same as `0`
- unknown values should render as empty in the UI and should be considered stale at startup

Using a plausibility threshold avoids showing `1970-01-01` for old counter-based records and makes the startup refresh decision deterministic.

## Refresh Write Rules

Successful refresh:

- live source refresh writes the fetched cache text, clears `last_error`, and stores the current Unix timestamp
- EPG refresh writes the merged XMLTV cache text, stores the current Unix timestamp, and keeps any partial-source errors in `last_error` as it does now

Failed refresh:

- keep the previous `last_refreshed_at`
- keep the previous cached content
- update `last_error`

This keeps `最近刷新` and the EPG status time aligned with the last successful refresh, not the last attempted refresh.

## UI Formatting

`直播源管理` should format refresh times through the existing local datetime formatter so both source rows and EPG status show `YYYY-MM-DD HH:MM:SS`.

Required behavior:

- live source table column `最近刷新` shows a formatted local datetime for plausible timestamps
- live source table shows an empty string for `0` or legacy counter values
- EPG status label shows formatted local datetime when there is no current error and the stored time is plausible
- EPG status label continues to show `last_error` verbatim when an error exists

No other columns or dialog actions change.

## Startup Refresh Policy

Keep the current startup background-refresh entry point in `App._start_live_background_refresh()`.

Define a shared staleness rule:

- refresh when the stored time is unknown
- refresh when the stored time is older than four hours
- skip when the stored time is within the last four hours

EPG startup refresh:

- only run when `epg_url` is non-empty
- then apply the staleness rule to `LiveEpgConfig.last_refreshed_at`

Live source startup refresh:

- iterate existing sources as today
- skip `manual` sources
- apply the staleness rule to `remote` and `local` sources

Manual dialog actions:

- `刷新` for a selected live source still refreshes immediately
- `立即更新` for EPG still refreshes immediately

## Implementation Shape

Recommended shape:

- add a small time utility helper for validating stored Unix timestamps and checking whether a refresh is stale
- reuse the existing datetime formatting helper for display
- update live source and EPG refresh services to write current Unix time on success
- update startup background refresh to call the staleness helper before scheduling work

This keeps timestamp interpretation centralized instead of duplicating threshold logic across UI, services, and startup flow.

## Testing

Add or update tests for:

- live source dialog rendering formatted source refresh times
- live source dialog rendering formatted EPG refresh time
- hiding legacy counter values or zero values in the UI
- live source refresh writing a real current timestamp on success
- EPG refresh writing a real current timestamp on success
- preserving previous successful timestamps on refresh failure
- startup refresh skipping fresh EPG data
- startup refresh skipping fresh live sources
- startup refresh refreshing stale live sources and stale EPG data
- startup refresh treating legacy counter values as stale
- startup refresh continuing to skip manual sources

## Risks And Mitigations

- Risk: old counter-based values could be mistaken for valid timestamps.
  Mitigation: define and reuse a plausibility threshold, and treat lower values as unknown.
- Risk: time checks could drift if implemented separately in multiple places.
  Mitigation: centralize timestamp validation and stale-check helpers.
- Risk: failed refreshes could overwrite the last known good timestamp.
  Mitigation: keep the existing failure pattern of preserving cached content and successful refresh metadata.
