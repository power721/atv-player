# Telegram Search Box Design

## Summary

Add a page-local search box to the `电报影视` tab. The search box should call `/tg-search/{token}?web=true&wd=<keyword>`, replace the current category results with poster-card search results, and return to category browsing when the search is cleared.

This should remain a narrow extension of the existing poster-grid page. The player-opening behavior stays unchanged: clicking a Telegram card still opens the player directly through the Telegram detail flow.

## Goals

- Add a search input to the `电报影视` tab only.
- Call the Telegram search endpoint with `web=true&wd=<keyword>`.
- Replace the currently shown category cards with search-result cards while search mode is active.
- Restore category browsing when the search is cleared.
- Keep card click behavior unchanged: Telegram cards still open the player directly.

## Non-Goals

- Adding a global app search box.
- Changing the `豆瓣电影` tab UI or behavior.
- Changing Telegram detail resolution or player request building.
- Adding a separate search-results page or dialog.

## Scope

Primary implementation lives in:

- `src/atv_player/api.py`
- `src/atv_player/controllers/telegram_search_controller.py`
- `src/atv_player/ui/douban_page.py`
- `src/atv_player/ui/main_window.py`

Primary verification lives in:

- `tests/test_api_client.py`
- `tests/test_telegram_search_controller.py`
- `tests/test_douban_page_ui.py`
- `tests/test_app.py`

## Design

### Search Availability

Only the `电报影视` page should show the new search UI. The `豆瓣电影` page should remain category-only.

The existing poster-grid page already supports configurable card click behavior. Extend that same page with an optional search capability flag instead of creating a separate Telegram-only page.

### Search Mode vs Category Mode

The page should support two result modes:

- category mode
- search mode

Category mode is the current behavior:

- the selected category drives the card list
- paging loads the next/previous page for that category

Search mode activates when the user submits a non-empty keyword:

- the page calls the Telegram search endpoint
- the returned items replace the currently displayed category cards
- paging applies to the search result set rather than the selected category

Clearing the search box exits search mode and restores category mode:

- the current category selection remains intact
- the page reloads the selected category from page 1

### UI Layout

The search controls should live above the poster grid inside the right-hand content area, not above the left category list.

Required controls:

- one text input for the keyword
- one search button
- one clear button

`豆瓣电影` should not render these controls. `电报影视` should render them in a way that does not disturb the current centered layout or poster-card spacing.

### Controller Contract

The Telegram controller should expose a search method that mirrors the current category-loading shape:

- input: keyword and page
- output: `tuple[list[VodItem], int]`

That keeps the page logic simple because category browsing and search browsing can share the same rendering and pagination path.

The search response should map into the same `VodItem` shape already used by Telegram categories. No player-specific logic should move into the page.

### Pagination

Pagination remains active in both modes.

In category mode:

- `上一页` and `下一页` operate on the selected category

In search mode:

- `上一页` and `下一页` operate on the current search keyword

The page label should continue to reflect the current mode’s total item count. Clearing search resets the restored category view to page 1.

### Error Handling

Search requests should reuse the existing async loading and status label patterns:

- searching state should update the status text
- API errors should appear in the status label
- unauthorized responses should emit the existing `unauthorized` signal
- stale async responses should still be ignored

The page should not mix category and search responses if the user switches state quickly.

## Testing Strategy

Add focused tests in `tests/test_api_client.py` for:

- Telegram search requests using `/tg-search/{token}?web=true&wd=<keyword>`

Add focused tests in `tests/test_telegram_search_controller.py` for:

- Telegram search payload mapping into `VodItem`
- total/pagecount handling for search results

Add UI tests in `tests/test_douban_page_ui.py` for:

- a page configured with search enabled shows the search controls
- searching replaces category cards with search-result cards
- clearing search restores the selected category and reloads page 1
- clicking a search-result card still emits `open_requested` when the page is in Telegram open mode

Add app-level confidence tests in `tests/test_app.py` for:

- the Telegram page is created with search enabled
- the Douban page remains without search controls

## Implementation Order

1. Add failing API and controller tests for Telegram keyword search.
2. Add failing UI tests for Telegram search mode and clearing behavior.
3. Implement the Telegram search API/controller methods.
4. Extend the poster-grid page with optional search controls and dual-mode paging.
5. Re-run the focused UI and app verification suite.
