# Bilibili Danmaku Provider Design

## Summary

Extend the internal Python danmaku service with a first-party `bilibili` provider. The provider should search Bilibili by title, carry forward structured candidate metadata such as `cid` and `bvid`, resolve the best available `cid` through structured APIs before falling back to HTML parsing, download the XML danmaku payload from `comment.bilibili.com`, parse it into unified danmaku records, and return the final XML through the existing service contract.

This design intentionally preserves the current service shape:

- `search_danmu(name, reg_src)` still returns normalized candidate items
- `resolve_danmu(page_url)` still returns XML
- provider implementations still resolve into `list[DanmakuRecord]`
- `build_xml()` remains the single final XML serializer used by the service layer

## Goals

- Add a production `bilibili` danmaku provider under `src/atv_player/danmaku/providers/`.
- Support Bilibili candidate search for:
  - `media_bangumi`
  - `media_ft`
  - `video`
- Prioritize `media_bangumi` and `media_ft` search results ahead of `video` results.
- Extend `DanmakuSearchItem` so search can preserve structured candidate metadata:
  - `cid`
  - `bvid`
  - `aid`
  - `ep_id`
  - `season_id`
  - `search_type`
- Resolve danmaku through the most stable available path:
  - candidate metadata `cid`
  - season API for `ep_id` and `season_id`
  - pagelist API for `bvid` and `aid`
  - HTML or embedded JSON fallback
- Download `https://comment.bilibili.com/{cid}.xml` and convert it into unified `DanmakuRecord` values.
- Cover the provider with deterministic unit tests using mocked HTTP responses only.

## Non-Goals

- Adding a public HTTP route for Bilibili danmaku.
- Integrating Bilibili-specific logic into the player UI.
- Replacing the existing provider protocol with an XML-returning provider interface.
- Implementing broad anti-bot bypass logic beyond the narrow search request path needed here.
- Supporting live danmaku, websocket danmaku, or historical danmaku archives beyond the XML endpoint.
- Building an advanced episode disambiguation engine beyond light normalization and title matching.

## Scope

Primary implementation should live in:

- `src/atv_player/danmaku/models.py`
- `src/atv_player/danmaku/service.py`
- `src/atv_player/danmaku/utils.py`
- `src/atv_player/danmaku/providers/base.py`
- new `src/atv_player/danmaku/providers/bilibili.py`
- `src/atv_player/danmaku/providers/__init__.py`

Primary verification should live in:

- new `tests/test_danmaku_bilibili_provider.py`
- updated `tests/test_danmaku_service.py`
- small compatibility updates in `tests/test_spider_plugin_controller.py` only if needed

## Public Interface Changes

`DanmakuSearchItem` should remain backward compatible for current callers, but gain optional provider metadata fields:

- `cid: int | None = None`
- `bvid: str = ""`
- `aid: int | None = None`
- `ep_id: int | None = None`
- `season_id: int | None = None`
- `search_type: str = ""`

Existing code that only reads `provider`, `name`, and `url` must continue to work unchanged.

`resolve_danmu(page_url)` should keep its current signature. The service continues to dispatch by URL, but the Bilibili provider may internally reuse metadata captured during search when the chosen candidate URL was produced earlier in the same process.

## Provider Ordering And Selection

Add `bilibili` to the supported provider set and domain matching rules.

The updated default provider order should be:

1. `tencent`
2. `youku`
3. `bilibili`
4. `iqiyi`
5. `mgtv`

`reg_src` should map these domains to `bilibili`:

- `bilibili.com`
- `www.bilibili.com`
- `m.bilibili.com`
- `b23.tv`

Behavior remains the same:

- recognized `reg_src` means search only that provider
- empty `reg_src` means search all providers in fixed order
- unknown `reg_src` falls back to default order

## Search Strategy

The Bilibili provider should issue three categorized search requests in this fixed order:

1. `media_bangumi`
2. `media_ft`
3. `video`

Each request uses:

- endpoint: `https://api.bilibili.com/x/web-interface/wbi/search/type`
- the normalized title as `keyword`
- a valid `search_type`
- WBI-signed query parameters

The provider should merge all parsed candidates into one result list. Candidate ranking should be:

1. by search type priority:
   - `media_bangumi`
   - `media_ft`
   - `video`
2. then by the existing title similarity scoring

This ranking is provider-internal. The service layer should still apply the existing cross-provider sorting behavior after candidate normalization.

## Search Result Parsing

The provider should parse each result type into one normalized `DanmakuSearchItem`.

For `media_bangumi` and `media_ft`, capture whenever available:

- display title
- canonical detail URL
- `season_id`
- `ep_id`
- `bvid`
- `cid`
- `search_type`

For `video`, capture whenever available:

- display title
- canonical detail URL or `arcurl`
- `bvid`
- `aid`
- `cid` if directly present
- `search_type`

If a result supplies multiple identifiers, keep them all. The provider should prefer preserving identifiers over guessing from URL strings later.

The provider should normalize result titles before similarity scoring by stripping simple HTML tags such as `<em class="keyword">`.

## Bilibili Web Client

Create a thin internal helper inside the Bilibili provider module, or a small neighboring helper class, responsible for:

- keeping a shared cookie jar for Bilibili requests
- setting browser-like headers
- retrieving any required initial fingerprint state
- generating WBI-signed parameters
- retrying the search request once with a refreshed ticket when Bilibili returns a known verification or risk-control response

This helper should stay narrow. It is not a general Bilibili SDK.

### Request Preparation

Search requests should use a consistent browser-style header set including:

- `User-Agent`
- `Referer: https://www.bilibili.com/`
- `Origin: https://www.bilibili.com`

The helper should prepare cookies through normal HTTP responses rather than hardcoded values.

### WBI Signing

The provider should derive WBI signing inputs from current Bilibili responses, not hardcode historical mixin keys.

The implementation may use the standard web flow:

- fetch the current nav or equivalent page data that exposes WBI image keys
- derive the mixin key
- sign query parameters for `x/web-interface/wbi/search/type`

The implementation must keep this logic isolated so later changes to WBI only affect one small area.

### Ticket Retry

The provider should not make ticket generation a mandatory first step.

Instead:

- run the signed search request normally
- if Bilibili returns a known risk-control or verification failure for that request
- call the ticket endpoint once to refresh the request state
- retry the same search request once

If the second attempt still fails, raise `DanmakuSearchError`.

This keeps the first version focused while still acknowledging the ticket path visible in the reference behavior.

## Cid Resolution Strategy

The provider should resolve `cid` in this exact preference order:

1. use `cid` already stored in candidate metadata
2. use `ep_id` with the season API
3. use `season_id` with the season API
4. use `bvid` or `aid` with the pagelist API
5. parse the detail page HTML or embedded JSON as a final fallback

The goal is to prefer structured API responses and leave HTML scraping as the last resort.

### Season API

For bangumi and film candidates, call:

- `https://api.bilibili.com/pgc/view/web/season?ep_id=...`
- or `https://api.bilibili.com/pgc/view/web/season?season_id=...`

Resolution behavior:

- if the candidate has `ep_id`, select the episode with the exact same `ep_id`
- if exact `ep_id` matching is unavailable, use the normalized title or episode-like label as a lightweight fallback
- if the candidate already has a `bvid` or `cid` from the season payload, preserve them for future reuse

### Pagelist API

For normal video candidates, call:

- `https://api.bilibili.com/x/player/pagelist?bvid=...`
- or `https://api.bilibili.com/x/player/pagelist?aid=...`

Resolution behavior:

- if only one page exists, use that page's `cid`
- if multiple pages exist, try to match the normalized candidate title against the page `part`
- if no page label matches clearly, fall back to the first page's `cid`

The fallback to the first page is intentional because it is more useful than failing outright for common one-episode search hits that still expose multi-page data.

### HTML Fallback

If structured APIs cannot produce a `cid`, fetch the candidate detail page and inspect embedded JSON or page script content.

The HTML fallback may extract from:

- JSON state embedded in the page
- inline script blocks containing `cid`
- Bilibili-specific initialization structures that expose episode or page metadata

The fallback should not rely on a single fragile regular expression when a small JSON extraction path is available.

If no `cid` can be found after all resolution paths are exhausted, raise `DanmakuResolveError`.

## XML Fetch And Parsing

Once a `cid` is known, the provider should request:

- `https://comment.bilibili.com/{cid}.xml`

The provider should parse XML records into `DanmakuRecord` items instead of bypassing the shared service serializer.

Expected record mapping:

- XML `p` field first position -> `time_offset`
- XML `p` field second position -> `pos`
- XML `p` field fourth position -> `color`
- element text -> `content`

The provider may ignore XML attributes that are not currently represented in `DanmakuRecord`.

Invalid or empty XML should raise `DanmakuResolveError` unless the payload is structurally valid but contains zero danmaku items, in which case the provider should return an empty list and let the service raise `DanmakuEmptyResultError`.

## Metadata Reuse

The provider should avoid needless follow-up requests when search already discovered stable identifiers.

A simple in-memory metadata reuse strategy is sufficient:

- key by canonical candidate URL
- store the enriched `DanmakuSearchItem`
- when `resolve(page_url)` is called, look up the metadata first

This cache is process-local and opportunistic. It is not required for correctness because all resolution paths must still work when only a plain Bilibili URL is available.

## Error Handling

Expected failure behavior:

- search request failure or malformed search payload -> `DanmakuSearchError`
- search blocked twice even after ticket refresh -> `DanmakuSearchError`
- unsupported or unrecognized Bilibili page URL shape -> `DanmakuResolveError`
- missing `cid` after all resolution attempts -> `DanmakuResolveError`
- XML download failure or malformed XML -> `DanmakuResolveError`
- valid XML with zero danmaku items -> empty list, then service raises `DanmakuEmptyResultError`

The provider should not silently return empty candidate lists when the request itself failed. Empty candidate lists are only valid when Bilibili genuinely returned no matches.

## Testing

Add focused unit tests with mocked HTTP responses for:

- parsing and ordering mixed `media_bangumi`, `media_ft`, and `video` search results
- preserving candidate metadata fields on `DanmakuSearchItem`
- resolving `cid` directly from stored candidate metadata
- resolving `cid` from season API by `ep_id`
- resolving `cid` from season API by `season_id`
- resolving `cid` from pagelist by `bvid`
- falling back to first pagelist entry when no `part` match exists
- falling back to HTML extraction when APIs do not provide a `cid`
- parsing `comment.bilibili.com/{cid}.xml` into `DanmakuRecord`
- surfacing search retry failure as `DanmakuSearchError`
- surfacing missing `cid` as `DanmakuResolveError`

Update service-level tests to lock:

- default provider order now includes `bilibili`
- `reg_src` mapping for Bilibili domains
- backward compatibility of existing candidate consumers that only use `url`

## Risks And Mitigations

- Risk: Bilibili WBI signing inputs change again.
  Mitigation: isolate signing logic in one helper and cover it with payload-shape tests around the local derivation path.

- Risk: search results and detail pages expose multiple plausible identifiers that disagree.
  Mitigation: preserve all structured identifiers and apply a fixed `cid` resolution precedence instead of ad hoc branching.

- Risk: multi-page normal videos may resolve the wrong `cid` when page labels are ambiguous.
  Mitigation: prefer exact title or episode-like matching first, then use a deterministic first-page fallback.

- Risk: provider-specific XML passthrough would fragment the service contract.
  Mitigation: keep Bilibili aligned with the existing provider protocol by parsing XML back into `DanmakuRecord`.

## Implementation Notes

This feature should be implemented incrementally under TDD:

1. extend `DanmakuSearchItem` with backward-compatible metadata fields
2. add failing service tests for provider order and Bilibili domain mapping
3. add failing provider tests for search result parsing and `cid` resolution
4. implement the Bilibili provider and wire it into the default service
5. add XML parsing coverage and edge-case error tests

This keeps the highest-risk parts, especially `cid` resolution, under direct test before integration code is written.
