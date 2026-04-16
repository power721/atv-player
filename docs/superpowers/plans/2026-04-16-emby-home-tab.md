# Emby Home Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `Emby` tab after `电报影视` that reuses the existing poster-grid/search UI and opens the player directly through `/emby/{token}` APIs.

**Architecture:** Extend `ApiClient` with Emby endpoints, add an `EmbyController` that matches the Telegram controller surface, and wire a third shared poster-grid page into `MainWindow`. Keep the page reuse intact: both Telegram and Emby use `DoubanPage` in `open` mode with search enabled, while only the controller and main-window wiring differ.

**Tech Stack:** Python, PySide6, httpx, pytest

---

## File Structure

- `src/atv_player/api.py`
  Adds `/emby/{token}` request helpers for categories, category items, keyword search, and detail-by-`ids`.
- `src/atv_player/controllers/emby_controller.py`
  Maps Emby payloads into `DoubanCategory`, `VodItem`, and `OpenPlayerRequest`, reusing the same playlist parsing pattern as Telegram.
- `src/atv_player/app.py`
  Instantiates the Emby controller and passes it into the main window.
- `src/atv_player/ui/main_window.py`
  Adds the `Emby` tab, tab ordering, page construction, unauthorized handling, and click-to-open wiring.
- `tests/test_api_client.py`
  Covers the Emby request shapes.
- `tests/test_emby_controller.py`
  Covers Emby category/search/detail mapping and playback request construction.
- `tests/test_app.py`
  Covers tab ordering, search-enabled page creation, and direct playback wiring for Emby.

### Task 1: Cover the Emby API surface

**Files:**
- Modify: `tests/test_api_client.py`
- Modify: `src/atv_player/api.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_api_client.py`:

```python
def test_api_client_lists_emby_categories() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"class": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_emby_categories()

    assert seen == {"path": "/emby/Harold", "query": ""}


def test_api_client_lists_emby_items() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_emby_items("Series", page=1)
    client.list_emby_items("Series", page=3)

    assert seen_queries == ["t=Series&pg=1", "t=Series&pg=3"]


def test_api_client_searches_emby_items_by_keyword() -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.query.decode())
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.search_emby_items("黑袍纠察队", page=1)
    client.search_emby_items("黑袍纠察队", page=2)

    assert seen_queries == [
        "wd=%E9%BB%91%E8%A2%8D%E7%BA%A0%E5%AF%9F%E9%98%9F",
        "wd=%E9%BB%91%E8%A2%8D%E7%BA%A0%E5%AF%9F%E9%98%9F&pg=2",
    ]


def test_api_client_gets_emby_detail_by_ids() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"list": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_emby_detail("1-3281")

    assert seen == {"path": "/emby/Harold", "query": "ids=1-3281"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_client.py -k "emby_" -v`
Expected: FAIL with `AttributeError` because `ApiClient` does not expose the Emby helpers yet.

- [ ] **Step 3: Write minimal implementation**

Add these methods to `src/atv_player/api.py`:

```python
def list_emby_categories(self) -> dict[str, Any]:
    return self._request("GET", f"/emby/{self._vod_token}")


def list_emby_items(self, category_id: str, page: int) -> dict[str, Any]:
    return self._request(
        "GET",
        f"/emby/{self._vod_token}",
        params={"t": category_id, "pg": page},
    )


def search_emby_items(self, keyword: str, page: int) -> dict[str, Any]:
    params: dict[str, Any] = {"wd": keyword}
    if page > 1:
        params["pg"] = page
    return self._request("GET", f"/emby/{self._vod_token}", params=params)


def get_emby_detail(self, vod_id: str) -> dict[str, Any]:
    return self._request("GET", f"/emby/{self._vod_token}", params={"ids": vod_id})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_client.py -k "emby_" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_api_client.py src/atv_player/api.py
git commit -m "test: cover emby home api"
```

### Task 2: Add the Emby controller

**Files:**
- Create: `tests/test_emby_controller.py`
- Create: `src/atv_player/controllers/emby_controller.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_emby_controller.py` with:

```python
from atv_player.controllers.emby_controller import EmbyController
from atv_player.models import DoubanCategory


class FakeApiClient:
    def __init__(self) -> None:
        self.category_payload = {"class": []}
        self.items_payload = {"list": [], "total": 0}
        self.search_payload = {"list": [], "total": 0}
        self.detail_payload = {"list": []}
        self.item_calls: list[tuple[str, int]] = []
        self.search_calls: list[tuple[str, int]] = []
        self.detail_calls: list[str] = []
        self.resolve_calls: list[str] = []

    def list_emby_categories(self) -> dict:
        return self.category_payload

    def list_emby_items(self, category_id: str, page: int) -> dict:
        self.item_calls.append((category_id, page))
        return self.items_payload

    def search_emby_items(self, keyword: str, page: int) -> dict:
        self.search_calls.append((keyword, page))
        return self.search_payload

    def get_emby_detail(self, vod_id: str) -> dict:
        self.detail_calls.append(vod_id)
        return self.detail_payload

    def get_detail(self, vod_id: str) -> dict:
        self.resolve_calls.append(vod_id)
        return {
            "list": [
                {
                    "vod_id": vod_id,
                    "vod_name": f"Resolved {vod_id}",
                    "vod_play_url": f"http://m/{vod_id}.m3u8",
                    "items": [
                        {"title": f"Resolved {vod_id}", "url": f"http://m/{vod_id}.m3u8", "vod_id": vod_id},
                    ],
                }
            ]
        }


def test_load_categories_maps_emby_class_payload() -> None:
    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "Series", "type_name": "剧集"},
            {"type_id": "Movie", "type_name": "电影"},
        ]
    }
    controller = EmbyController(api)

    categories = controller.load_categories()

    assert categories == [
        DoubanCategory(type_id="Series", type_name="剧集"),
        DoubanCategory(type_id="Movie", type_name="电影"),
    ]


def test_search_items_maps_emby_search_payload() -> None:
    api = FakeApiClient()
    api.search_payload = {
        "list": [
            {
                "vod_id": "1-3281",
                "vod_name": "黑袍纠察队",
                "vod_pic": "poster.jpg",
                "vod_remarks": "4K",
            }
        ],
        "total": 31,
    }
    controller = EmbyController(api)

    items, total = controller.search_items("黑袍纠察队", page=1)

    assert api.search_calls == [("黑袍纠察队", 1)]
    assert total == 31
    assert items[0].vod_id == "1-3281"
    assert items[0].vod_name == "黑袍纠察队"


def test_build_request_from_detail_uses_ids_endpoint_and_playlist_parsing() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1-3281",
                "vod_name": "Season 1",
                "vod_pic": "poster.jpg",
                "vod_play_url": "Episode 1$1-3282#Episode 2$1-3283",
            }
        ]
    }
    controller = EmbyController(api)

    request = controller.build_request("1-3281")

    assert api.detail_calls == ["1-3281"]
    assert request.vod.vod_id == "1-3281"
    assert [item.title for item in request.playlist] == ["Episode 1", "Episode 2"]
    assert [item.vod_id for item in request.playlist] == ["1-3282", "1-3283"]
    resolved = request.detail_resolver(request.playlist[1])
    assert api.resolve_calls == ["1-3283"]
    assert resolved is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_emby_controller.py -v`
Expected: FAIL because the Emby controller module does not exist.

- [ ] **Step 3: Write minimal implementation**

Create `src/atv_player/controllers/emby_controller.py`:

```python
from __future__ import annotations

from atv_player.controllers.browse_controller import _map_vod_item
from atv_player.controllers.douban_controller import _map_category, _map_item
from atv_player.controllers.telegram_search_controller import _parse_playlist
from atv_player.models import DoubanCategory, OpenPlayerRequest, PlayItem, VodItem


class EmbyController:
    _PAGE_SIZE = 30

    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def load_categories(self) -> list[DoubanCategory]:
        payload = self._api_client.list_emby_categories()
        return [_map_category(item) for item in payload.get("class", [])]

    def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_emby_items(category_id, page=page)
        items = [_map_item(item) for item in payload.get("list", [])]
        total_raw = payload.get("total")
        if total_raw is not None:
            total = int(total_raw)
        else:
            pagecount = int(payload.get("pagecount") or 0)
            total = pagecount * self._PAGE_SIZE
        return items, total

    def search_items(self, keyword: str, page: int) -> tuple[list[VodItem], int]:
        payload = self._api_client.search_emby_items(keyword, page=page)
        items = [_map_item(item) for item in payload.get("list", [])]
        total_raw = payload.get("total")
        if total_raw is not None:
            total = int(total_raw)
        else:
            pagecount = int(payload.get("pagecount") or 0)
            total = pagecount * self._PAGE_SIZE
        return items, total

    def resolve_playlist_item(self, item: PlayItem) -> VodItem | None:
        if not item.vod_id:
            return None
        try:
            payload = self._api_client.get_detail(item.vod_id)
            return _map_vod_item(payload["list"][0])
        except (KeyError, IndexError):
            return None

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        payload = self._api_client.get_emby_detail(vod_id)
        detail = _map_vod_item(payload["list"][0])
        playlist = _parse_playlist(detail.vod_play_url)
        if not playlist and detail.items:
            playlist = list(detail.items)
        if not playlist:
            raise ValueError(f"没有可播放的项目: {detail.vod_name}")
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            clicked_index=0,
            source_mode="detail",
            source_vod_id=detail.vod_id,
            detail_resolver=self.resolve_playlist_item,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_emby_controller.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_emby_controller.py src/atv_player/controllers/emby_controller.py
git commit -m "feat: add emby home controller"
```

### Task 3: Wire the Emby tab into the app

**Files:**
- Modify: `tests/test_app.py`
- Modify: `src/atv_player/ui/main_window.py`
- Modify: `src/atv_player/app.py`

- [ ] **Step 1: Write the failing tests**

Update `tests/test_app.py` with:

```python
class FakeEmbyController(FakeDoubanController):
    def build_request(self, vod_id: str):
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="Emby Movie"),
            playlist=[PlayItem(title="Episode 1", url="", vod_id="ep-emby-1")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )
```

Change `test_main_window_starts_on_douban_tab()` to construct `MainWindow` with `emby_controller=FakeEmbyController()` and assert:

```python
assert window.nav_tabs.count() == 5
assert window.nav_tabs.tabText(0) == "豆瓣电影"
assert window.nav_tabs.tabText(1) == "电报影视"
assert window.nav_tabs.tabText(2) == "Emby"
assert window.nav_tabs.tabText(3) == "文件浏览"
assert window.nav_tabs.tabText(4) == "播放记录"
```

Add:

```python
def test_main_window_enables_search_controls_for_emby_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        emby_controller=FakeEmbyController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)

    assert window.emby_page.keyword_edit.isHidden() is False


def test_main_window_opens_player_from_emby_card_signal(qtbot, monkeypatch) -> None:
    controller = FakeEmbyController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        emby_controller=controller,
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))

    window.emby_page.open_requested.emit("1-3281")

    assert opened
    assert opened[0].vod.vod_name == "Emby Movie"
    assert opened[0].source_vod_id == "1-3281"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py -k "emby" -v`
Expected: FAIL because `MainWindow` and `AppCoordinator` do not accept `emby_controller` yet.

- [ ] **Step 3: Write minimal implementation**

Update `src/atv_player/ui/main_window.py`:

```python
class _EmptyEmbyController(_EmptyDoubanController):
    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")
```

Extend `MainWindow.__init__`:

```python
def __init__(..., douban_controller=None, telegram_controller=None, emby_controller=None) -> None:
```

Construct and wire the page:

```python
self.emby_page = DoubanPage(
    emby_controller or _EmptyEmbyController(),
    click_action="open",
    search_enabled=True,
)
self.emby_controller = emby_controller or _EmptyEmbyController()
```

Update tabs:

```python
self.nav_tabs.addTab(self.douban_page, "豆瓣电影")
self.nav_tabs.addTab(self.telegram_page, "电报影视")
self.nav_tabs.addTab(self.emby_page, "Emby")
self.nav_tabs.addTab(self.browse_page, "文件浏览")
self.nav_tabs.addTab(self.history_page, "播放记录")
```

Connect signals:

```python
self.emby_page.open_requested.connect(self._handle_emby_open_requested)
self.emby_page.unauthorized.connect(self.logout_requested.emit)
```

Add:

```python
def _handle_emby_open_requested(self, vod_id: str) -> None:
    try:
        request = self.emby_controller.build_request(vod_id)
    except Exception as exc:
        self.show_error(str(exc))
        return
    self.open_player(request)
```

Update `src/atv_player/app.py`:

```python
from atv_player.controllers.emby_controller import EmbyController
...
emby_controller = EmbyController(self._api_client)
...
emby_controller=emby_controller,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py -k "emby" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_app.py src/atv_player/ui/main_window.py src/atv_player/app.py
git commit -m "feat: wire emby home tab"
```

### Task 4: Final verification

**Files:**
- Test: `tests/test_api_client.py`
- Test: `tests/test_emby_controller.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Run the focused verification suite**

Run: `uv run pytest tests/test_api_client.py tests/test_emby_controller.py tests/test_app.py -k "emby" -v`
Expected: PASS

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 3: Commit the finished change**

```bash
git add src/atv_player/api.py src/atv_player/controllers/emby_controller.py src/atv_player/ui/main_window.py src/atv_player/app.py tests/test_api_client.py tests/test_emby_controller.py tests/test_app.py docs/superpowers/specs/2026-04-16-emby-home-tab-design.md docs/superpowers/plans/2026-04-16-emby-home-tab.md
git commit -m "feat: add emby home tab"
```
