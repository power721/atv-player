# Emby Home Tab Design

## Summary

Add a new `Emby` tab immediately after `电报影视`. The Emby tab should behave the same way as the Telegram home tab: category browsing on the left, poster cards on the right, a page-local search box, and direct playback when a poster card is clicked.

The main difference is the API surface. Emby uses `/emby/{token}` and does not use `web=true`. For detail playback requests, Emby uses `ids=...` instead of Telegram’s `id=...`.

## Goals

- Add an `Emby` tab after `电报影视`.
- Reuse the existing poster-grid page behavior already used by Telegram.
- Support Emby category browsing, keyword search, and direct playback.
- Keep the UI and playback behavior aligned with the Telegram tab.

## Non-Goals

- Changing the `豆瓣电影` tab behavior.
- Changing the `电报影视` API contract.
- Introducing a new page type just for Emby.
- Refactoring the player flow beyond what is required to accept Emby requests.

## Scope

Primary implementation lives in:

- `src/atv_player/api.py`
- `src/atv_player/controllers/emby_controller.py`
- `src/atv_player/ui/main_window.py`
- `src/atv_player/app.py`

Primary verification lives in:

- `tests/test_api_client.py`
- `tests/test_emby_controller.py`
- `tests/test_app.py`

The existing poster-grid UI file should be reused rather than expanded with Emby-specific rendering logic.

## Design

### Tab Placement

The tab order should become:

1. `豆瓣电影`
2. `电报影视`
3. `Emby`
4. `文件浏览`
5. `播放记录`

Only the new Emby tab is inserted. Existing tab labels and their behavior should remain unchanged.

### UI Behavior

The Emby tab should use the same poster-grid page that Telegram uses today:

- left category list
- right poster-card grid
- local search box in the right content column
- previous/next page controls
- poster click opens the player directly

That means Emby should instantiate the existing shared page in `open` mode with search enabled, just like Telegram.

### API Contract

Emby requests are rooted at `/emby/{token}`.

Expected request shapes:

- categories: `GET /emby/{token}`
- category items: `GET /emby/{token}?t=<category>&pg=<page>`
- search: `GET /emby/{token}?wd=<keyword>` and later pages `GET /emby/{token}?wd=<keyword>&pg=<page>`
- detail: `GET /emby/{token}?ids=<vod_id>`

Unlike Telegram:

- no `web=true`
- no `id=...` detail parameter

### Controller Contract

The Emby controller should expose the same surface that Telegram already exposes to the shared page and main window:

- `load_categories() -> list[DoubanCategory]`
- `load_items(category_id, page) -> tuple[list[VodItem], int]`
- `search_items(keyword, page) -> tuple[list[VodItem], int]`
- `build_request(vod_id) -> OpenPlayerRequest`

That symmetry allows the shared page and `MainWindow` wiring to stay simple.

### Playback Request Construction

Emby detail responses should be translated into the same `OpenPlayerRequest` shape used by Telegram:

- detail payload maps to `VodItem`
- `vod_play_url` is parsed into a playlist
- playlist items keep `vod_id`
- unresolved play-item URLs can still be filled later through a detail resolver when required

The only protocol-level difference from Telegram is the detail request parameter: `ids` instead of `id`.

### Search Behavior

The Emby tab should inherit the same search-mode behavior already implemented for Telegram:

- entering a keyword switches the page into search mode
- results replace the currently visible category cards
- pagination applies to the search result set
- clearing search returns to the selected category at page 1

No new UI state model should be invented for Emby.

## Testing Strategy

Add focused tests in `tests/test_api_client.py` for:

- Emby categories request path
- Emby category paging request params
- Emby keyword search request params
- Emby detail request using `ids=...`

Add focused tests in `tests/test_emby_controller.py` for:

- Emby category mapping into `DoubanCategory`
- Emby search mapping into `VodItem`
- Emby detail mapping into `OpenPlayerRequest`

Add app-level tests in `tests/test_app.py` for:

- tab order including `Emby`
- Emby page created with search enabled
- Emby card clicks opening the player directly

## Implementation Order

1. Add failing API tests for Emby endpoints.
2. Add failing controller tests for Emby mapping and playback request construction.
3. Implement the Emby API methods and controller.
4. Wire the Emby tab into `MainWindow` and `AppCoordinator`.
5. Run focused app verification and then the full test suite.
