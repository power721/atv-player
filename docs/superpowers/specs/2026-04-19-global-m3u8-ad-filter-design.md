# Global M3U8 Ad Filter Design

## Summary

Add a conservative M3U8 ad-filtering step to the shared playback path so all playback sources can remove clearly identified inserted ad segments before mpv starts playback.

The filter must prefer false negatives over false positives. It should only rewrite playlists when the playlist text contains explicit ad signatures such as `/adjump/`. If no explicit signature is found, playback must continue with the original URL unchanged.

## Goals

- Apply the same M3U8 ad-filtering behavior to all playback sources that ultimately produce a remote `.m3u8` URL.
- Remove only clearly identified ad segments and avoid heuristic-only deletion.
- Run the filtering step before playback starts so mpv receives a cleaned playlist URL or local file path.
- Fall back to the original URL when filtering is unnecessary or fails.
- Keep the implementation isolated enough that existing controllers do not need source-specific ad-filter logic.

## Non-Goals

- Detecting all possible ad insertions across arbitrary providers.
- Removing segments based only on `#EXT-X-DISCONTINUITY`, uniform durations, or naming-pattern guesses.
- Building a general local streaming proxy server.
- Changing playback behavior for non-M3U8 media such as `.mp4`, `.flv`, local files, or RTSP streams.
- Adding user-facing settings or UI for ad-filter configuration in this iteration.

## Current Behavior

All playback sources eventually resolve to `PlayItem.url` and optional request headers. `PlayerWindow` then sends that URL directly to `MpvWidget`, which forwards it to mpv.

Today there is no shared interception step for remote M3U8 playlists. If a provider inserts ad segments into the playlist, the player passes the playlist through unchanged and mpv plays those segments as part of normal playback.

## Design

### Shared Filtering Service

Add a dedicated M3U8 filtering service in the shared playback layer. Its responsibilities are:

- decide whether a given `PlayItem` is eligible for filtering
- fetch the remote M3U8 text with the play item's request headers
- remove only explicitly identified ad segments
- when changes were made, write a cleaned temporary M3U8 file and return that local path
- otherwise return the original URL unchanged

The service should only run for remote HTTP or HTTPS `.m3u8` URLs. Other URL schemes and local files should bypass filtering entirely.

### Ad Detection Rules

The filter must use an allowlist of strong ad signatures instead of heuristic-only inference.

Initial explicit signatures:

- `/adjump/`
- `/video/adjump/`

Deletion rule:

- when a media-segment URI line matches an explicit ad signature, remove that URI line and its associated `#EXTINF` line

Conservative constraints:

- do not delete a segment solely because it is inside a `#EXT-X-DISCONTINUITY` block
- do not delete a segment solely because durations are regular, short, or anomalous
- do not delete a segment solely because the filename pattern differs from neighboring segments

This means the filter may miss some ads, but it should not remove ordinary content that merely looks structurally different.

### Playlist Rewriting

The filter should preserve the original playlist as much as possible.

Rules:

- keep all non-ad tags and non-ad segment URIs in their original order
- remove only the matched ad segment pairs
- after segment removal, drop redundant `#EXT-X-DISCONTINUITY` lines when they no longer separate media on both sides
- when writing a cleaned local playlist file, rewrite remaining media-segment URIs to absolute URLs so local playback does not depend on the original remote playlist base path

To avoid breaking relative references, the cleaned playlist file should be created only after normalizing remaining media-segment URIs to absolute URLs. This keeps mpv playback stable when the cleaned file is stored locally.

If the fetched playlist is a master playlist rather than a media playlist, the service should not rewrite it in this iteration and should return the original URL unchanged.

### Playback Integration

Integrate filtering at the shared playback entry point in `PlayerWindow`, just before the current item is handed to `MpvWidget`.

Behavior:

- if the current item already has a playable URL, attempt M3U8 filtering before starting playback
- if the item still needs deferred URL resolution, keep the existing resolution flow first, then run filtering on the resolved URL before playback starts
- update the current `PlayItem.url` only when filtering produced a rewritten local playlist file
- keep `PlayItem.headers` unchanged for the fetch step and for any non-rewritten playback

This placement ensures browse, live, spider-plugin, Emby, Jellyfin, Telegram, and any future sources all share the same filtering behavior without controller-specific changes.

### Threading And UX

Filtering requires one extra playlist fetch before first playback of a qualifying item, so playback startup may be slightly slower.

To avoid blocking the UI thread:

- run filtering in the existing asynchronous play-item preparation flow
- keep the player-window failure handling consistent with current async resolution behavior
- if filtering fails because of network, parse, or file-write errors, log the error and continue with the original URL

The user explicitly accepts the startup tradeoff in exchange for safer filtering.

### Temporary File Handling

Cleaned playlists should be written under the app's temporary or cache area with stable but disposable filenames.

Requirements:

- each rewritten playlist should have a `.m3u8` suffix
- temporary files may be reused or overwritten for the same source URL if convenient
- failure to write a temporary file must result in fallback to the original URL

No persistent user-facing library or database model is needed for this feature.

## Error Handling

- If the URL is not a remote `.m3u8`, skip filtering.
- If fetching the playlist fails, log the failure and play the original URL.
- If the response body is empty or does not look like an M3U8 playlist, play the original URL.
- If no explicit ad signature is found, play the original URL.
- If rewriting produces an invalid or empty playable playlist, fall back to the original URL.
- If temporary file creation fails, fall back to the original URL.

Filtering errors must never prevent playback from starting when the original URL is still usable.

## Testing Strategy

Add or update tests to cover:

- explicit `/adjump/` segment detection removes only the matched `#EXTINF` plus URI pairs
- non-ad discontinuity blocks remain untouched
- redundant discontinuity markers are removed only after adjacent ad-segment deletion
- master playlists are passed through unchanged
- remote non-M3U8 URLs are passed through unchanged
- filtering failure falls back to the original URL
- player-window shared playback flow applies filtering before handing the URL to mpv
- all-source integration remains implicit by testing the shared playback entry point rather than source-specific controllers

## Risks And Mitigations

- Risk: local rewritten playlists break relative segment paths.
  Mitigation: rewrite media-segment URIs to absolute URLs before writing the cleaned local playlist.

- Risk: the extra fetch noticeably delays playback start.
  Mitigation: keep the operation asynchronous and restricted to remote `.m3u8` URLs only.

- Risk: false positives remove actual content.
  Mitigation: only delete segments that match explicit ad signatures; avoid structural heuristics.

- Risk: filtering bugs prevent playback entirely.
  Mitigation: treat filtering as best-effort and always fall back to the original URL on failure.

## Implementation Notes

- Prefer a focused service module rather than embedding parsing logic directly into `PlayerWindow`.
- Reuse existing `httpx` dependency for playlist fetching.
- Keep the first iteration scoped to top-level media playlists. Nested playlist recursion can be added later if needed.
