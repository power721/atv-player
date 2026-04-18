# Live TXT Source Design

## Summary

Add support for the common plain-text live source format that uses `分组名,#genre#` group markers and `频道名,URL` channel rows.

The app should accept this format from both remote URLs and local files, parse it into the existing live playlist model, and keep the current duplicate-channel merge behavior so same-name channels within the same group become one channel with multiple playable lines.

## Goals

- Support remote `txt` live source URLs in the existing custom live source flow.
- Support local `txt` live source files in the existing custom live source flow.
- Parse the provided text format into the same `ParsedPlaylist` structure used by `m3u`.
- Preserve current duplicate-channel merge behavior in `CustomLiveService`.
- Keep the existing player playlist behavior for multi-line playback.

## Non-Goals

- Supporting arbitrary `txt` playlist dialects beyond the explicitly approved format.
- Adding `txt`-specific HTTP header metadata.
- Changing backend `/live/{token}` live categories or playback behavior.
- Adding source-type selection UI or a separate import wizard.

## Supported TXT Format

The supported plain-text format is intentionally narrow:

- a group row uses `组名,#genre#`
- a channel row uses `频道名,URL`
- duplicate channel rows with the same channel name under the same group are allowed
- blank lines should be ignored
- comment lines that start with `#` should be ignored, except `#genre#` used as the second field in a group row

Example:

```text
🇨🇳IPV4线路,#genre#
CCTV-1,http://107.150.60.122/live/cctv1hd.m3u8
CCTV-1,http://63.141.230.178:82/gslb/zbdq5.m3u8?id=cctv1hd
```

## Architecture

### Parser Boundary

Keep `parse_m3u()` focused on raw `m3u` parsing. Do not mix `txt` syntax into that function.

Add a new parser entrypoint that returns `ParsedPlaylist` for either supported source format. A suitable shape is:

```python
parse_live_playlist(text: str) -> ParsedPlaylist
```

Detection rule:

- if the trimmed content starts with `#EXTM3U`, parse as `m3u`
- otherwise, parse as the approved `txt` format

This keeps format detection centralized and leaves `CustomLiveService` unaware of concrete source syntax.

### TXT Parsing Rules

The `txt` parser should process non-empty lines in order.

- Split each non-comment line on the first comma only.
- If the second field equals `#genre#` after trimming, start or switch the current group to the first field.
- Otherwise, treat the line as a channel row where:
  - the first field is the channel display name
  - the second field is the stream URL
- Channel rows belong to the most recently declared group.
- If a channel row appears before any group row, store it as an ungrouped channel.

Output should reuse the existing models:

- grouped channels become `ParsedGroup` entries with `ParsedChannel` rows
- ungrouped channels become `ParsedPlaylist.ungrouped_channels`
- channel keys should remain stable and sequential, matching current parser behavior

No `txt`-specific logo or header metadata is produced in this first version.

### Service Integration

`CustomLiveService` should stop calling `parse_m3u()` directly and instead call the new unified parser entrypoint.

This change should apply uniformly to:

- remote cached source text
- freshly downloaded remote source text
- local file source text

Manual sources remain unchanged because they already build `ParsedPlaylist` directly from repository rows.

### Duplicate Channel Merge

No new merge logic is needed. Once `txt` input is converted into `ParsedPlaylist`, the existing merge layer in `CustomLiveService` should continue to:

- merge channels by `(group_key, channel_name)`
- preserve line order
- expose one visible channel card
- build one `PlayItem` per preserved line in the player playlist

This preserves the approved behavior for one channel with multiple lines.

## UI Changes

The live source manager should stop implying that only `m3u` is supported.

Required updates:

- remote source prompt label changes from `M3U URL` to `直播源 URL`
- local file picker filter expands from `*.m3u *.m3u8` to `*.m3u *.m3u8 *.txt`

The add-source flow, auto-generated display names, and refresh flow should remain unchanged.

## Error Handling

- Malformed `txt` lines without a valid comma-separated pair should be ignored.
- Channel rows with empty names or empty URLs should be ignored.
- Empty groups should not produce visible channels by themselves unless the source consists only of groups, in which case existing empty-state behavior remains unchanged.
- Format detection should prefer `m3u` only for explicit `#EXTM3U` content to avoid accidental misclassification of plain-text sources.

## Testing

Add coverage for:

- parsing grouped `txt` channel rows into `ParsedPlaylist`
- preserving ungrouped `txt` channels when no group marker appears first
- ignoring blank lines, comments, and malformed `txt` rows
- loading local `txt` files through `CustomLiveService`
- merging duplicate same-name `txt` channels within one group into one visible item with multiple playlist lines
- keeping same-name channels in different groups separate
- exposing `*.txt` in the local source picker filter
- showing the updated `直播源 URL` remote prompt label

## Risks And Mitigations

- Risk: broadening `parse_m3u()` would tangle two unrelated formats.
  Mitigation: add a dedicated `txt` parser and a small format-dispatch entrypoint.
- Risk: permissive `txt` parsing could silently accept broken rows.
  Mitigation: ignore malformed rows and keep parser scope limited to the approved syntax only.
- Risk: UI still signaling `m3u`-only support would make the feature hard to discover.
  Mitigation: update the prompt label and file picker filter together with parser support.
