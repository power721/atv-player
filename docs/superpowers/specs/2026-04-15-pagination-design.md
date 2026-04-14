# Pagination Design

## Summary

Add minimal pagination to the desktop app's browse and history views so they can navigate backend pages instead of always loading a fixed first page.

This change intentionally stays smaller than the web UI. It does not add numbered page buttons. It only adds:

- previous page
- next page
- current page label
- page size selector

## Scope

### Browse Page

The browse page will:

- request folder data with the current `page` and `size`
- show the current page state under or beside the file table
- allow moving to the previous and next page when valid
- reset to page 1 when loading a different folder path
- remember page and page size per path within the current app session

Remembering pagination per path matches the web behavior closely enough for desktop use without introducing route state.

### History Page

The history page will:

- request history data with the current `page` and `size`
- show the current page state
- allow moving to the previous and next page when valid
- allow changing page size
- refresh the current page after delete and clear operations

History pagination state is global to the history view rather than keyed by path.

## UI Approach

Use a small control row beneath each table with:

- `上一页` button
- page status label such as `第 2 / 5 页`
- `下一页` button
- page size combo box

Buttons are disabled when moving is not possible.

This avoids introducing a custom page number widget while still giving enough control for desktop browsing.

## Data Flow

### Browse Page

- Store `current_page`, `page_size`, and `total_items` on the widget.
- Store per-path pagination state in a dictionary keyed by normalized path.
- `load_path(path)` restores the remembered state for that path or defaults to page 1.
- Refresh reloads the current path using the current page state.

### History Page

- Store `current_page`, `page_size`, and `total_items` on the widget.
- `load_history()` uses the current values instead of a fixed `page=1,size=100`.
- delete and clear call `load_history()` again after the backend mutation.

## Error Handling

- If a browse or history request fails, keep the existing page state so the user can retry.
- Page navigation never goes below page 1.
- If deleting records empties the last page, step back one page before reloading if needed.

## Testing

Add UI tests for:

- browse page sends the selected page and page size to the controller
- browse page resets to page 1 when switching folders
- browse page remembers pagination per path
- history page sends the selected page and page size to the controller
- previous and next buttons enable and disable correctly
- history deletion reloads using the adjusted page state
