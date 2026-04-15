# Startup Browse Timeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent startup crashes caused by initial file-list `httpx.ReadTimeout` exceptions by translating transport failures into `ApiError` messages that the existing browse UI can render.

**Architecture:** Keep the fix at the API boundary in `ApiClient._request()` so all callers receive normalized domain errors instead of raw `httpx` transport exceptions. Prove the behavior through one red-green cycle that covers both direct API mapping and the real startup path, then add browse-page regression coverage for the existing breadcrumb error rendering.

**Tech Stack:** Python 3.14, httpx, PySide6, pytest, pytest-qt, uv

---

### Task 1: Normalize Transport Exceptions At The API Boundary

**Files:**
- Modify: `tests/test_api_client.py`
- Modify: `tests/test_app.py`
- Modify: `src/atv_player/api.py`

- [ ] **Step 1: Write the failing API and startup tests**

Add a reusable transport stub plus three API-client tests to `tests/test_api_client.py`:

```python
class RaisingTransport(httpx.BaseTransport):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise self.exc


def test_api_client_maps_file_list_read_timeout_to_localized_api_error() -> None:
    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="vod-123",
        transport=RaisingTransport(httpx.ReadTimeout("timed out")),
    )

    with pytest.raises(ApiError) as exc:
        client.list_vod("1$/电影$1", page=1, size=50)

    assert str(exc.value) == "加载文件列表超时"


def test_api_client_maps_non_file_list_timeout_to_generic_timeout_error() -> None:
    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="vod-123",
        transport=RaisingTransport(httpx.ConnectTimeout("timed out")),
    )

    with pytest.raises(ApiError) as exc:
        client.telegram_search("movie")

    assert str(exc.value) == "请求超时"


def test_api_client_maps_transport_http_error_to_network_request_failed() -> None:
    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="vod-123",
        transport=RaisingTransport(httpx.HTTPError("boom")),
    )

    with pytest.raises(ApiError) as exc:
        client.telegram_search("movie")

    assert str(exc.value) == "网络请求失败"
```

Add an integration-style startup test to `tests/test_app.py` that exercises the real startup chain through `BrowseController` and `MainWindow`:

```python
import httpx

from atv_player.api import ApiClient


class RaisingTransport(httpx.BaseTransport):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise self.exc


def test_app_coordinator_show_main_keeps_window_open_when_initial_browse_times_out(qtbot, monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.config = AppConfig(
                base_url="http://127.0.0.1:4567",
                username="alice",
                token="auth-123",
                vod_token="vod-123",
                last_path="/电影",
            )

        def load_config(self) -> AppConfig:
            return self.config

        def save_config(self, config: AppConfig) -> None:
            self.config = config

        def clear_token(self) -> None:
            self.config.token = ""
            self.config.vod_token = ""

    class TimeoutApiClient(ApiClient):
        def __init__(self, base_url: str, token: str = "", vod_token: str = "") -> None:
            super().__init__(
                base_url,
                token=token,
                vod_token=vod_token,
                transport=RaisingTransport(httpx.ReadTimeout("timed out")),
            )

    coordinator = AppCoordinator(FakeRepo())
    monkeypatch.setattr(app_module, "ApiClient", TimeoutApiClient)

    window = coordinator._show_main()
    qtbot.addWidget(window)

    assert isinstance(window, MainWindow)
    status_widget = window.browse_page.breadcrumb_layout.itemAt(0).widget()
    assert status_widget.text() == "/电影 | 加载文件列表超时"
```

- [ ] **Step 2: Run the new tests and verify they fail for the expected reason**

Run:

```bash
uv run pytest \
  tests/test_api_client.py::test_api_client_maps_file_list_read_timeout_to_localized_api_error \
  tests/test_api_client.py::test_api_client_maps_non_file_list_timeout_to_generic_timeout_error \
  tests/test_api_client.py::test_api_client_maps_transport_http_error_to_network_request_failed \
  tests/test_app.py::test_app_coordinator_show_main_keeps_window_open_when_initial_browse_times_out \
  -v
```

Expected:
- The three `tests/test_api_client.py` cases fail because raw `httpx` exceptions escape instead of being converted to `ApiError`.
- The startup test fails because `MainWindow` construction still aborts when the initial browse request hits `httpx.ReadTimeout`.

- [ ] **Step 3: Write the minimal production fix in `src/atv_player/api.py`**

Keep all response-status handling unchanged and only wrap the request call plus request-context detection:

```python
class ApiClient:
    ...
    def _is_file_list_request(self, url: str, params: Any) -> bool:
        if not url.startswith("/vod/"):
            return False
        if not isinstance(params, dict):
            return False
        return params.get("ac") == "web" and "t" in params

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.ReadTimeout as exc:
            if self._is_file_list_request(url, kwargs.get("params")):
                raise ApiError("加载文件列表超时") from exc
            raise ApiError("请求超时") from exc
        except httpx.TimeoutException as exc:
            raise ApiError("请求超时") from exc
        except httpx.HTTPError as exc:
            raise ApiError("网络请求失败") from exc

        if response.status_code == 401:
            raise UnauthorizedError("Unauthorized")
        if response.is_error:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise ApiError(payload.get("message") or payload.get("detail") or response.text)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text
```

- [ ] **Step 4: Run the focused red-green verification**

Run:

```bash
uv run pytest \
  tests/test_api_client.py::test_api_client_maps_file_list_read_timeout_to_localized_api_error \
  tests/test_api_client.py::test_api_client_maps_non_file_list_timeout_to_generic_timeout_error \
  tests/test_api_client.py::test_api_client_maps_transport_http_error_to_network_request_failed \
  tests/test_app.py::test_app_coordinator_show_main_keeps_window_open_when_initial_browse_times_out \
  -v
```

Expected:
- `4 passed`

- [ ] **Step 5: Commit the root-cause fix**

Run:

```bash
git add tests/test_api_client.py tests/test_app.py src/atv_player/api.py
git commit -m "fix: handle browse startup timeouts"
```

### Task 2: Add Browse Page Regression Coverage For Timeout Status Rendering

**Files:**
- Modify: `tests/test_browse_page_ui.py`

- [ ] **Step 1: Add a focused browse-page regression test**

Add this near the other `BrowsePage` behavior tests in `tests/test_browse_page_ui.py`:

```python
class ErroringBrowseController(FakeBrowseController):
    def load_folder(self, path: str, page: int = 1, size: int = 50):
        raise ApiError("加载文件列表超时")


def test_browse_page_shows_folder_timeout_in_breadcrumb_status(qtbot) -> None:
    page = BrowsePage(ErroringBrowseController())
    qtbot.addWidget(page)

    page.load_path("/电影")

    status_widget = page.breadcrumb_layout.itemAt(0).widget()
    assert status_widget.text() == "/电影 | 加载文件列表超时"
    assert page.table.rowCount() == 0
```

- [ ] **Step 2: Run the browse-page regression test**

Run:

```bash
uv run pytest tests/test_browse_page_ui.py::test_browse_page_shows_folder_timeout_in_breadcrumb_status -v
```

Expected:
- `1 passed`

If this test fails, stop and compare `BrowsePage.load_path()` with `_set_breadcrumb_status()` before changing any UI code. The intended outcome is to confirm that the existing `ApiError` path already renders the message correctly.

- [ ] **Step 3: Re-run all timeout-focused coverage together**

Run:

```bash
uv run pytest \
  tests/test_api_client.py \
  tests/test_app.py::test_app_coordinator_show_main_keeps_window_open_when_initial_browse_times_out \
  tests/test_browse_page_ui.py::test_browse_page_shows_folder_timeout_in_breadcrumb_status \
  -v
```

Expected:
- All selected tests pass.

- [ ] **Step 4: Commit the regression coverage**

Run:

```bash
git add tests/test_browse_page_ui.py
git commit -m "test: cover browse timeout status"
```

### Task 3: Run Full Verification Before Hand-off

**Files:**
- No file changes expected

- [ ] **Step 1: Run the full test suite**

Run:

```bash
uv run pytest
```

Expected:
- Entire suite passes with no new failures.

- [ ] **Step 2: Capture the verification result in the hand-off summary**

Record:
- The exact `pytest` summary line.
- That startup now stays in the main window and the browse area shows `加载文件列表超时`.
- That 401 behavior remains unchanged because the response-status branch in `ApiClient._request()` was not modified.
