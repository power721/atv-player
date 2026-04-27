# Danmaku Search And Resolution Design

## Summary

Add an application-internal Python danmaku service that can search candidate video pages from a normalized title and then resolve a selected candidate page into XML danmaku records. The first version should provide the shared service layer only. It should not add HTTP routes and should not integrate danmaku display into the player.

## Goals

- Add a Python danmaku service module under `src/atv_player/` with stable public entry points for search and resolution.
- Normalize titles and optionally constrain provider selection from `reg_src`.
- Search Tencent Video and Youku for danmaku candidates and return normalized search results.
- Resolve Tencent Video and Youku candidate page URLs into unified danmaku records.
- Build final XML output in the expected `<i><d ... /></i>` format.
- Keep the architecture ready for later providers such as iQIYI and MGTV without over-designing the first version.
- Cover the service with unit tests using mocked HTTP responses rather than live network calls.

## Non-Goals

- Adding local HTTP endpoints such as `GET /danmuku?...`.
- Rendering danmaku in the player window or integrating with mpv subtitle loading.
- Implementing iQIYI `brotli + protobuf` parsing in this change.
- Implementing MGTV signed request handling in this change.
- Supporting bilibili, RRMJ, XiaWen, MDD, HXQuan, or other providers in this change.
- Adding on-disk caches, persistence, retry queues, or background refresh.
- Building a sophisticated episode-guessing system beyond light title normalization and matching.

## Scope

Primary implementation should live in:

- new modules under `src/atv_player/danmaku/`

No API route or `ApiClient` change is required for this design. Provider modules should rely on injected HTTP callables or local `httpx` usage inside the danmaku package.

Primary verification should live in:

- new tests under `tests/` for danmaku utilities, service dispatch, and provider parsing

## Public Interface

The danmaku service should expose three public Python entry points:

- `search_danmu(name: str, reg_src: str = "") -> list[DanmakuSearchItem]`
- `resolve_danmu(page_url: str) -> str`
- `build_xml(records: Sequence[DanmakuRecord]) -> str`

Suggested shared models:

- `DanmakuSearchItem`
  - `provider: str`
  - `name: str`
  - `url: str`
  - `ratio: float = 0.0`
  - `simi: float = 0.0`
- `DanmakuRecord`
  - `time_offset: float`
  - `pos: int`
  - `color: str`
  - `content: str`

`resolve_danmu()` should return the final XML string directly so an eventual HTTP wrapper can reuse the same behavior without changing the service contract.

## Module Layout

Create a focused danmaku package under `src/atv_player/danmaku/`:

- `service.py`
  - service entry points
  - provider selection
  - search aggregation
  - XML building
- `models.py`
  - shared dataclasses
- `utils.py`
  - title normalization
  - domain matching
  - lightweight similarity and filtering helpers
  - XML escaping
- `providers/base.py`
  - common provider protocol or base class
- `providers/tencent.py`
  - Tencent Video search and danmaku resolution
- `providers/youku.py`
  - Youku search and danmaku resolution
- `providers/iqiyi.py`
  - first-version skeleton that raises a clear not-implemented error
- `providers/mgtv.py`
  - first-version skeleton that raises a clear not-implemented error

This keeps the orchestration logic stable while later provider work stays isolated to provider modules.

## Search Flow

`search_danmu(name, reg_src)` should follow a fixed sequence:

1. Normalize the input title.
2. Select one provider from `reg_src` when the source domain maps cleanly to a supported platform.
3. Otherwise search providers in a fixed default order.
4. Merge each provider's normalized candidate results.
5. Drop obviously mismatched titles with lightweight filtering.
6. Return results sorted by best match first.

The first-version default provider order should be:

1. `tencent`
2. `youku`
3. `iqiyi`
4. `mgtv`

Only Tencent and Youku should currently yield real results. iQIYI and MGTV should remain present in the order so the service shape matches the intended architecture, but their implementations should fail explicitly when invoked.

## Title Normalization And Matching

The service should keep normalization intentionally light and deterministic:

- trim whitespace
- collapse repeated whitespace
- strip common bracketed suffix noise
- strip common quality or source suffixes when they appear as non-title noise
- normalize full-width and half-width punctuation where useful for matching

Matching should avoid extra dependencies. The first version should use:

- normalized containment checks
- a simplified title form with punctuation and common episode markers removed
- `difflib.SequenceMatcher` for a `ratio` score
- a lightweight `simi` score that can initially mirror `ratio` or bias toward episode-like matches

This is sufficient to rank likely results without introducing a heavy fuzzy-match framework.

## Provider Selection

`reg_src` is a provider preference hint, not a required field.

Map source domains as follows:

- `qq.com` and `v.qq.com` -> `tencent`
- `youku.com` and `v.youku.com` -> `youku`
- `iqiyi.com` -> `iqiyi`
- `mgtv.com` -> `mgtv`

Behavior:

- if `reg_src` maps to a known provider, search only that provider
- if `reg_src` is empty, search the default provider order
- if `reg_src` is non-empty but not recognized, fall back to the default provider order rather than failing

## Resolution Flow

`resolve_danmu(page_url)` should follow a fixed sequence:

1. detect provider from the candidate page URL
2. dispatch to the provider-specific `resolve(page_url)` implementation
3. receive a unified `list[DanmakuRecord]`
4. call `build_xml(records)` and return the XML string

The service layer should not contain site-specific parsing details beyond provider dispatch.

## XML Output

`build_xml(records)` should always emit:

- XML declaration with UTF-8 encoding
- one root `<i>` element
- one `<d>` element per danmaku record

Each danmaku item should use:

- `time_offset` as seconds
- `pos` from the record
- fixed font size field `25`
- `color` as the serialized color value expected by the downstream consumer
- XML-escaped text content

The target shape is:

```xml
<?xml version="1.0" encoding="UTF-8"?><i><d p="{time_offset},{pos},25,{color}">...</d></i>
```

The first version may default `pos` to `1` when a provider does not expose richer positioning information.

## Tencent Provider

The Tencent provider should implement both search and resolution.

### Search

The first version should:

- call the Tencent search endpoint derived from the reference behavior
- parse candidate titles and candidate page URLs
- return normal Tencent detail page URLs in normalized candidate results
- populate normalized `DanmakuSearchItem` values

### Resolution

The first version should:

- accept a Tencent candidate page URL
- fetch the page and extract `videoId` from page content or page-data responses
- use page-data fallbacks when direct HTML extraction is insufficient
- fetch segmented danmaku from the Tencent barrage endpoint
- merge all returned segments into unified records
- de-duplicate records with a stable key such as `(time_offset, content)`

The provider does not need to reproduce every branch present in the reference binary. It only needs to support the common page-to-`videoId` and `videoId`-to-segmented-danmaku path reliably enough for the first version.

## Youku Provider

The Youku provider should implement both search and resolution.

### Search

The first version should:

- query Youku search results using the reference search endpoints
- parse candidate titles and candidate page URLs
- extract whichever of `showId` and `vid` are present and sufficient to build or resolve a normal Youku page URL
- prefer returning normal Youku page URLs

### Resolution

The first version should:

- accept a Youku candidate page URL
- fetch the page and extract `vid`
- fetch danmaku pages or segments through the Youku danmaku endpoint
- obtain `dataVersion` from the page when the endpoint requires it
- convert API response items into unified records

As with Tencent, the first version should favor the common successful path rather than trying to duplicate every edge branch visible in the reference binary.

## iQIYI And MGTV Skeletons

iQIYI and MGTV provider modules should exist in the package now, but they should raise explicit not-implemented errors for both search and resolution.

This is important for two reasons:

- the package structure should already reflect the intended provider expansion
- unsupported behavior should fail clearly instead of silently returning empty data

The exception message should make the missing capability obvious, for example that iQIYI requires `brotli + protobuf` and MGTV requires signed requests that are not implemented in the first version.

## Error Handling

Do not silently swallow provider failures into empty XML.

Add a small danmaku-specific exception hierarchy:

- `DanmakuError`
- `ProviderNotSupportedError`
- `DanmakuSearchError`
- `DanmakuResolveError`
- `DanmakuEmptyResultError`

Expected behavior:

- unsupported provider URL -> `ProviderNotSupportedError`
- search request or parsing failure -> `DanmakuSearchError`
- provider-specific page or danmaku parsing failure -> `DanmakuResolveError`
- successful resolution path with zero danmaku records -> `DanmakuEmptyResultError`

This keeps service consumers able to distinguish real empty results from implementation failures.

## Testing

Tests should be fully local and mock-driven.

### Utility Tests

Cover:

- title normalization
- provider matching from `reg_src`
- title filtering behavior
- XML building and escaping

### Service Tests

Cover:

- default provider ordering
- single-provider restriction when `reg_src` matches a known domain
- fallback to default order when `reg_src` is unknown
- provider dispatch by resolved page URL
- sorting of aggregated search results

### Provider Tests

Use fake HTTP responses to cover:

- Tencent search result parsing
- Tencent page `videoId` extraction
- Tencent segmented danmaku conversion and de-duplication
- Youku search result parsing
- Youku page `vid` extraction
- Youku danmaku response conversion
- explicit failure paths for unsupported or malformed responses

Live integration tests should not be added in this change.

## Result

After this change, the app should have a dedicated Python danmaku service that can:

- search Tencent and Youku candidate video pages from a title
- honor `reg_src` as a provider preference hint
- resolve a selected Tencent or Youku page URL into standardized XML danmaku output

The implementation should remain intentionally scoped: no HTTP wrapper, no player integration, and no first-version support for iQIYI or MGTV resolution internals.
