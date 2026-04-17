# Live Duplicate Channel Merge Design

## Summary

When a custom live source contains multiple entries with the same channel name, the live list should show that channel only once. Opening it should pass all preserved stream URLs into the player as a multi-item playlist so the user can switch lines after playback starts.

The merge applies only within the same browsing scope:

- grouped channels merge only with channels that share the same group and name
- ungrouped channels merge only with ungrouped channels that share the same name

The player-side playlist should preserve the original order of URLs and title each playable line with the channel name plus a numeric suffix only when multiple lines exist, such as `CCTV1综合 1`, `CCTV1综合 2`, and `CCTV1综合 3`.

## Goals

- Show only one channel card for duplicate custom live entries that share the same group and channel name.
- Preserve every original stream URL as a playable line in the player.
- Keep line order stable based on original playlist order.
- Reuse the existing player playlist UI for manual line switching.
- Apply the same behavior to remote, local, and manual custom live sources.
- Preserve per-line HTTP headers for playback.

## Non-Goals

- Changing server-provided live categories or playback behavior from the backend `/live/{token}` API.
- Auto-switching to another line when one line fails.
- Showing line count badges or source-domain labels in the channel list.
- Expanding the player UI to display per-line poster art or metadata beyond the existing playlist titles.
- Deduplicating channels across different groups or across different live sources.

## User Experience

### Live Channel List

Inside a custom live source category, duplicate channel entries should collapse into one visible channel item. For example, if a source contains four `CCTV1综合` entries under `央视频道`, the list should show only one `CCTV1综合` card.

If the same name appears in different groups, each group keeps its own channel item. This avoids merging entries that the source author intentionally separated.

### Player Playlist

Clicking a merged channel should open the player with one playlist item per preserved stream URL.

Playlist title rules:

- if the channel has one stream URL, keep the original channel name such as `CCTV1综合`
- if the channel has multiple stream URLs, suffix each line with a 1-based index such as `CCTV1综合 1`, `CCTV1综合 2`, and `CCTV1综合 3`

This keeps the channel list clean while still exposing manual line switching through the current player playlist panel.

## Architecture

### Parser Boundary

`parse_m3u()` should remain a raw parser. It should continue to emit one parsed channel per `#EXTINF + URL` pair and should not absorb duplicate-merging behavior.

This keeps the parser responsible only for syntactic translation from `m3u` text into `ParsedPlaylist`.

### Custom Live Aggregation

Duplicate merging should happen inside `CustomLiveService`, after a source has been loaded into `ParsedPlaylist`.

Add a service-level aggregation step that:

- groups channels by `(group scope, channel name)`
- preserves the first occurrence order for list display
- keeps all original stream URLs attached to the merged channel
- keeps each line's own headers
- selects channel-level logo data from the first non-empty logo in the merged set

The grouping key should be:

- `(group_key, channel.name)` for grouped channels
- `("", channel.name)` for ungrouped channels

This ensures duplicates merge only within the same browsing scope.

### View Model Shape

`CustomLiveService` currently iterates individual `LiveSourceChannelView` objects, each with one stream URL. That is no longer enough for merged playback.

The service should instead operate on a merged channel view that contains:

- a stable `channel_id` derived from the first parsed entry
- the shared display name
- the shared group identity
- one channel-level logo
- an ordered list of playable lines, where each line carries its own `url`, `headers`, and optional logo

This merged structure can remain private to `CustomLiveService`. No change is required to the public `LiveController` contract.

## Data Flow

### Loading Items

`load_items()` and `load_folder_items()` should build `VodItem` cards from merged channel views rather than raw parsed channels.

Effects:

- duplicate channels within the same group collapse to one `VodItem`
- the visible `vod_name` stays equal to the original channel name
- the visible `vod_pic` uses the merged channel logo
- the visible `vod_id` uses the merged channel's stable `channel_id`

### Building Player Requests

`build_request()` should resolve the merged channel by `channel_id` and generate the player playlist from all preserved lines.

For each playable line:

- `PlayItem.url` comes from that line's original stream URL
- `PlayItem.headers` comes from that line's own parsed headers
- `PlayItem.index` follows merged order
- `PlayItem.title` follows the naming rule for single-line versus multi-line channels

The returned `OpenPlayerRequest` remains a normal live playback request and still disables local history.

## Ordering And Identity Rules

- The first duplicate entry defines the merged channel's `channel_id`.
- The first duplicate entry determines the merged channel's position in the channel list.
- Later duplicate entries append additional lines to that merged channel in original order.
- Different groups with the same channel name do not merge.
- Manual live entries use the same rules as parsed `m3u` channels.

This preserves deterministic browsing and playback while minimizing surface-area changes.

## Error Handling

- If a merged channel has no playable URLs, it should not be exposed in the channel list.
- `build_request()` should keep the existing `没有可播放的项目` failure when no line can be resolved for the requested channel.
- Playback failure for one line should not auto-advance to another line. The existing player failure logging remains the only failure handling in this change.

## Testing

Add focused service-layer coverage for:

- grouped duplicate channels collapsing to one visible `VodItem`
- ungrouped duplicate channels collapsing to one visible `VodItem`
- different groups with the same channel name remaining separate
- merged player requests expanding to multiple `PlayItem` entries in source order
- multi-line titles using `频道名 1`, `频道名 2`, and so on
- single-line channels keeping the unsuffixed original title
- per-line HTTP headers remaining attached to the correct `PlayItem`
- manual-source duplicate channels following the same merge rules

Parser tests should stay focused on raw `m3u` parsing and should not be rewritten to expect deduplication.

## Risks And Mitigations

- Risk: moving deduplication into the parser would blur the boundary between parsing and product behavior.
  Mitigation: keep merging in `CustomLiveService` only.
- Risk: merged channels could accidentally lose later-line headers.
  Mitigation: store playback lines as distinct records and build each `PlayItem` from its own line data.
- Risk: merging across groups could hide intentionally separated sources.
  Mitigation: scope the merge key to group plus channel name rather than channel name alone.
