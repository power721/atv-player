# Telegram Search Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `电报影视` tab after `豆瓣电影`, backed by `/tg-search/{token}`, reusing the Douban-style card layout and opening the player directly from card clicks.

**Architecture:** Extend `ApiClient` with `/tg-search/{token}` endpoints, add a dedicated controller that maps categories/items and converts detail responses into `OpenPlayerRequest`, and reuse the existing poster-grid page with a configurable click action so Douban keeps search behavior while Telegram opens the player. Main window wiring remains the single integration point for tab order and playback launch.

**Tech Stack:** Python, PySide6, httpx, pytest

---

### Task 1: Cover the tg-search API surface

**Files:**
- Modify: `tests/test_api_client.py`
- Modify: `src/atv_player/api.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_api_client_lists_telegram_search_categories() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = str(request.url.query)
        return httpx.Response(200, json={"class": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_telegram_search_categories()

    assert seen == {"path": "/tg-search/Harold", "query": "web=true"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_client.py -k "telegram_search_categories or telegram_search_items or telegram_search_detail" -v`
Expected: FAIL with `AttributeError` because `ApiClient` does not expose the tg-search helpers yet.

- [ ] **Step 3: Write minimal implementation**

```python
def list_telegram_search_categories(self) -> dict[str, Any]:
    return self._request("GET", f"/tg-search/{self._vod_token}", params={"web": True})

def list_telegram_search_items(self, category_id: str, page: int) -> dict[str, Any]:
    params = {"t": category_id, "web": True}
    if category_id != "0":
        params["pg"] = page
    return self._request("GET", f"/tg-search/{self._vod_token}", params=params)

def get_telegram_search_detail(self, vod_id: str) -> dict[str, Any]:
    return self._request("GET", f"/tg-search/{self._vod_token}", params={"id": vod_id})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_client.py -k "telegram_search_categories or telegram_search_items or telegram_search_detail" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_api_client.py src/atv_player/api.py
git commit -m "test: cover tg-search api client"
```

### Task 2: Build the Telegram search controller

**Files:**
- Create: `tests/test_telegram_search_controller.py`
- Create: `src/atv_player/controllers/telegram_search_controller.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_load_categories_inserts_recommendation_first() -> None:
    ...

def test_load_items_uses_recommendation_endpoint_without_page_param() -> None:
    ...

def test_build_request_from_detail_uses_folder_playback_resolution_pattern() -> None:
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telegram_search_controller.py -v`
Expected: FAIL because the controller module does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
class TelegramSearchController:
    def load_categories(self) -> list[DoubanCategory]:
        ...

    def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
        ...

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_telegram_search_controller.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_telegram_search_controller.py src/atv_player/controllers/telegram_search_controller.py
git commit -m "feat: add telegram search controller"
```

### Task 3: Reuse the card grid for direct-open behavior

**Files:**
- Modify: `tests/test_douban_page_ui.py`
- Modify: `src/atv_player/ui/douban_page.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_douban_page_clicking_card_can_emit_open_requested(qtbot) -> None:
    page = DoubanPage(FakeDoubanController(), click_action="open")
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_douban_page_ui.py -k "open_requested or clicking_card" -v`
Expected: FAIL because `DoubanPage` only emits search requests today.

- [ ] **Step 3: Write minimal implementation**

```python
class DoubanPage(QWidget):
    search_requested = Signal(str)
    open_requested = Signal(str)

    def __init__(self, controller, click_action: str = "search") -> None:
        self._click_action = click_action

    def _handle_card_clicked(self, item) -> None:
        if self._click_action == "open":
            self.open_requested.emit(item.vod_id)
            return
        self.search_requested.emit(item.vod_name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_douban_page_ui.py -k "open_requested or clicking_card" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_douban_page_ui.py src/atv_player/ui/douban_page.py
git commit -m "refactor: make poster grid card action configurable"
```

### Task 4: Wire the new tab into the app

**Files:**
- Modify: `tests/test_app.py`
- Modify: `src/atv_player/app.py`
- Modify: `src/atv_player/ui/main_window.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_main_window_inserts_telegram_tab_after_douban(qtbot) -> None:
    ...

def test_main_window_opens_player_from_telegram_card_signal(qtbot, monkeypatch) -> None:
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py -k "telegram_tab or telegram_card" -v`
Expected: FAIL because `MainWindow` and `AppCoordinator` do not accept the telegram controller yet.

- [ ] **Step 3: Write minimal implementation**

```python
self.telegram_page = DoubanPage(telegram_search_controller, click_action="open")
self.nav_tabs.addTab(self.telegram_page, "电报影视")
self.telegram_page.open_requested.connect(self._handle_telegram_open_requested)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py -k "telegram_tab or telegram_card" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_app.py src/atv_player/app.py src/atv_player/ui/main_window.py
git commit -m "feat: add telegram search home tab"
```

### Task 5: Final regression check

**Files:**
- Test: `tests/test_api_client.py`
- Test: `tests/test_telegram_search_controller.py`
- Test: `tests/test_douban_page_ui.py`
- Test: `tests/test_app.py`
- Test: `tests/test_player_controller.py`

- [ ] **Step 1: Run focused regression**

Run: `uv run pytest tests/test_api_client.py tests/test_telegram_search_controller.py tests/test_douban_page_ui.py tests/test_app.py tests/test_player_controller.py -v`
Expected: PASS

- [ ] **Step 2: Commit verification-ready changes**

```bash
git add tests/test_api_client.py tests/test_telegram_search_controller.py tests/test_douban_page_ui.py tests/test_app.py tests/test_player_controller.py src/atv_player/api.py src/atv_player/controllers/telegram_search_controller.py src/atv_player/ui/douban_page.py src/atv_player/ui/main_window.py src/atv_player/app.py
git commit -m "feat: add telegram search tab"
```
