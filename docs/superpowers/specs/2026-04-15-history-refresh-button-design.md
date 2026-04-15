# History Refresh Button Design

## Summary

Add a refresh button to the playback history page so users can manually reload the current history page without losing their current pagination state.

The feature is intentionally narrow. It reuses the existing `HistoryPage.load_history()` behavior and does not introduce any new controller or API methods.

## Goals

- Add a visible `刷新` button to the playback history page action bar.
- Reload the current history page when the user clicks the button.
- Preserve the current `current_page` and `page_size` values during refresh.
- Reuse existing error handling and authorization behavior.

## Non-Goals

- Add new controller methods for refresh.
- Reset the current page to 1 when refreshing.
- Add loading spinners, toasts, or new status UI.
- Change delete, clear, or pagination behavior.

## Scope

Primary implementation lives in `src/atv_player/ui/history_page.py`.

Primary verification lives in `tests/test_browse_page_ui.py`.

No controller or API changes are required for the initial implementation.

## Design

### UI Placement

Add a `QPushButton("刷新")` to the existing action row in `HistoryPage`, alongside the current `删除` and `清空` buttons.

The button should live in the same left-side action cluster as the existing history actions so that manual data operations remain grouped together.

### Interaction

Clicking the refresh button should call `HistoryPage.load_history()` directly.

Because `load_history()` already reads `self.current_page` and `self.page_size`, this automatically preserves the current pagination state instead of forcing the user back to the first page.

The button should not add special-case logic for selection retention. The page should simply redraw from the current backend result, matching the behavior of delete and clear reloads.

### Error Handling

Refresh should keep the existing `load_history()` error behavior:

- `UnauthorizedError` still emits `unauthorized`
- `ApiError` still returns silently without adding new UI

This keeps refresh behavior aligned with initial page load and pagination clicks.

## Testing Strategy

Add focused tests in `tests/test_browse_page_ui.py` for:

- the history page exposing a refresh button in the action area
- clicking refresh calling the controller again with the existing `current_page` and `page_size`

No controller test changes are needed because refresh does not change the history controller API.

## Implementation Order

1. Add failing history-page UI tests for refresh button presence and click behavior.
2. Add the refresh button to `HistoryPage` and connect it to `load_history()`.
3. Run the focused history-page UI tests to confirm refresh preserves pagination state.
