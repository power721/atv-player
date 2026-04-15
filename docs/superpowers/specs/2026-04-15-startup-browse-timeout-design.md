# Startup Browse Timeout Handling Design

## Summary

When the app starts into the main window and the initial file-browser load times out, the application must no longer crash with a raw `httpx.ReadTimeout`. Instead, the network exception should be translated into an `ApiError`, the main window should still open, and the file-browser area should show a readable timeout message.

This change is intentionally narrow. It standardizes HTTP transport-layer failures at the API client boundary and relies on the existing page-level `ApiError` handling to present the failure in the UI.

## Goals

- Prevent startup crashes caused by `httpx.ReadTimeout` while loading the initial browse path.
- Convert transport-layer `httpx` exceptions into `ApiError` consistently inside `ApiClient`.
- Show `加载文件列表超时` in the file-browser UI when the initial folder listing times out.
- Preserve the existing `UnauthorizedError` behavior for 401 responses.

## Non-Goals

- Add retries or backoff.
- Introduce asynchronous startup for the initial browse load.
- Redesign the browse-page error presentation.
- Change login or token-fetch behavior beyond sharing the same exception-mapping strategy.

## Scope

Primary implementation lives in:

- `src/atv_player/api.py`

Primary verification lives in:

- `tests/test_api_client.py`
- `tests/test_browse_page_ui.py`
- `tests/test_app.py`

No new UI components are required.

## Root Cause

`ApiClient._request()` currently assumes every failure reaches it as an HTTP response and only translates HTTP status failures into `ApiError` or `UnauthorizedError`.

When the backend stalls, `httpx.Client.request()` raises `httpx.ReadTimeout` directly before any response exists. That exception bypasses the existing `ApiError` handling in `BrowsePage.load_path()`, escapes through `MainWindow.__init__()`, and aborts application startup.

The page layer is already designed to recover cleanly from `ApiError`. The missing piece is consistent exception translation at the API boundary.

## Design

### API Boundary

Wrap the `self._client.request(...)` call in `ApiClient._request()` and translate transport-layer exceptions before any response handling logic runs.

Behavior:

- `httpx.ReadTimeout` during a file-list request becomes `ApiError("加载文件列表超时")`
- other `httpx.TimeoutException` values become `ApiError("请求超时")`
- other `httpx.HTTPError` values become `ApiError("网络请求失败")`

The existing response-based behavior remains unchanged:

- 401 still raises `UnauthorizedError("Unauthorized")`
- non-401 error responses still use backend-provided `message` / `detail` when available

### Request Context Detection

The timeout message needs one special-case context: initial and manual folder-list loading.

Detect a file-list request by the request target passed into `_request()`:

- URL path matches `/vod/<token>`
- query params include `ac=web`
- query params include `t=...`

This is sufficient to distinguish folder listing from detail lookups, which use `ids=...` instead of `t=...`.

### UI Behavior

No new UI handling is needed in `BrowsePage`.

Once the timeout is mapped to `ApiError("加载文件列表超时")`, the existing `except ApiError as exc:` branch in `BrowsePage.load_path()` will:

- keep the page alive
- update the breadcrumb/status area with the error text
- avoid propagating the exception back to `MainWindow`

Because of that, `MainWindow` and `AppCoordinator.start()` can complete normally even when the initial browse load fails.

## Testing Strategy

Add focused tests in `tests/test_api_client.py` for:

- file-list `ReadTimeout` becoming `ApiError("加载文件列表超时")`
- non-file-list timeout becoming `ApiError("请求超时")`
- non-timeout `httpx.HTTPError` becoming `ApiError("网络请求失败")`

Add a focused UI test in `tests/test_browse_page_ui.py` for:

- `BrowsePage.load_path()` showing `加载文件列表超时` in the breadcrumb/status area when the controller raises that `ApiError`

Add a startup test in `tests/test_app.py` for:

- `MainWindow` still being constructed successfully when the initial browse load raises `ApiError("加载文件列表超时")`

## Implementation Order

1. Add failing API-client tests for timeout and network exception mapping.
2. Implement transport-layer exception translation in `ApiClient._request()`.
3. Add failing browse-page and startup tests proving the app no longer crashes on initial browse timeout.
4. Verify the existing page-level `ApiError` handling satisfies the new behavior without extra UI changes.
5. Run the focused regression suites for API, browse page, and app startup.
