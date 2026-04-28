# MGTV Danmaku Provider Design

## Summary

Extend the internal Python danmaku service with a first-party `mgtv` provider. The provider should search MGTV by title, expand collection-level search hits into episode-level candidates when possible, resolve a selected MGTV play URL into segmented danmaku JSON requests, convert the returned comments into unified `DanmakuRecord` values, and let the existing service continue to emit final XML through `build_xml()`.

This design keeps the current service contract unchanged:

- `search_danmu(name, reg_src)` returns normalized `DanmakuSearchItem` values
- `resolve_danmu(page_url)` returns XML
- provider implementations still resolve into `list[DanmakuRecord]`
- `build_xml()` remains the single XML serializer

## Goals

- Replace the current `MgtvDanmakuProvider` stub with a working implementation in `src/atv_player/danmaku/providers/mgtv.py`.
- Support MGTV title search through the public search endpoint at `mobileso.bz.mgtv.com`.
- Filter search results to MGTV-owned media entries only.
- Expand collection hits into episode candidates for series and variety content so the service can match requested episode numbers.
- Resolve MGTV play URLs into danmaku records using the current segmented barrage APIs.
- Prefer the newer CDN-backed barrage segment API, but fall back to the older time-sliced barrage API when the newer path is unavailable.
- Convert MGTV comment shape, position, and color fields into the existing unified `DanmakuRecord` model.
- Cover the provider with deterministic unit tests using mocked HTTP responses only.

## Non-Goals

- Adding a new public API endpoint for danmaku.
- Changing player UI behavior or adding MGTV-specific UI state.
- Building persistence or cross-process caches for search metadata.
- Supporting MGTV live danmaku or historical pagination beyond the standard segmented APIs.
- Reproducing every filtering rule from the JavaScript reference for all media classes.

## Scope

Primary implementation should live in:

- `src/atv_player/danmaku/providers/mgtv.py`
- `src/atv_player/danmaku/providers/__init__.py` only if import shape changes
- `src/atv_player/danmaku/service.py` only if default ordering or instantiation needs a compatibility update

Primary verification should live in:

- new `tests/test_danmaku_mgtv_provider.py`
- updated `tests/test_danmaku_service.py` only if service-level behavior needs explicit MGTV coverage

## Public Interface

`MgtvDanmakuProvider` should keep the same external shape as other providers:

- `search(name: str) -> list[DanmakuSearchItem]`
- `resolve(page_url: str) -> list[DanmakuRecord]`
- `supports(page_url: str) -> bool`

No danmaku model changes are required for the first MGTV version. `DanmakuSearchItem` already carries the fields the service consumes, and MGTV-specific identifiers can stay internal to the provider.

## Search Strategy

The provider should call:

- `https://mobileso.bz.mgtv.com/msite/search/v2`

using the normalized query string as `q` and browser-like headers matching the behavior used by other providers.

The provider should:

1. parse `data.contents`
2. keep only entries where:
   - top-level `content.type == "media"`
   - item `source == "imgo"`
3. strip HTML tags from result titles
4. extract `collection_id` from item URLs like `/b/{cid}/...`
5. build canonical MGTV URLs from the result data or later episode expansion

### Candidate Expansion

MGTV search results are often collection-level rather than episode-level. To make episode-specific searches work with the current service filtering, the provider should expand search hits into episode candidates when possible.

The provider should distinguish two cases:

- movie-like result:
  - return one candidate pointing to the best main-feature play URL
- episodic or variety-like result:
  - fetch the collection episode list and return one candidate per retained episode

The expansion step should live entirely inside the MGTV provider so `DanmakuService.search_danmu()` can keep treating MGTV like every other provider.

## Collection Episode Expansion

The provider should query:

- `https://pcweb.api.mgtv.com/variety/showlist`

with `collection_id`, `month`, and `page=1`.

MGTV paginates by month tabs. The provider should:

1. request the default month first
2. read `data.tab_m`
3. request each additional month tab
4. merge all `data.list` episode entries

For the first version, episode retention should be pragmatic rather than exhaustive:

- drop entries marked as preview content when `isnew == "2"`
- drop clearly non-main-content titles such as trailers, highlights, clips,幕后,花絮,彩蛋, reaction, and similar obvious noise
- keep intact or normal episode items

### Episode Naming

Each expanded candidate name should combine the base series title with the episode title assembled from MGTV fields:

- `t2`
- `t1`
- fallback `t3`

This ensures the existing service can still run title similarity and episode-number matching against human-readable episode names such as `歌手2026 第1期` or `某剧 第10集`.

## Movie Main-Feature Selection

When expansion returns movie-like data or a collection with only one usable play item, the provider should choose the best main feature in this order:

1. first item with `isIntact == "1"`
2. first item where `isnew != "2"`
3. first list item

This mirrors the JavaScript reference closely enough without carrying over unrelated cache behavior.

## URL And Identifier Resolution

`resolve(page_url)` should accept canonical MGTV play URLs such as:

- `https://www.mgtv.com/b/{cid}/{vid}.html`

The provider should parse:

- `cid` from the second-to-last path segment
- `vid` from the last path segment without `.html`

If the URL shape is invalid, raise `DanmakuResolveError`.

## Danmaku Segment Resolution

The provider should first call:

- `https://pcweb.api.mgtv.com/video/info?cid={cid}&vid={vid}`

to retrieve the video duration.

Then it should try the newer control endpoint:

- `https://galaxy.bz.mgtv.com/getctlbarrage?...&vid={vid}&cid={cid}`

If the response contains both:

- `data.cdn_list`
- `data.cdn_version`

the provider should build CDN segment URLs in 60-second slices:

- `https://{first_cdn}/{cdn_version}/{index}.json`

where `index` starts at `0` and continues until the video duration is covered.

### Fallback Segment Strategy

If `getctlbarrage` fails, returns invalid JSON, or lacks the required CDN fields, the provider should fall back to:

- `https://galaxy.bz.mgtv.com/rdbarrage?vid={vid}&cid={cid}&time={offset_ms}`

using 60-second offsets in milliseconds from `0` to the end of the video.

This fallback is required because the current stub should become broadly usable, not only functional for pages where the newer API succeeds.

## Comment Parsing

Each fetched segment JSON should read `data.items` and convert every item with non-empty `content` into a `DanmakuRecord`.

Field mapping should be:

- `time_offset`: `item.time / 1000`
- `content`: stripped `item.content`
- `pos`:
  - `1` by default for scrolling
  - `5` when `v2_position == 1` for top
  - `4` when `v2_position == 2` for bottom
- `color`:
  - use averaged `v2_color.color_left` and `v2_color.color_right` when present and valid
  - fall back to white `16777215` when color is absent or invalid

The provider should ignore malformed items rather than failing the entire resolve call.

## Color Conversion

MGTV exposes colors as strings like `rgb(255,0,0)` or related RGB-compatible values. The provider should implement a small internal helper that:

- parses a supported RGB triplet
- converts it to the decimal integer string expected by `DanmakuRecord.color`
- returns `16777215` when parsing fails

When both left and right colors exist and are valid, use their integer average. When only one side is valid, use that side.

## Error Handling

Search should raise `DanmakuSearchError` when MGTV returns a payload shape the provider cannot parse at all.

Search should return an empty list when:

- the HTTP request succeeds but yields no usable media entries
- collection expansion for a specific candidate fails after the initial search payload has been parsed

Resolve should raise `DanmakuResolveError` when:

- the page URL cannot be parsed into `cid` and `vid`
- the initial video-info request fails or returns no usable duration

Resolve should return an empty list when:

- segment metadata resolves but every segment request is empty or malformed

This matches the current service contract, where an empty provider result becomes `DanmakuEmptyResultError` at the service layer.

## Request Behavior

All MGTV requests should use browser-like headers with:

- `User-Agent`
- `Referer: https://www.mgtv.com/`
- JSON `Accept` where appropriate

The provider may fetch segments sequentially in the first version. There is no need to introduce async or thread pools inside the provider for this change.

## Test Plan

Add unit coverage for:

- search payload parsing for valid `imgo` media results
- search filtering that drops non-`imgo` or invalid URL results
- collection expansion that turns one series result into episode candidates
- movie main-feature selection
- `supports()` accepting `mgtv.com` URLs
- `resolve()` using the CDN control endpoint when available
- `resolve()` falling back to `rdbarrage` when CDN metadata is missing
- position and color conversion into unified records
- invalid MGTV play URLs raising `DanmakuResolveError`

All tests should mock HTTP calls and avoid live network traffic.

## Open Decisions

The first version will use a compact blacklist for obvious non-main-content MGTV episodes instead of porting the full JavaScript blacklist verbatim. If real titles later show false positives or false negatives, the blacklist can be tuned in the provider without changing the service interface.
