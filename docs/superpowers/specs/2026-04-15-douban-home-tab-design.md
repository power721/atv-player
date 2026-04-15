# Douban Home Tab Design

## Summary

Add a dedicated `豆瓣电影` tab as the first tab in the desktop app. This tab shows Douban-backed movie categories and poster cards similar to the web reference, but its click action stays aligned with the desktop flow: selecting a movie switches to `文件浏览`, fills the search box with the movie name, and starts the existing Telegram search.

The resulting tab order becomes:

- `豆瓣电影`
- `文件浏览`
- `播放记录`

## Goals

- expose the Douban movie list on the app home surface instead of hiding it behind file browsing
- keep Douban browsing separate from file browsing so each page owns one clear workflow
- reuse the existing browse search behavior instead of creating a second search-results implementation
- stay close to the reference interaction without copying web-only details that do not fit the desktop app

## Non-Goals

- no Douban detail dialog or inline metadata drill-down
- no direct playback from the Douban tab
- no extra filters such as year, genre, region, or sort in the first version
- no page-size selector on the Douban tab

## UI Structure

### Main Window

`MainWindow` will instantiate a new `DoubanPage` and insert it at index 0 of the existing `QTabWidget`.

The first visible tab on startup becomes `豆瓣电影`. `文件浏览` and `播放记录` keep their existing behavior and simply shift to the right.

### Douban Page

`DoubanPage` will be a dedicated widget with:

- a left category list
- a right poster grid
- a bottom pagination row
- a small status label for loading and failure states

The layout should follow the desktop patterns already used elsewhere in the app: centered content, bounded width, and straightforward Qt widgets rather than trying to mimic Element Plus exactly.

### Poster Cards

Each poster card will show:

- poster image
- movie name
- remark or rating text

Cards are clickable. Clicking a card emits the movie name for search handoff.

No secondary buttons, context menus, or detail popups are included in this scope.

## Data Model

Reuse `VodItem` for Douban list items because the backend returns the same core fields already used elsewhere:

- `vod_id`
- `vod_name`
- `vod_pic`
- `vod_remarks`

Add a new lightweight `DoubanCategory` dataclass with:

- `type_id`
- `type_name`

This keeps category parsing explicit without overloading unrelated models.

## API Design

Extend `ApiClient` with two dedicated methods:

- `list_douban_categories()`
- `list_douban_items(category_id: str, page: int, size: int = 35)`

Both methods call `/tg-db/{token}`:

- category request: no `t` parameter, read `class` from the payload
- item request: `ac=web`, `t=<category>`, `pg=<page>`, `size=35`

The controller layer should not construct raw HTTP requests itself.

## Controller Design

Add a new `DoubanController` responsible only for Douban tab data mapping:

- load category list
- load poster items for a category and page

`BrowseController` remains focused on:

- folder listing
- Telegram search
- search-result path resolution
- player request construction

This separation avoids turning `BrowseController` into a mixed “everything media-related” controller.

## Page Interaction Flow

### Initial Load

When `DoubanPage` is first shown or initialized:

1. load categories asynchronously
2. if categories exist, select the first category automatically
3. load page 1 of that category

If category loading fails, show a status message in the page and keep the page interactive for retry.

### Category Selection

When the user selects a category:

1. store the selected category id
2. reset the current page to 1
3. load poster items for that category asynchronously

### Pagination

The Douban tab uses a fixed page size of `35`, matching the reference behavior closely enough for the desktop version.

The pagination row will provide:

- `上一页`
- page label such as `第 1 / 8 页`
- `下一页`

No numbered pager is required.

### Search Handoff

When the user clicks a Douban poster card:

1. `DoubanPage` emits `search_requested(movie_name)`
2. `MainWindow` switches to the `文件浏览` tab
3. `MainWindow` calls a new public method on `BrowsePage`, such as `search_keyword(movie_name)`
4. `BrowsePage` updates the search field and runs its existing async search flow

This keeps all browse-search state changes inside `BrowsePage` and avoids duplicate search code in the new page.

## Async and State Handling

`DoubanPage` should follow the same threading pattern already used by `BrowsePage`:

- run network calls on a background thread
- return results through Qt signals
- ignore stale responses with a request id guard

Store page state on the widget:

- `selected_category_id`
- `current_page`
- `page_size`
- `total_items`
- current category list
- current poster items

The first version does not need to persist Douban tab state to `AppConfig`.

## Error Handling

- category-load failure: keep the page visible and show a readable status message
- item-load failure: keep the current category selection and preserve the last successful poster grid while updating the status label
- unauthorized response: emit `unauthorized` so the main window can reuse the existing logout flow
- empty category result: show a neutral empty-state message and disable pagination
- empty item result: show an empty-state message in the content area rather than leaving ambiguous blank space

## Image Loading

Poster cards should render real poster images in the first implementation.

When loading Douban poster URLs, reuse the same Douban image request convention already present in the player window, including the Douban referer handling required for `doubanio.com` images. The exact helper can be shared or extracted if that reduces duplication cleanly.

If an image fails to load, the card should still show the title and remark with a simple placeholder area instead of collapsing.

## Testing

Add tests for the new behavior at three levels.

### Controller Tests

- category payload maps to `DoubanCategory`
- item payload maps to `VodItem`
- item loading sends the expected `category`, `page`, and fixed `size=35`

### Page Tests

- `DoubanPage` shows the expected high-level layout pieces
- selecting a category triggers page reset and item load
- pagination buttons enable and disable correctly
- clicking a poster emits the expected movie name
- stale async responses do not overwrite newer results

### Main Window and Browse Integration

- main window tab order becomes `豆瓣电影 / 文件浏览 / 播放记录`
- receiving a Douban search signal switches to `文件浏览`
- the browse page public search entry fills the keyword box and starts search

## Implementation Notes

- keep `DoubanPage` in its own module instead of growing `browse_page.py`
- keep `BrowsePage.search()` as the underlying implementation and add a thin public wrapper for external callers
- prefer minimal reusable helpers over speculative abstraction
- do not refactor unrelated browse, player, or history code while adding the tab
