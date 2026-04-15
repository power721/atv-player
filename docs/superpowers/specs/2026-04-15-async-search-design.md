# Async Search Design

## Summary

Move Telegram search in the desktop app off the UI thread for both search entry points:

- the embedded search panel on the browse page
- the standalone search page

Keep the change small. The controller and API surface stay synchronous. The UI starts a background worker and applies results back on the Qt main thread.

This change also keeps the earlier search behavior request:

- empty strings are allowed and sent to the backend
- searching shows a visible loading state

## Scope

### Browse Page

- Clicking `搜索` starts a background search instead of blocking the window.
- The search panel becomes visible while loading so the user can see status.
- The status label shows `搜索中...` while the request is running.
- Empty keywords are allowed.
- Only the latest search result should update the UI if the user starts another search before the first one returns.

### Search Page

- Clicking `搜索` starts a background search instead of blocking the window.
- The status field shows `搜索中...` while the request is running.
- Empty keywords are allowed.
- Only the latest search result should update the UI if multiple searches overlap.

## Architecture

Reuse the existing lightweight threading pattern already present in the player window:

- start a `threading.Thread` for the blocking controller call
- emit a Qt signal with the result, error, or unauthorized state
- handle all widget updates on the main thread

Each page owns its own request counter. Starting a search increments the counter and captures the current request id. Completion signals carry that id back to the page. If an older request finishes after a newer one has started, the page ignores it.

## UI State

While a search is running:

- disable the keyword input
- disable the search button
- disable the drive filter
- disable the clear button

When the request finishes, restore those controls.

Browse page behavior for empty results stays aligned with today:

- show the panel while loading or when there is a message to display
- hide the panel when the completed result set is empty and there is no error

Standalone search page keeps its visible table and status field, but updates them only after the worker finishes.

## Error Handling

- `UnauthorizedError` continues to emit the existing `unauthorized` signal.
- `ApiError` shows the backend message in the existing status widget.
- Stale completion signals are ignored using the request id.
- UI reset runs in the main thread for both success and failure paths.

## Testing

Add UI coverage for both pages:

- empty keyword searches still call the controller
- search enters a loading state before the worker completes
- controls are disabled while loading and re-enabled after completion
- successful completion populates results on the main thread
- stale completions from older requests do not overwrite the latest search
