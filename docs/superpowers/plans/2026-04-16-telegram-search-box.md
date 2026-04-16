# Telegram Search Box Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a page-local search box to the `电报影视` tab that searches `/tg-search/{token}?web=true&wd=<keyword>`, replaces category cards with search-result cards, and restores category browsing when cleared.

**Architecture:** Extend the Telegram API/controller surface with a keyword search method that returns the same `(items, total)` shape as category browsing. Reuse the existing poster-grid page by adding optional search controls and a dual-mode state machine (`category` vs `search`) so Telegram gains search without forking a separate page and Douban remains unchanged.

**Tech Stack:** Python, PySide6, httpx, pytest

---

## File Structure

- `src/atv_player/api.py`
  Adds the low-level Telegram keyword search request for `/tg-search/{token}?web=true&wd=<keyword>`.
- `src/atv_player/controllers/telegram_search_controller.py`
  Maps Telegram keyword search responses into `VodItem` objects and returns `(items, total)` for the UI.
- `src/atv_player/ui/douban_page.py`
  Gains optional search controls and a small mode/state layer so the same poster-grid page can run either in category mode only or in category-plus-search mode.
- `src/atv_player/ui/main_window.py`
  Instantiates the Telegram page with search enabled while leaving Douban unchanged.
- `tests/test_api_client.py`
  Covers the new Telegram keyword search request shape.
- `tests/test_telegram_search_controller.py`
  Covers Telegram keyword search result mapping and total/pagecount handling.
- `tests/test_douban_page_ui.py`
  Covers search control visibility, search mode, clear behavior, and click behavior in Telegram open mode.
- `tests/test_app.py`
  Confirms the Telegram page is created with search enabled and Douban is not.

### Task 1: Add API coverage for Telegram keyword search

**Files:**
- Modify: `tests/test_api_client.py`
- Modify: `src/atv_player/api.py`

- [ ] **Step 1: Write the failing test**

Add this test to `tests/test_api_client.py`:

```python
def test_api_client_searches_telegram_items_by_keyword() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.search_telegram_items("黑袍纠察队", page=1)

    assert seen == {
        "path": "/tg-search/Harold",
        "query": "web=true&wd=%E9%BB%91%E8%A2%8D%E7%BA%A0%E5%AF%9F%E9%98%9F",
    }
```

Add a second assertion in the same test for a later page:

```python
    client.search_telegram_items("黑袍纠察队", page=3)

    assert seen == {
        "path": "/tg-search/Harold",
        "query": "web=true&wd=%E9%BB%91%E8%A2%8D%E7%BA%A0%E5%AF%9F%E9%98%9F&pg=3",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_client.py::test_api_client_searches_telegram_items_by_keyword -v`
Expected: FAIL with `AttributeError` because `ApiClient` does not expose `search_telegram_items()`.

- [ ] **Step 3: Write minimal implementation**

Add this method to `src/atv_player/api.py`:

```python
def search_telegram_items(self, keyword: str, page: int) -> dict[str, Any]:
    params: dict[str, Any] = {"web": True, "wd": keyword}
    if page > 1:
        params["pg"] = page
    return self._request("GET", f"/tg-search/{self._vod_token}", params=params)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_client.py::test_api_client_searches_telegram_items_by_keyword -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_api_client.py src/atv_player/api.py
git commit -m "test: cover telegram keyword search api"
```

### Task 2: Add Telegram controller search mapping

**Files:**
- Modify: `tests/test_telegram_search_controller.py`
- Modify: `src/atv_player/controllers/telegram_search_controller.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_telegram_search_controller.py`:

```python
def test_search_items_maps_search_payload() -> None:
    api = FakeApiClient()
    api.search_payload = {
        "list": [
            {
                "vod_id": "https://pan.quark.cn/s/demo",
                "vod_name": "黑袍纠察队",
                "vod_pic": "poster.jpg",
                "vod_remarks": "4K",
            }
        ],
        "total": 31,
    }
    controller = TelegramSearchController(api)

    items, total = controller.search_items("黑袍纠察队", page=1)

    assert api.search_calls == [("黑袍纠察队", 1)]
    assert total == 31
    assert items[0].vod_id == "https://pan.quark.cn/s/demo"
    assert items[0].vod_name == "黑袍纠察队"
    assert items[0].vod_pic == "poster.jpg"
    assert items[0].vod_remarks == "4K"


def test_search_items_uses_pagecount_when_total_is_missing() -> None:
    api = FakeApiClient()
    api.search_payload = {"list": [], "pagecount": 3}
    controller = TelegramSearchController(api)

    _items, total = controller.search_items("黑袍纠察队", page=2)

    assert total == 90
```

Update the fake API in the same test file with:

```python
self.search_payload = {"list": [], "total": 0}
self.search_calls: list[tuple[str, int]] = []

def search_telegram_items(self, keyword: str, page: int) -> dict:
    self.search_calls.append((keyword, page))
    return self.search_payload
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telegram_search_controller.py -k "search_items" -v`
Expected: FAIL because `TelegramSearchController` does not implement `search_items()`.

- [ ] **Step 3: Write minimal implementation**

Add this method to `src/atv_player/controllers/telegram_search_controller.py`:

```python
def search_items(self, keyword: str, page: int) -> tuple[list[VodItem], int]:
    payload = self._api_client.search_telegram_items(keyword, page=page)
    items = [_map_item(item) for item in payload.get("list", [])]
    total_raw = payload.get("total")
    if total_raw is not None:
        total = int(total_raw)
    else:
        pagecount = int(payload.get("pagecount") or 0)
        total = pagecount * self._PAGE_SIZE
    return items, total
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_telegram_search_controller.py -k "search_items" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_telegram_search_controller.py src/atv_player/controllers/telegram_search_controller.py
git commit -m "feat: add telegram controller keyword search"
```

### Task 3: Add optional search mode to the poster-grid page

**Files:**
- Modify: `tests/test_douban_page_ui.py`
- Modify: `src/atv_player/ui/douban_page.py`

- [ ] **Step 1: Write the failing tests**

Extend `tests/test_douban_page_ui.py` with a Telegram-capable fake controller:

```python
class SearchableDoubanController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self.search_calls: list[tuple[str, int]] = []
        self.search_results = (
            [VodItem(vod_id="s1", vod_name="黑袍纠察队", vod_pic="poster-search", vod_remarks="搜索结果")],
            30,
        )

    def search_items(self, keyword: str, page: int):
        self.search_calls.append((keyword, page))
        return self.search_results
```

Add these tests:

```python
def test_douban_page_can_show_search_controls_when_enabled(qtbot) -> None:
    page = DoubanPage(SearchableDoubanController(), click_action="open", search_enabled=True)
    qtbot.addWidget(page)

    assert page.keyword_edit.isHidden() is False
    assert page.search_button.isHidden() is False
    assert page.clear_button.isHidden() is False


def test_douban_page_search_replaces_category_cards_and_clear_restores_category(qtbot) -> None:
    controller = SearchableDoubanController()
    page = DoubanPage(controller, click_action="open", search_enabled=True)
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    assert page.card_buttons[0].text() == "霸王别姬\n9.6"

    page.keyword_edit.setText("黑袍纠察队")
    page.search()

    qtbot.waitUntil(lambda: controller.search_calls == [("黑袍纠察队", 1)])
    qtbot.waitUntil(lambda: page.card_buttons[0].text() == "黑袍纠察队\n搜索结果")
    assert page.current_page == 1

    page.clear_search()

    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("suggestion", 1))
    qtbot.waitUntil(lambda: page.card_buttons[0].text() == "霸王别姬\n9.6")


def test_douban_page_clicking_search_result_can_emit_open_requested(qtbot) -> None:
    controller = SearchableDoubanController()
    page = DoubanPage(controller, click_action="open", search_enabled=True)
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    page.keyword_edit.setText("黑袍纠察队")
    page.search()
    qtbot.waitUntil(lambda: page.card_buttons[0].text() == "黑袍纠察队\n搜索结果")

    with qtbot.waitSignal(page.open_requested, timeout=1000) as signal:
        page.card_buttons[0].click()

    assert signal.args == ["s1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_douban_page_ui.py -k "search_controls or search_replaces or search_result_can_emit_open_requested" -v`
Expected: FAIL because `DoubanPage` does not yet expose search controls or search mode methods.

- [ ] **Step 3: Write minimal implementation**

Update `src/atv_player/ui/douban_page.py` as follows:

```python
from PySide6.QtWidgets import QLineEdit

class DoubanPage(QWidget):
    def __init__(self, controller, click_action: str = "search", search_enabled: bool = False) -> None:
        self._search_enabled = search_enabled
        self._search_mode = False
        self._search_keyword = ""
        self.keyword_edit = QLineEdit()
        self.search_button = QPushButton("搜索")
        self.clear_button = QPushButton("清空")
        ...
```

Add the controls to the right-hand layout only when enabled:

```python
if self._search_enabled:
    search_row = QHBoxLayout()
    search_row.addWidget(self.keyword_edit, 1)
    search_row.addWidget(self.search_button)
    search_row.addWidget(self.clear_button)
    right.addLayout(search_row)
else:
    self.keyword_edit.hide()
    self.search_button.hide()
    self.clear_button.hide()
```

Add these methods:

```python
def search(self) -> None:
    keyword = self.keyword_edit.text().strip()
    if not keyword:
        self.clear_search()
        return
    self._search_mode = True
    self._search_keyword = keyword
    self.current_page = 1
    self._search_items(keyword, self.current_page)


def clear_search(self) -> None:
    if not self._search_enabled:
        return
    self.keyword_edit.clear()
    self._search_mode = False
    self._search_keyword = ""
    self.current_page = 1
    if self.selected_category_id:
        self.load_items(self.selected_category_id, self.current_page)


def _search_items(self, keyword: str, page: int) -> None:
    self._items_request_id += 1
    request_id = self._items_request_id
    self.status_label.setText("搜索中...")
    def run() -> None:
        try:
            items, total = self.controller.search_items(keyword, page)
        ...
```

Update `_handle_category_row_changed()`, `previous_page()`, and `next_page()` to keep category-mode paging and search-mode paging separate:

```python
if self._search_mode:
    self._search_items(self._search_keyword, self.current_page)
else:
    self.load_items(self.selected_category_id, self.current_page)
```

Connect:

```python
if self._search_enabled:
    self.search_button.clicked.connect(self.search)
    self.clear_button.clicked.connect(self.clear_search)
    self.keyword_edit.returnPressed.connect(self.search)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_douban_page_ui.py -k "search_controls or search_replaces or search_result_can_emit_open_requested" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_douban_page_ui.py src/atv_player/ui/douban_page.py
git commit -m "feat: add telegram search mode to poster page"
```

### Task 4: Wire Telegram page search on and verify app integration

**Files:**
- Modify: `tests/test_app.py`
- Modify: `src/atv_player/ui/main_window.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_app.py`:

```python
def test_main_window_enables_search_controls_only_for_telegram_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)

    assert window.douban_page.keyword_edit.isHidden() is True
    assert window.telegram_page.keyword_edit.isHidden() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py::test_main_window_enables_search_controls_only_for_telegram_page -v`
Expected: FAIL because `MainWindow` does not yet create the Telegram page with `search_enabled=True`.

- [ ] **Step 3: Write minimal implementation**

Update the Telegram page construction in `src/atv_player/ui/main_window.py`:

```python
self.telegram_page = DoubanPage(
    telegram_controller or _EmptyTelegramController(),
    click_action="open",
    search_enabled=True,
)
```

Leave the Douban page construction unchanged so its search controls stay hidden.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py::test_main_window_enables_search_controls_only_for_telegram_page -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_app.py src/atv_player/ui/main_window.py
git commit -m "feat: enable telegram search box"
```

### Task 5: Final verification

**Files:**
- Test: `tests/test_api_client.py`
- Test: `tests/test_telegram_search_controller.py`
- Test: `tests/test_douban_page_ui.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Run the focused verification suite**

Run: `uv run pytest tests/test_api_client.py tests/test_telegram_search_controller.py tests/test_douban_page_ui.py tests/test_app.py -k "telegram or search" -v`
Expected: PASS

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 3: Commit the finished change**

```bash
git add src/atv_player/api.py src/atv_player/controllers/telegram_search_controller.py src/atv_player/ui/douban_page.py src/atv_player/ui/main_window.py tests/test_api_client.py tests/test_telegram_search_controller.py tests/test_douban_page_ui.py tests/test_app.py docs/superpowers/specs/2026-04-16-telegram-search-box-design.md docs/superpowers/plans/2026-04-16-telegram-search-box.md
git commit -m "feat: add telegram home search box"
```
