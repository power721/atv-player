# Spider Plugin Category Filters Design

Spider plugins may return `filters` from `homeContent()`. Each filter group describes optional `extend` values supported by a category when the app later calls `categoryContent(tid, pg, filter, extend)`. The desktop plugin page should expose those category filters in the left-side browsing UI without changing `searchContent()` behavior.

## Goals

- Surface plugin-provided category filters in the spider-plugin poster grid page.
- Keep filters collapsed by default and let users expand them from a dedicated button.
- Apply selected filter values only to the currently selected category list.
- Remember filter selections per category when the user switches away and back.
- Keep plugin search behavior unchanged and independent from category filters.

## Scope

- Shared category/filter models used by poster-grid controllers
- Spider-plugin home/category loading in `SpiderPluginController`
- Spider-plugin tab UI in `PosterGridPage`
- Controller and UI test coverage for category filters

## Non-goals

- Passing filter state into `searchContent()`
- Adding filters to non-plugin content sources unless they later opt into the shared model
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
- options missing either display text or value are skipped
- malformed `filters` payloads should not break category loading

### Spider-plugin category loading

When `PosterGridPage` requests category items, `SpiderPluginController.load_items()` behaves as follows:

- `category_id == "home"`: return cached homepage recommendation items and ignore `filters`
- normal plugin category: call `categoryContent(category_id, str(page), False, filters or {})`

This keeps the plugin protocol aligned with the existing `extend` contract while making the current filter state explicit at the page/controller boundary.

### Poster-grid filter UI

`PosterGridPage` adds a collapsible filter area tied to the currently selected category.

- When `search_enabled=True`, place a `筛选` button after the search button.
- When `search_enabled=False`, still show a standalone `筛选` button at the top of the right content column.
- The filter panel is collapsed by default.
- If the current category has no filters, hide the `筛选` button and keep the panel closed.
- Entering search mode hides the filter button and panel because search is independent from category filters.
- Clearing search restores the filter button/panel state for the currently selected category.

Expanded filter content is rendered as one labeled control per filter group. A simple combo box per group is sufficient because the plugin payload already provides discrete option values.

### Filter state and reload behavior

`PosterGridPage` keeps in-memory filter selections keyed by category id.

- Each category starts with its default option selection. In practice this is the first option returned by the plugin, which should usually be `不限`.
- When the user changes a filter value, the page updates only that category's remembered selection.
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
- `SpiderPluginController.load_items()` passes selected filter values into `categoryContent(..., extend)` for normal categories.
- `SpiderPluginController.load_items("home", ...)` ignores filters.
- `PosterGridPage` hides filter controls by default and shows the button only for categories with filters.
- Expanding a filtered category renders filter controls from the category definition.
- Changing a filter reloads page 1 with the selected filter dictionary.
- Filter selections are remembered per category when switching categories.
- Entering search mode hides category filters.
- Clearing search restores category-mode filter controls and reloads the selected category with its remembered filters.
- Poster-grid pages backed by controllers without filters still behave exactly as before.
