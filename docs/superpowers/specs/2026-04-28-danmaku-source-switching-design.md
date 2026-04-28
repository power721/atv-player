# Danmaku Source Switching Design

## Summary

Add a desktop-only danmaku source selection flow on top of the existing multi-provider danmaku search and single-provider resolution pipeline. Playback should still auto-select a default danmaku source, but users should be able to inspect grouped candidates, switch to another source manually, and retry search with a temporary per-episode query override. The application should also remember the user's successful danmaku source choice at the series level so later episodes of the same show reuse the preferred source automatically.

## Goals

- Keep the current automatic danmaku loading behavior as the default path.
- Expose grouped danmaku candidates to the player so users can switch sources manually.
- Group results by provider and show multiple candidate videos per provider.
- Remember a successful danmaku source choice per series, not per episode.
- Allow users to edit the current episode's search keyword and run a fresh danmaku search.
- Keep manual search keyword edits temporary to the current play item only.
- Preserve the current provider-specific `resolve_danmu(page_url)` contract.
- Avoid breaking playback when search or resolution fails.

## Non-Goals

- Merging danmaku records from multiple providers into one combined stream.
- Remembering manual search keywords across episodes or across sessions.
- Adding mobile-specific UI constraints or responsive behaviors.
- Replacing the current danmaku XML cache with a new cache system.
- Changing subtitle rendering, ASS conversion, or mpv danmaku loading behavior.
- Reworking provider-specific search and resolution implementations in this design.

## Scope

Primary implementation should live in:

- `src/atv_player/danmaku/models.py`
- `src/atv_player/danmaku/service.py`
- `src/atv_player/plugins/controller.py`
- `src/atv_player/models.py`
- `src/atv_player/ui/player_window.py`
- new danmaku preference persistence module or storage helpers under `src/atv_player/danmaku/` or `src/atv_player/storage.py`

Primary verification should live in:

- `tests/test_danmaku_service.py`
- `tests/test_spider_plugin_controller.py`
- `tests/test_player_window_ui.py`
- new storage tests if a dedicated persistence helper is added

## Core Product Decision

The user experience should be:

1. auto-search candidates when playback starts
2. auto-select a default source
3. auto-load danmaku from that source
4. let the user open a danmaku source panel to inspect grouped candidates
5. let the user switch to another candidate and reload danmaku
6. remember the successful manual choice for later episodes of the same series

This is intentionally not a pure manual workflow. Automatic selection remains the baseline, and manual switching acts as correction when the default source is poor.

## Search And Resolution Model

Keep the current separation:

- search returns candidate source metadata
- resolution accepts one selected page URL and returns XML

`resolve_danmu(page_url)` should remain unchanged.

`search_danmu(name, reg_src)` should also remain available for existing call sites and tests.

Add a new higher-level search entry point that is UI-friendly:

- `search_danmu_sources(name: str, reg_src: str = "", preferred_provider: str = "", preferred_page_url: str = "") -> DanmakuSourceSearchResult`

This new method should build on the same provider search behavior as `search_danmu()`, but return grouped results plus a computed default selection.

## Danmaku Source Models

Add dedicated models for grouped source selection:

- `DanmakuSourceOption`
  - `provider: str`
  - `name: str`
  - `url: str`
  - `ratio: float`
  - `simi: float`
  - `duration_seconds: int`
  - `episode_match: bool`
  - `preferred_by_history: bool = False`
  - `resolve_ready: bool = True`

- `DanmakuSourceGroup`
  - `provider: str`
  - `provider_label: str`
  - `options: list[DanmakuSourceOption]`
  - `preferred_by_history: bool = False`

- `DanmakuSourceSearchResult`
  - `groups: list[DanmakuSourceGroup]`
  - `default_option_url: str`
  - `default_provider: str`

These models should be read-only dataclasses like the existing danmaku models.

## Grouping And Ranking

Search results should be ranked in two layers.

### Cross-provider ranking

Priority should be:

1. exact historical `preferred_page_url` match for the current series
2. historical `preferred_provider`
3. provider inferred from the playback source URL or `reg_src`
4. existing global provider order

### In-provider ranking

Within each provider, sort candidates by:

1. explicit episode match
2. title similarity
3. duration proximity or long-form preference when relevant
4. historical success preference

The service should compute a default selected candidate using the same ranking output it returns to the UI.

## Series-Level Memory

Persist danmaku source preference by series, not by episode.

The stored key should be a normalized `series_key` derived from the media title without episode information. It should be deterministic and based on the same normalization family already used by danmaku matching.

Persisted value should include:

- `provider`
- `page_url`
- `title`
- `updated_at`

Recommended storage shape:

```json
{
  "series_key": {
    "provider": "tencent",
    "page_url": "https://v.qq.com/...",
    "title": "剑来 第12集",
    "updated_at": 1770000000
  }
}
```

This should not be stored as fixed fields on `AppConfig`. It is a variable-sized mapping and should live in a dedicated persistence location.

## Memory Lookup Rules

When danmaku search runs for a play item:

1. compute `series_key`
2. load the stored preference for that series, if any
3. if the stored `page_url` exists in current search results, default to it
4. otherwise, if the stored `provider` still has candidates, default to that provider's first candidate
5. otherwise, default to the global best candidate

When the user manually switches and the selected source resolves successfully:

- overwrite the stored preference for that `series_key`

When manual switching fails:

- keep the old persisted preference unchanged

## Per-Episode Temporary Search Override

The player should support editing the search keyword for the current episode and rerunning danmaku search.

This override is intentionally temporary:

- it applies only to the current `PlayItem`
- it is not saved to series-level memory
- it is not reused for later episodes
- it is not reused after reopening playback

Add per-item state:

- `danmaku_search_query: str`
- `danmaku_search_query_overridden: bool`

Behavior:

- on first load, fill `danmaku_search_query` with the auto-generated query
- if the user edits the query and clicks `重新搜索`, rerun grouped source search using the edited query
- if the user clicks `恢复默认搜索词`, restore the generated default query and rerun search

If a temporary manual query returns no candidates:

- keep the current input value visible
- show a no-results state
- keep the last successfully loaded danmaku active

## PlayItem State

Extend `PlayItem` with danmaku source-selection state:

- `danmaku_series_key: str = ""`
- `danmaku_search_query: str = ""`
- `danmaku_search_query_overridden: bool = False`
- `danmaku_candidates: list[DanmakuSourceGroup] = field(default_factory=list)`
- `selected_danmaku_url: str = ""`
- `selected_danmaku_provider: str = ""`
- `selected_danmaku_title: str = ""`
- `danmaku_error: str = ""`

Existing fields remain:

- `danmaku_xml`
- `danmaku_pending`

This makes source-selection state explicit rather than inferring everything from loaded XML alone.

## Controller Flow

`SpiderPluginController` should stop treating danmaku search as a hidden list of fallback URLs only.

Updated flow:

1. build the default search query from `media_title + episode label`
2. compute `series_key`
3. load series-level danmaku preference
4. call `search_danmu_sources(...)`
5. populate `PlayItem` candidate and selection state
6. resolve the default selected candidate
7. if resolution fails, fall back to the next candidate according to ranking rules
8. preserve grouped candidates so the UI can expose them later

The controller should still tolerate search and resolution failures without breaking media playback.

## Manual Switching Flow

From the player UI, when the user selects another danmaku source:

1. update the current item's selected source fields
2. call `resolve_danmu(selected_url)`
3. replace `danmaku_xml`
4. refresh danmaku subtitle loading in the player
5. if successful, persist series-level preference
6. if failed, keep grouped candidates and show the error without losing playback

Manual switching should not require a new playback session or a full item reload.

## Player UI

Add a desktop danmaku source panel or dialog.

Recommended layout:

- top area: current selected danmaku source status
- search area: editable query input plus `重新搜索` and `恢复默认搜索词`
- left column: provider groups with provider name and candidate count
- right column: candidates for the selected provider
- action area: `切换并加载`

Candidate rows should show enough context to disambiguate similarly named results:

- title
- episode indicator when present
- duration when present
- match score or a simple qualitative match label
- whether the result is the remembered historical source

The UI should not expose raw provider internals such as `cid`, `aid`, or other site-specific metadata.

## Error Handling

Search failure:

- do not interrupt playback
- keep danmaku unavailable for that attempt
- surface a lightweight error state in the source panel if opened

Default source resolution failure:

- auto-try the next ranked candidate
- if all candidates fail, keep playback running without danmaku

Manual source switch failure:

- keep the panel open
- show the error
- keep the previous successful danmaku active if one exists
- do not overwrite persisted series preference

Manual query returns no results:

- show empty results
- keep the edited query text
- allow retry or reset to default query

## Caching

Keep the existing danmaku XML cache behavior for successfully resolved XML.

This design does not add:

- search-result caching
- provider-response caching
- negative caching for failed manual queries

If cached XML exists for the default generated query and playback source, the controller may continue to use it for fast startup. The grouped source panel can still perform a fresh candidate search when opened or when the user requests `重新搜索`.

## Testing

Tests should cover:

- grouped search results and default selection output from `DanmakuService`
- preference hit on exact historical `page_url`
- fallback to historical provider when historical page URL is absent
- fallback to global best candidate when no historical preference matches
- controller populating `PlayItem` danmaku candidate state
- controller retaining grouped candidates after auto-resolution
- manual source switch success updating series-level preference
- manual source switch failure not updating series-level preference
- temporary per-episode search override affecting only the current `PlayItem`
- reset-to-default-query behavior
- manual query no-result behavior preserving existing danmaku XML

## Result

After this change, danmaku remains automatic by default but becomes inspectable and correctable. Users can choose a better source when multiple providers have danmaku, the application learns that preference at the series level, and a temporary per-episode search override provides a recovery path when automatic title matching is wrong.
