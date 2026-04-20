# Local HLS Proxy Design

## Summary

Add a built-in local HLS proxy that becomes the default playback path for all remote HTTP or HTTPS `.m3u8` URLs.

The proxy will run inside the desktop app on `127.0.0.1:2323` and act as the data plane for playlist rewriting, segment proxying, PNG-to-TS repair, conservative ad removal, in-memory caching, and short-range segment prefetching.

The existing PySide6 player UI remains the control plane. It should decide what to play and log what happened, while the new proxy is responsible for the bytes returned to mpv.

## Goals

- Route every remote `.m3u8` playback request through a built-in local proxy by default.
- Keep the current UI and controller structure intact and integrate incrementally into the existing repository.
- Rewrite media playlists and master playlists so mpv fetches all dependent resources through the local proxy.
- Preserve source-specific playback headers by storing them in proxy session state and replaying them on outbound origin requests.
- Repair disguised transport-stream segments that prepend a PNG payload before TS bytes.
- Remove only conservatively identified ad segments in the first iteration.
- Add bounded in-memory caching and short-range asynchronous prefetching for segment playback stability.
- Surface proxy failures in player logs and fall back to the original remote URL when proxy preparation fails before playback starts.

## Non-Goals

- Reorganizing the repository into a brand-new top-level project layout.
- Replacing mpv or moving playback logic out of the existing `PlayerWindow` and `MpvWidget`.
- Adding a persistent disk cache in this iteration.
- Building a generalized HTTP proxy for non-HLS media such as `.mp4`, `.flv`, RTSP, or local files.
- Exposing user-facing settings for proxy enablement, ad rules, cache size, or prefetch tuning in this iteration.
- Detecting every possible ad break with aggressive heuristics.
- Implementing a browser-facing streaming service outside the desktop application process.

## Current Behavior

Today the shared playback flow resolves a `PlayItem.url` and optional request headers in `PlayerWindow`, optionally rewrites some remote `.m3u8` URLs with `M3U8AdFilter`, and then forwards the final URL directly to mpv through `MpvWidget`.

This model fails for sources whose segment URLs are not directly playable by ffmpeg or mpv, including streams where segment payloads are disguised with a valid PNG header before TS bytes. It also keeps playlist rewriting and byte-level repair outside a reusable streaming boundary.

## Design

### Control Plane And Data Plane Boundary

The existing UI and playback session flow remain the control plane:

- `PlayerWindow` decides when playback starts and which `PlayItem` is active.
- controllers and plugins continue resolving titles, URLs, and request headers.
- `MpvWidget` remains responsible only for loading the final URL into mpv.

The new local HLS proxy becomes the data plane:

- fetching playlists and dependent assets from the origin
- rewriting playlists into local proxy URLs
- proxying segment and asset requests
- repairing segment byte streams
- caching and prefetching segment payloads
- reporting proxy activity through structured log messages

This split keeps UI logic separate from byte-oriented streaming behavior.

### Repository Integration

The implementation should fit the existing repository layout and add focused modules under `src/atv_player/proxy/`:

- `__init__.py`: public exports
- `server.py`: local HTTP server lifecycle and request routing on `127.0.0.1:2323`
- `session.py`: per-playback proxy session registry keyed by short token
- `m3u8.py`: playlist parsing, rewriting, and dependent-resource tracking
- `segment.py`: segment fetch, repair, cache integration, and prefetch coordination
- `stripper.py`: PNG-header stripping, TS sync detection, and TS alignment helpers
- `cache.py`: bounded in-memory TTL/LRU cache and in-flight request deduplication
- `adblock.py`: conservative first-pass ad detection rules

Existing modules integrate as follows:

- `player/m3u8_ad_filter.py` evolves into the shared entry point that decides whether a URL should be proxied and returns the local proxy URL.
- `ui/player_window.py` keeps the existing asynchronous preparation path but uses the proxy entry point instead of writing local playlists to disk.
- `player/mpv_widget.py` remains unaware of proxy internals and simply loads the local proxy URL it receives.

### Proxy Server Lifecycle

The app should own one proxy server instance for the process lifetime.

Requirements:

- bind to `127.0.0.1:2323`
- start lazily before the first proxied playback request or during app initialization if that is simpler
- be thread-safe for concurrent mpv requests
- expose a clear startup failure when port `2323` is unavailable
- support graceful shutdown when the app exits

`server.py` should use the Python standard-library HTTP server stack with a threaded request server. Flask is unnecessary for this repository.

### Playback Preparation Flow

For every current `PlayItem` in `PlayerWindow`:

- if the final URL is not a remote HTTP or HTTPS `.m3u8`, keep existing behavior and play it directly
- if the final URL is a remote `.m3u8`, ask the shared proxy-preparation service for a local proxy URL
- if proxy preparation succeeds, replace `PlayItem.url` with the local proxy URL before calling `MpvWidget.load`
- if proxy preparation fails, log the failure and continue with the original remote URL

The local proxy URL should look like:

- `http://127.0.0.1:2323/m3u?token=<session-token>`

The token maps to origin URL and header state stored in the proxy session registry.

### Session Registry

Each proxied playback item gets a short-lived proxy session entry containing:

- original playlist URL
- normalized playback headers
- creation time and last-access time
- most recently parsed playlist metadata
- indexed segment URLs for prefetch decisions

The registry exists in process memory only.

Session tokens should be opaque, short, and hard to guess. `secrets.token_urlsafe` is sufficient.

Old sessions should be evicted on a short inactivity timeout so playback history does not leak into long-lived process memory.

### Playlist Fetching And Rewriting

When mpv requests `/m3u?token=...`, the proxy should:

1. load the session entry
2. fetch the origin playlist with the stored headers using a shared `httpx.Client`
3. validate that the response body looks like an HLS playlist
4. parse the playlist line by line while preserving non-target tags
5. rewrite dependent URIs into local proxy URLs

Two playlist cases must be handled:

- media playlist: rewrite media segment URIs to `/seg?token=<token>&i=<index>`
- master playlist: rewrite variant URIs to `/m3u?token=<child-token>`

The rewriter must also rewrite dependent resource tags such as:

- `#EXT-X-KEY:URI="..."`
- `#EXT-X-MAP:URI="..."`

Those URIs should be rewritten to `/asset?...` so encryption keys and init segments still flow through the proxy and use the stored playback headers.

Relative URIs must be resolved against the current playlist URL before proxy rewriting.

### Ad Removal Rules

The first iteration should remain conservative.

A segment may be removed when at least one of the following is true:

- its absolute URL contains an explicit ad marker such as `/adjump/` or `/video/adjump/`
- its duration is less than `1.0` second
- its absolute URL contains a clear ad substring such as `ad` in an obvious path component or filename

Deletion behavior:

- remove the matched media URI line
- remove its associated `#EXTINF` line
- keep unrelated tags and surrounding structure whenever possible
- remove redundant `#EXT-X-DISCONTINUITY` lines only when they no longer separate media on both sides

The implementation must prefer false negatives over false positives. If parsing is ambiguous, keep the segment.

### Segment Proxying

When mpv requests `/seg?token=...&i=N`, the proxy should:

1. resolve the indexed segment URL from the stored playlist metadata
2. compute a cache key from absolute URL plus effective outbound headers
3. return cached repaired bytes when available
4. otherwise fetch the origin segment
5. repair the response payload if needed
6. store the repaired bytes in cache
7. return the repaired bytes as `video/MP2T`
8. trigger asynchronous prefetch of the next few indexed segments

The first implementation may buffer the segment payload in memory before returning it, as long as the code structure leaves room for a future fully streaming path. The cache requirement makes full buffering acceptable for the first iteration.

### Asset Proxying

When mpv requests `/asset?...`, the proxy should transparently fetch and return non-segment dependent resources such as:

- HLS encryption keys
- init segments referenced by `#EXT-X-MAP`
- other binary resources referenced from playlist tags

Assets should use the same shared `httpx.Client`, the same stored headers, and the same cache mechanism when useful. Asset responses must not go through PNG-to-TS repair.

### Segment Repair

`stripper.py` should implement byte-level repair helpers for disguised TS segments.

Repair steps:

1. search for the PNG `IEND` trailer bytes
2. if present, drop all bytes through the end of the PNG payload
3. search the remaining bytes for MPEG-TS sync byte `0x47`
4. if a sync byte is found, drop all leading bytes before the first sync position
5. if the repaired payload appears to contain TS packets, trim any leading misalignment so packet boundaries start on a `188`-byte packet boundary when possible
6. if no sync byte is found, fall back to the original bytes

This behavior is intentionally conservative. Failed repair should not destroy otherwise playable content.

### Caching

The first iteration should implement in-memory caching only.

Requirements:

- TTL/LRU bounded cache for segment bytes
- smaller TTL for playlists, longer TTL for segment payloads
- cache key includes origin URL plus a fingerprint of outbound headers
- reuse in-flight fetches so duplicate concurrent requests for the same segment do not trigger duplicate origin downloads
- keep the cache bounded for live streams and short VOD bursts

The implementation may use `cachetools`, which should be added as a dependency.

### Prefetching

After a segment fetch succeeds, the proxy should asynchronously prefetch the next `2` to `3` indexed segments from the same parsed media playlist.

Rules:

- only prefetch segment URLs already known from the latest playlist parse
- skip cached segments
- skip segments that are already in flight
- do not block the foreground response path on prefetch work
- prefetch should reuse the same fetch, repair, and cache code path as foreground segment requests

Prefetching is a stability and latency optimization, not a correctness dependency.

### Header Propagation

The proxy must preserve playback headers even though mpv only sees a localhost URL.

Outbound origin requests should:

- start from headers stored in the proxy session
- normalize keys and values to strings
- preserve headers such as `Referer`, `User-Agent`, cookies, and provider-specific fields when present
- add a fallback `User-Agent` only if the source did not provide one

Header propagation must be centralized inside the proxy rather than split across mpv request configuration and proxy handlers.

### Logging

The proxy should emit structured application logs for:

- proxy server startup success or failure
- session creation
- playlist fetch and rewrite results
- ad-segment removals
- cache hits and misses
- segment repair outcomes
- prefetch scheduling
- fatal proxy request failures

`PlayerWindow` should surface preparation failures in the existing playback log panel. Detailed byte-plane logs can remain in the application logger for now.

## Error Handling

- If the proxy server cannot bind to `127.0.0.1:2323`, playback preparation should log a clear error and fall back to the original remote URL.
- If proxy session creation fails, play the original remote URL.
- If the origin playlist fetch fails, `/m3u` should return `502 Bad Gateway`.
- If the origin response body is not a valid HLS playlist, `/m3u` should return `502 Bad Gateway`.
- If playlist rewriting fails unexpectedly, fall back to the original remote URL at preparation time when possible.
- If a segment fetch fails, `/seg` should return `502 Bad Gateway`.
- If segment repair fails to find a safe TS sync point, return the original bytes rather than an empty payload.
- If a requested segment index no longer exists for the current session, `/seg` should return `404`.
- If a session token is missing or expired, proxy endpoints should return `404`.

Proxy failures must never leave the UI blocked waiting indefinitely for playback preparation.

## Testing Strategy

Add focused coverage in new and existing test modules:

- `tests/test_hls_proxy_m3u8.py`
  - rewrites media playlists to local `/seg` URLs
  - rewrites master playlists to child `/m3u` URLs
  - rewrites `#EXT-X-KEY` and `#EXT-X-MAP` URIs to `/asset`
  - removes only conservatively matched ad segments
- `tests/test_hls_proxy_stripper.py`
  - strips a PNG preamble and returns bytes starting at TS sync
  - preserves plain TS payloads
  - falls back to original bytes when no sync byte is found
  - realigns a misaligned TS payload when possible
- `tests/test_hls_proxy_segment.py`
  - header propagation to origin fetches
  - cache hits avoid refetch
  - in-flight deduplication avoids duplicate downloads
  - prefetch schedules only the next few segments
- `tests/test_player_window_ui.py`
  - remote `.m3u8` URLs are prepared as `127.0.0.1:2323` local proxy URLs
  - preparation failure still falls back to the original URL and logs an error
- existing `tests/test_mpv_widget.py`
  - remain focused on mpv loading behavior and should not absorb proxy logic

## Risks And Mitigations

- Risk: proxying all remote `.m3u8` playback introduces a new failure point.
  Mitigation: keep preparation best-effort and fall back to the origin URL when proxy setup fails before playback starts.

- Risk: conservative ad rules still remove legitimate short segments.
  Mitigation: require explicit URL signals or very short duration, and prefer keeping ambiguous segments.

- Risk: in-memory buffering increases RAM usage on large or high-bitrate streams.
  Mitigation: keep cache size bounded, use short TTLs, and scope the first iteration to in-memory caching only.

- Risk: port `2323` may already be occupied.
  Mitigation: fail fast, log the exact startup problem, and fall back to direct playback.

- Risk: long-lived session state accumulates for many playback attempts.
  Mitigation: expire inactive sessions and clear them during normal server maintenance.

## Implementation Notes

- Reuse the existing asynchronous playback preparation path in `PlayerWindow` instead of adding a second orchestration flow.
- Keep proxy byte-processing code out of `MpvWidget` and source controllers.
- Reuse `httpx` for outbound requests and a single shared client for connection pooling.
- Add the proxy in incremental slices:
  1. local proxy server and session registry
  2. playlist rewriting for remote `.m3u8`
  3. segment fetch and PNG-to-TS repair
  4. cache and prefetch
  5. conservative ad removal

This sequencing keeps the repository structure stable while introducing the new data-plane boundary the user requested.
