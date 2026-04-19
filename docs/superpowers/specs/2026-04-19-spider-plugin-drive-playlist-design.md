# Spider Plugin Drive Playlist Design

## Summary

Allow spider-plugin detail playback to treat supported drive-share links as deferred playlist sources.

When a plugin returns a supported drive link instead of a direct media URL or plugin-resolvable play id, the player should resolve that link through `/tg-search/{token}?ac=gui&id={link}` and use the backend response as the playable playlist for the clicked item only.

## Goals

- Detect supported drive-share links returned by spider-plugin detail data.
- Resolve matched links through the existing `/tg-search/{token}` backend endpoint.
- Reuse the backend detail payload to build playable items instead of inventing a second parsing format.
- Keep ordinary media URLs and ordinary plugin `playerContent()` playback unchanged.
- Support mixed plugin playlists where some items are direct media URLs and some items are drive links.

## Non-Goals

- Changing Telegram search page behavior.
- Adding a new visible UI entry point for pasting drive links.
- Pre-resolving every drive link before the user clicks play.
- Changing non-plugin playback sources.
- Adding cross-item or cross-route caching for resolved drive playlists.

## Current Behavior

`SpiderPluginController._build_playlist()` parses `vod_play_url` into `PlayItem` objects.

For each item:

- if the value looks like a direct media URL, it is stored in `PlayItem.url`
- otherwise the value is stored in `PlayItem.vod_id`

At playback time, `SpiderPluginController._resolve_play_item()` only knows how to call `spider.playerContent(flag, id, vipFlags)` for items that do not already have a URL.

This means a plugin item whose value is a drive-share link is currently treated as a normal plugin play id. That fails because the item needs backend playlist resolution through `/tg-search/{token}?ac=gui&id={link}` instead of `playerContent()`.

## Supported Drive Detection

Add a focused drive-link detector in the plugin playback path.

The detector should match the following domains:

- `alipan.com`, `aliyundrive.com`
- `mypikpak.com`
- `xunlei.com`
- `123pan.com`, `123pan.cn`, `123684.com`, `123865.com`, `123912.com`, `123592.com`
- `quark.cn`
- `139.com`
- `uc.cn`
- `115.com`, `115cdn.com`, `anxia.com`
- `189.cn`
- `baidu.com`

Detection only needs to answer whether a value should be treated as a drive-share link. The numeric type codes used elsewhere do not need to be persisted in this feature because the `/tg-search/{token}` detail endpoint only requires the raw link.

## Design

### API Layer

`ApiClient` should expose a dedicated method for drive-share detail resolution through the existing Telegram-search backend:

- request path: `/tg-search/{vod_token}`
- query params: `ac=gui`, `id=<raw link>`

The method should return the raw payload so controllers can map it using the existing browse-item conversion utilities.

Even though the endpoint is shared with Telegram search detail, this feature should use a clearly named API method so plugin playback logic stays readable and future callers do not need to know the endpoint aliasing.

### Playlist Parsing Reuse

The Telegram-search detail format already maps cleanly into the player model:

- `vod_play_url` can produce deferred `PlayItem` entries
- `items` can act as a fallback playlist when present

The plugin flow should reuse the same playlist-building rules instead of duplicating backend-detail parsing in a plugin-specific format.

To keep the mapping consistent, extract or reuse the existing Telegram-search playlist parsing helper for both controllers.

### Plugin Playback Resolution

`SpiderPluginController._resolve_play_item()` should branch in this order:

1. If the item already has `url`, return immediately.
2. If the item does not have `vod_id`, return immediately.
3. If `vod_id` is a supported drive-share link:
   - call the new API method
   - map the returned detail item
   - build a playlist using the reused Telegram-search parsing rules
   - choose the first playable entry from that resolved playlist
   - copy the playable URL and any resolvable metadata needed by the clicked item
4. Otherwise, keep the existing `spider.playerContent()` flow.

This preserves lazy resolution: only the clicked drive item is resolved, and it is resolved only when playback actually needs it.

### Mixed Playlist Semantics

Mixed plugin playlists should work without changing request construction:

- direct media URL items keep their existing `url`
- ordinary plugin play ids keep using `playerContent()`
- drive-share link items use `/tg-search/{token}?ac=gui&id={link}`

No attempt should be made to replace the entire plugin request playlist with the resolved drive-detail playlist. The clicked item is resolved in place to a concrete playable URL, which matches the existing player contract for deferred plugin playback.

### Error Handling

If the backend detail payload for a drive link:

- has no `list` entry, raise the existing "没有可播放的项目" style error
- maps to an empty playlist and has no fallback items, raise the existing "没有可播放的项目" style error
- resolves to playlist entries that still do not yield a usable URL, raise a playback-resolution error

This keeps failure reporting aligned with current plugin playback behavior.

## Testing Strategy

Add or update tests for:

- API client builds the `/tg-search/{token}?ac=gui&id=<link>` request for drive-link detail resolution
- plugin controller detects supported drive-share links
- plugin playback resolution keeps using `playerContent()` for ordinary non-link ids
- plugin playback resolution bypasses `playerContent()` for supported drive links
- plugin playback resolution maps backend detail payload into a playable URL for the clicked item
- mixed playlists keep direct media URLs unchanged while resolving only drive-link items on demand

## Risks And Mitigations

- Risk: plugin playback becomes tightly coupled to Telegram-search controller details.
  Mitigation: share only the narrow playlist-parsing helper, not controller behavior.

- Risk: some plugin play ids may contain a supported domain name but are not real share links.
  Mitigation: limit the detector to URL-like strings and supported domains only.

- Risk: backend drive detail may return nested deferred items instead of direct URLs.
  Mitigation: treat missing playable URLs as a resolution failure rather than silently misplaying the item.
