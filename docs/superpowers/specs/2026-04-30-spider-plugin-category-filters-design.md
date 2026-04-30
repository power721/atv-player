# Spider Plugin Category Filters Design

Spider plugins may return `filters` from `homeContent()`. Each filter group describes optional `extend` values supported by a category when the app later calls `categoryContent(tid, pg, filter, extend)`. The poster-grid pages should expose those category filters in the left-side browsing UI without changing `searchContent()` behavior, and API-backed sources that already expose the same filter model should reuse the same UI.

## Goals

- Surface plugin-provided category filters in the spider-plugin poster grid page.
- Keep filters collapsed by default and let users expand them from a dedicated button.
- Apply selected filter values only to the currently selected category list.
- Remember filter selections per category when the user switches away and back.
- Keep plugin search behavior unchanged and independent from category filters.

## Scope

- Shared category/filter models used by poster-grid controllers
- Spider-plugin home/category loading in `SpiderPluginController`
- Poster-grid filter support in Douban, Emby, and Jellyfin controllers
- Poster-grid tab UI in `PosterGridPage`
- Controller and UI test coverage for category filters

## Non-goals

- Passing filter state into `searchContent()`
- Persisting filter selections across app restarts

## Design

### Shared filter models

`DoubanCategory` becomes the shared category shape for poster-grid pages and gains an optional `filters` field.

Add small typed models for category filters instead of passing raw plugin dictionaries through the UI:

- `CategoryFilterOption`
  - `name`: user-visible label mapped from plugin field `n`
  - `value`: request value mapped from plugin field `v`
- `CategoryFilter`
  - `key`: extend key such as `sc` or `status`
  - `name`: user-visible group name
  - `options`: available choices for that group

Controllers that do not support filters can continue returning categories with an empty filter list.

Sources that already provide top-level `filters` payloads, such as Douban, Emby, and Jellyfin, map those payloads into the same category model so the page can stay source-agnostic.

### Controller interface

Poster-grid controllers standardize on:

- `load_categories() -> list[DoubanCategory]`
- `load_items(category_id: str, page: int, filters: dict[str, str] | None = None) -> tuple[list[VodItem], int]`

Existing non-plugin controllers accept the new `filters` argument and ignore it.

`search_items(keyword, page)` remains unchanged. Search results are not filtered by category filter state.

### Spider-plugin filter mapping

`SpiderPluginController._ensure_home_loaded()` parses both `class` and `filters` from `homeContent(False)`.

- `class` still defines visible categories.
- `filters` is expected to be a mapping keyed by category `type_id`.
- Each category receives only its own parsed filter groups.
- The synthetic `home` category inserted for homepage recommendations does not expose filters.

Filter parsing is tolerant:

- groups missing `key` are ignored
- groups missing `name` fall back to an empty label and are ignored if unusable
- options missing display text are skipped
- empty-string option values are preserved so plugins can explicitly define a default choice like `全部`
- malformed `filters` payloads should not break category loading

### Spider-plugin category loading

When `PosterGridPage` requests category items, `SpiderPluginController.load_items()` behaves as follows:

- `category_id == "home"`: return cached homepage recommendation items and ignore `filters`
- normal plugin category: call `categoryContent(category_id, str(page), False, filters or {})`

This keeps the plugin protocol aligned with the existing `extend` contract while making the current filter state explicit at the page/controller boundary.

### Poster-grid filter UI

`PosterGridPage` adds a collapsible filter area tied to the currently selected category.

- When `search_enabled=True`, place the search controls in the order `搜索`, `清空`, `筛选`.
- When `search_enabled=False`, still show a standalone `筛选` button at the top of the right content column.
- The filter panel is collapsed by default.
- If the current category has no filters, hide the `筛选` button and keep the panel closed.
- Entering search mode hides the filter button and panel because search is independent from category filters.
- Clearing search restores the filter button/panel state for the currently selected category.
- When the keyword box is empty, disable both `搜索` and `清空`.

Expanded filter content is rendered as one labeled single-select button group per filter group.

- Each group renders as a row of checkable tag-like buttons that wrap across lines when needed.
- Only one option per group may be selected at a time.
- Clicking an empty-value option such as `全部` clears that group's active filter.
- If a group does not provide an explicit empty-value option, the page prepends a synthetic `默认` option with value `""`.

### Poster-grid filter button styling

`PosterGridPage` styles filter-option buttons locally instead of introducing a new shared theme layer.

- Apply styling only to category filter option buttons, not to unrelated action buttons such as `搜索`, `清空`, `筛选`, or pagination.
- Base the colors on the existing light theme tokens:
  - default background: `#ffffff`
  - hover background: `#e8e8e8`
  - border: `#d0d0d0`
  - primary text: `#1a1a1a`
  - secondary text for future disabled styling: `#666666`
  - selected accent: `#0066cc`
  - selected hover accent: `#0080ff`
- Use the restrained selected style:
  - unselected: white background, gray border, dark text
  - hovered: light-gray background
  - selected: white background, blue border, blue text
  - selected and hovered: brighter blue border and text
- Keep the button silhouette compact and tag-like so the filters remain visually lighter than poster cards.

### Poster-grid cursor behavior

All clickable buttons in `PosterGridPage` use `setCursor(Qt.CursorShape.PointingHandCursor)`.

- This includes filter option buttons, `搜索`, `清空`, `筛选`, pagination buttons, breadcrumb buttons, and poster card buttons.
- Non-interactive widgets keep their default cursor so the page does not imply clickability where none exists.

### Filter state and reload behavior

`PosterGridPage` keeps in-memory filter selections keyed by category id.

- Each category starts with no active filter values, represented by an empty state dictionary.
- When the user changes a filter value, the page updates only that category's remembered selection.
- Remembered filter state stores only non-empty values.
- Any filter change resets `current_page` to `1` and reloads the current category.
- Changing categories restores the remembered filter values for that category before loading items.
- Search mode does not mutate remembered category filter state.

The page passes the selected category's current filter dictionary into `controller.load_items(...)` whenever category cards are loaded.

## Failure handling

- If a category exposes malformed filters, ignore invalid groups/options and keep the page usable.
- If a category item request fails after a filter change, keep the previous cards visible and show the error in `status_label`, matching existing page behavior.
- Existing async request ids continue to guard against stale category or search responses so rapid category/filter changes cannot apply out-of-date results.

## Tests

- `SpiderPluginController` maps `homeContent().filters` onto the matching categories.
- `DoubanController`, `EmbyController`, and `JellyfinController` map top-level `filters` payloads onto categories and pass selected filter values back into their category list APIs.
- `SpiderPluginController.load_items()` passes selected filter values into `categoryContent(..., extend)` for normal categories.
- `SpiderPluginController.load_items("home", ...)` ignores filters.
- `PosterGridPage` hides filter controls by default and shows the button only for categories with filters.
- Expanding a filtered category renders button-style single-select controls from the category definition.
- Plugin-provided empty-value options are displayed directly and do not get duplicated by the synthetic default option.
- Filter option buttons expose the expected local stylesheet fragments for default, hover, and checked states.
- All clickable buttons in `PosterGridPage` expose the pointing-hand cursor.
- Changing a filter reloads page 1 with the selected filter dictionary.
- Filter selections are remembered per category when switching categories.
- Entering search mode hides category filters.
- Clearing search restores category-mode filter controls and reloads the selected category with its remembered filters.
- Search and clear buttons are disabled when the keyword box is empty.
- Poster-grid pages backed by controllers without filters still behave exactly as before.
