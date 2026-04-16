# Live Home Tab Design

## Summary

Add a new `网络直播` tab immediately after `电报影视`. The tab should reuse the existing poster-grid page with a category list on the left and poster cards on the right, but it should not add keyword search because the requested API surface only includes category browsing, nested folder browsing, and detail playback.

The backend API is rooted at `/live/{token}`. Top-level categories come from `GET /live/{token}`. Nested content is requested with `t=<type_id>` and `pg=<page>`, where the `type_id` can represent either a category such as `bili` or a deeper folder identifier such as `bili-9` or `bili-9-744`. Final playback opens through `GET /live/{token}?ids=<vod_id>`.

## Goals

- Add a `网络直播` tab after `电报影视`.
- Reuse the existing `DoubanPage` poster-grid UI.
- Insert `推荐` at the front of the category list with `type_id="recommend"`.
- Support nested folder browsing inside the tab by reusing the same `t=<id>&pg=1` request shape.
- Open the player from `ids=...` detail responses for final playable items.

## Non-Goals

- Adding a keyword search box to the live tab.
- Changing the behavior of `豆瓣电影`, `电报影视`, `Emby`, or `Jellyfin`.
- Adding a new dedicated page widget for live content.
- Introducing capability-gating for the live tab.

## Scope

Primary implementation lives in:

- `src/atv_player/api.py`
- `src/atv_player/controllers/live_controller.py`
- `src/atv_player/ui/main_window.py`
- `src/atv_player/app.py`

Primary verification lives in:

- `tests/test_api_client.py`
- `tests/test_live_controller.py`
- `tests/test_app.py`

## Design

### Tab Placement

The tab order should become:

1. `豆瓣电影`
2. `电报影视`
3. `网络直播`
4. `Emby`
5. `Jellyfin`
6. `文件浏览`
7. `播放记录`

Only the new live tab is inserted. Existing tabs keep their labels and behavior.

### UI Behavior

`网络直播` should reuse `DoubanPage` in `open` mode:

- left category list
- right poster-card grid
- previous/next page controls
- no keyword search controls

Card clicks should be interpreted through `vod_tag`:

- `vod_tag == "folder"`: load the clicked item as a deeper folder in the current tab
- anything else: build a detail playback request and open the player

Folder navigation stays inside the same tab and always uses page `1` for the newly opened folder.

### API Contract

Live requests are rooted at `/live/{token}`.

Expected request shapes:

- categories: `GET /live/{token}`
- category or folder items: `GET /live/{token}?t=<type_id>&pg=<page>`
- detail: `GET /live/{token}?ids=<vod_id>`

Examples from the requested backend:

- `GET /live/Harold`
- `GET /live/Harold?t=bili&pg=1`
- `GET /live/Harold?t=bili-9&pg=1`
- `GET /live/Harold?t=bili-9-744&pg=1`
- `GET /live/Harold?ids=bili$1785607569`

### Controller Contract

The new live controller should match the same page-facing contract already used by the shared poster-grid page:

- `load_categories() -> list[DoubanCategory]`
- `load_items(category_id, page) -> tuple[list[VodItem], int]`
- `load_folder_items(vod_id) -> tuple[list[VodItem], int]`
- `build_request(vod_id) -> OpenPlayerRequest`

`load_categories()` should always prepend:

- `DoubanCategory(type_id="recommend", type_name="推荐")`

while filtering out any duplicate backend-provided entry with the same `type_id`.

### Playback Request Construction

Detail payloads should be mapped into `OpenPlayerRequest` using the same `VodItem` mapping already used elsewhere in the app.

Because the requested live API does not include a separate playback-source endpoint, the controller should extract playable URLs directly from the detail payload:

- prefer `items` if present and any item contains a non-empty `url`
- otherwise parse `vod_play_url`
- accept `title$url` segments as direct stream URLs
- if the parsed segment does not look like a direct URL, keep it as `vod_id`

This keeps the controller tolerant of both direct-stream payloads and playlist-like payloads while still opening the player with concrete URLs when available.

If the detail payload does not produce any playable item with a URL, `build_request()` should raise a user-facing error instead of opening an empty player session.

## Testing Strategy

Add focused tests in `tests/test_api_client.py` for:

- live categories request path
- live category and folder paging params
- live detail request using `ids=...`

Add focused tests in `tests/test_live_controller.py` for:

- `推荐` inserted first with `type_id="recommend"`
- folder loading reusing the same `t=<vod_id>&pg=1` API path
- detail payload mapping into an `OpenPlayerRequest` with direct stream URLs
- mixed payload parsing where `title$url` becomes a playable `PlayItem`

Add app-level tests in `tests/test_app.py` for:

- tab order including `网络直播`
- live page created without search controls
- live file-card clicks opening the player
- live folder-card clicks loading nested items in the current tab

## Implementation Order

1. Add failing API tests for `/live/{token}` endpoints.
2. Add failing controller tests for category insertion, folder loading, and detail playback mapping.
3. Implement the live API methods and the new controller.
4. Wire the `网络直播` tab into `MainWindow` and `AppCoordinator`.
5. Run focused verification and then the relevant broader test suites.
