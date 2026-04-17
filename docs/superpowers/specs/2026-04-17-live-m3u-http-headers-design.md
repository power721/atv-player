# Live M3U HTTP Headers Design

## Summary

Support custom HTTP request headers declared in custom live `m3u` playlists so streams that require headers such as `User-Agent` or `Referer` can play correctly.

The target input shape is `#EXTINF` metadata like:

```text
#EXTINF:-1 tvg-id="江苏卫视" tvg-name="江苏卫视" http-user-agent="AptvPlayer-UA" http-header="Referer=https://litchi-play-encrypted-site.jstv.com/" group-title="卫视IPV4",江苏卫视
```

The implementation should parse these header declarations from custom `m3u` sources, carry them through `PlayItem.headers`, and rely on the existing mpv header loading path.

## Goals

- Parse `http-user-agent` from custom `m3u` `#EXTINF` metadata
- Parse `http-header` from custom `m3u` `#EXTINF` metadata
- Support multiple `http-header` values separated by `&`
- Pass parsed headers through custom live playback requests into `PlayItem.headers`

## Non-Goals

- Changing backend `/live` detail parsing
- Supporting additional `http-header` separators beyond `&`
- Adding UI for manual editing of playback headers
- Changing the existing mpv header application mechanism

## Design

### Parsing Layer

Extend `ParsedChannel` in `src/atv_player/m3u_parser.py` with:

- `headers: dict[str, str]`

Header parsing rules:

- `http-user-agent="AptvPlayer-UA"` becomes `{"User-Agent": "AptvPlayer-UA"}`
- `http-header="Referer=https://a/&Origin=https://b/"` is split on `&`
- each segment is split on the first `=`
- invalid segments without `=` are ignored
- empty values are ignored
- if the same header key appears more than once, the last one wins

Header-name behavior:

- `http-user-agent` always maps to `User-Agent`
- keys from `http-header` are preserved as provided

### Service Layer

`CustomLiveService` should continue using the parser output as its source of truth.

When building `OpenPlayerRequest` for a custom live channel:

- copy parsed headers into `PlayItem.headers`

Browsing cards do not need to display header information.

### Player Layer

No new player protocol is required.

The existing path already supports:

- `PlayItem.headers`
- `PlayerWindow` passing headers to `MpvWidget.load()`
- `MpvWidget` converting headers into mpv `http-header-fields`

This change should only reuse that existing behavior.

## Testing

Add focused tests for:

- `tests/test_m3u_parser.py`
  - parses `http-user-agent`
  - parses `http-header` with `&`-separated multiple headers
  - ignores malformed header segments
- `tests/test_custom_live_service.py`
  - custom playback request includes parsed headers in `PlayItem.headers`

Add a player-layer test only if current coverage does not already prove that `PlayItem.headers` reaches mpv header fields.

## Risks And Mitigations

- Risk: malformed `http-header` content breaks playlist parsing.
  Mitigation: ignore invalid segments and keep playlist parsing tolerant.
- Risk: headers parse correctly but do not reach playback.
  Mitigation: add focused service coverage asserting `PlayItem.headers` contents.
