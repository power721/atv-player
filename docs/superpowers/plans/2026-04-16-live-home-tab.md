# Live Home Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `网络直播` tab after `电报影视` that browses `/live/{token}` categories and nested folders, then opens playable live-stream detail results in the existing player.

**Architecture:** Extend `ApiClient` with `/live/{token}` helpers, add a focused `LiveController` that mirrors the existing poster-grid page contract, and wire a new shared `DoubanPage` instance into `MainWindow` and `AppCoordinator`. Keep nested folder handling in the main window, matching the existing Emby and Jellyfin interaction pattern.

**Tech Stack:** Python, PySide6, httpx, pytest

---

## File Structure

- `src/atv_player/api.py`
  Adds `/live/{token}` request helpers for category listing, nested folder listing, and detail-by-`ids`.
- `src/atv_player/controllers/live_controller.py`
  Maps live payloads into `DoubanCategory`, `VodItem`, and `OpenPlayerRequest`, including direct-stream playlist extraction from detail payloads.
- `src/atv_player/app.py`
  Instantiates the live controller and passes it into the main window.
- `src/atv_player/ui/main_window.py`
  Adds the `网络直播` tab, tab ordering, page construction, and folder-vs-playback click handling.
- `tests/test_api_client.py`
  Covers the live request shapes.
- `tests/test_live_controller.py`
  Covers live category insertion, nested folder loading, and playback request construction.
- `tests/test_app.py`
  Covers tab ordering, the hidden search controls on the live page, and live card click behavior.

### Task 1: Cover the live API surface

**Files:**
- Modify: `tests/test_api_client.py`
- Modify: `src/atv_player/api.py`

- [ ] **Step 1: Write the failing test**

Add these tests to `tests/test_api_client.py`:

```python
def test_api_client_lists_live_categories() -> None:
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

    client.list_live_categories()

    assert seen == {"path": "/live/Harold", "query": ""}


def test_api_client_lists_live_items() -> None:
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

    client.list_live_items("bili", page=1)
    client.list_live_items("bili-9", page=1)
    client.list_live_items("bili-9-744", page=2)

    assert seen_queries == ["t=bili&pg=1", "t=bili-9&pg=1", "t=bili-9-744&pg=2"]


def test_api_client_gets_live_detail_by_ids() -> None:
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

    client.get_live_detail("bili$1785607569")

    assert seen == {"path": "/live/Harold", "query": "ids=bili%241785607569"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_client.py -k "live_" -v`
Expected: FAIL with `AttributeError` because the live API helpers do not exist yet.

- [ ] **Step 3: Write minimal implementation**

Add these methods to `src/atv_player/api.py`:

```python
def list_live_categories(self) -> dict[str, Any]:
    return self._request("GET", f"/live/{self._vod_token}")


def list_live_items(self, category_id: str, page: int) -> dict[str, Any]:
    return self._request(
        "GET",
        f"/live/{self._vod_token}",
        params={"t": category_id, "pg": page},
    )


def get_live_detail(self, vod_id: str) -> dict[str, Any]:
    return self._request("GET", f"/live/{self._vod_token}", params={"ids": vod_id})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api_client.py -k "live_" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_api_client.py src/atv_player/api.py
git commit -m "test: cover live home api"
```

### Task 2: Add the live controller

**Files:**
- Create: `tests/test_live_controller.py`
- Create: `src/atv_player/controllers/live_controller.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_live_controller.py` with:

```python
from atv_player.models import DoubanCategory


class FakeApiClient:
    def __init__(self) -> None:
        self.category_payload = {"class": []}
        self.items_payload = {"list": [], "total": 0}
        self.detail_payload = {"list": []}
        self.item_calls: list[tuple[str, int]] = []
        self.detail_calls: list[str] = []

    def list_live_categories(self) -> dict:
        return self.category_payload

    def list_live_items(self, category_id: str, page: int) -> dict:
        self.item_calls.append((category_id, page))
        return self.items_payload

    def get_live_detail(self, vod_id: str) -> dict:
        self.detail_calls.append(vod_id)
        return self.detail_payload


def test_load_categories_inserts_recommendation_first() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "bili", "type_name": "哔哩哔哩"},
            {"type_id": "douyu", "type_name": "斗鱼"},
        ]
    }
    controller = LiveController(api)

    categories = controller.load_categories()

    assert categories == [
        DoubanCategory(type_id="recommend", type_name="推荐"),
        DoubanCategory(type_id="bili", type_name="哔哩哔哩"),
        DoubanCategory(type_id="douyu", type_name="斗鱼"),
    ]


def test_load_folder_items_reuses_live_listing_api() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()
    api.items_payload = {
        "list": [
            {"vod_id": "bili-9-744", "vod_name": "分区", "vod_tag": "folder"},
            {"vod_id": "bili$1785607569", "vod_name": "直播间", "vod_tag": "file"},
        ]
    }
    controller = LiveController(api)

    items, total = controller.load_folder_items("bili-9")

    assert api.item_calls == [("bili-9", 1)]
    assert total == 2
    assert [(item.vod_id, item.vod_tag) for item in items] == [
        ("bili-9-744", "folder"),
        ("bili$1785607569", "file"),
    ]


def test_build_request_parses_title_url_playlist_from_detail_payload() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "bili$1785607569",
                "vod_name": "主播直播间",
                "vod_play_url": "线路 1$https://stream.example/live.m3u8#线路 2$https://backup.example/live.m3u8",
            }
        ]
    }
    controller = LiveController(api)

    request = controller.build_request("bili$1785607569")

    assert api.detail_calls == ["bili$1785607569"]
    assert request.vod.vod_id == "bili$1785607569"
    assert [item.title for item in request.playlist] == ["线路 1", "线路 2"]
    assert [item.url for item in request.playlist] == [
        "https://stream.example/live.m3u8",
        "https://backup.example/live.m3u8",
    ]


def test_build_request_prefers_detail_items_when_item_urls_exist() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "bili$1785607569",
                "vod_name": "主播直播间",
                "items": [
                    {"title": "高清", "url": "https://stream.example/hd.m3u8", "vod_id": "line-hd"},
                    {"title": "超清", "url": "https://stream.example/uhd.m3u8", "vod_id": "line-uhd"},
                ],
            }
        ]
    }
    controller = LiveController(api)

    request = controller.build_request("bili$1785607569")

    assert [item.title for item in request.playlist] == ["高清", "超清"]
    assert [item.url for item in request.playlist] == [
        "https://stream.example/hd.m3u8",
        "https://stream.example/uhd.m3u8",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_live_controller.py -v`
Expected: FAIL with `ModuleNotFoundError` because `live_controller.py` does not exist yet.

- [ ] **Step 3: Write minimal implementation**

Create `src/atv_player/controllers/live_controller.py` with a controller that:

- maps backend classes through `_map_category`
- prepends `DoubanCategory(type_id="recommend", type_name="推荐")`
- maps list payloads through `_map_item`
- reuses `list_live_items(vod_id, page=1)` for folder loading
- converts detail payloads into `OpenPlayerRequest`
- prefers `detail.items` when they already contain URLs
- otherwise parses `vod_play_url` into `PlayItem(title, url, vod_id)`

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_live_controller.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_live_controller.py src/atv_player/controllers/live_controller.py
git commit -m "feat: add live home controller"
```

### Task 3: Wire the live tab into the application

**Files:**
- Modify: `tests/test_app.py`
- Modify: `src/atv_player/ui/main_window.py`
- Modify: `src/atv_player/app.py`

- [ ] **Step 1: Write the failing test**

Add tests to `tests/test_app.py` covering:

```python
def test_main_window_starts_with_live_tab_after_telegram(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.tabText(2) == "网络直播"


def test_main_window_disables_search_controls_for_live_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)

    assert window.live_page.keyword_edit.isHidden() is True


def test_main_window_opens_player_from_live_card_signal(qtbot, monkeypatch) -> None:
    controller = FakeLiveController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=controller,
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))

    window.live_page.item_open_requested.emit(VodItem(vod_id="bili$1785607569", vod_name="直播间", vod_tag="file"))

    assert opened
    assert opened[0].source_vod_id == "bili$1785607569"


def test_main_window_live_folder_click_loads_folder_in_current_tab(qtbot, monkeypatch) -> None:
    controller = FakeLiveController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=controller,
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened = []
    shown = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(
        window.live_page,
        "show_items",
        lambda items, total, page=1, empty_message="当前分类暂无内容": shown.append((items, total, page, empty_message)),
    )

    window.live_page.item_open_requested.emit(VodItem(vod_id="bili-9", vod_name="分区", vod_tag="folder"))

    assert opened == []
    assert controller.folder_calls == ["bili-9"]
    assert shown[0][1:] == (1, 1, "当前文件夹暂无内容")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py -k "live_" -v`
Expected: FAIL because `MainWindow` does not accept `live_controller` and has no live-tab wiring yet.

- [ ] **Step 3: Write minimal implementation**

Update:

- `src/atv_player/app.py` to instantiate `LiveController`
- `src/atv_player/ui/main_window.py` to accept `live_controller`, create `self.live_page = DoubanPage(..., click_action="open")`, insert the `网络直播` tab after `电报影视`, lazy-load it, and handle folder-vs-open behavior

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py -k "live_|main_window_starts|hides_emby" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_app.py src/atv_player/ui/main_window.py src/atv_player/app.py
git commit -m "feat: add live home tab"
```

### Task 4: Run focused verification

**Files:**
- Modify: none

- [ ] **Step 1: Run the focused suites**

Run: `uv run pytest tests/test_api_client.py tests/test_live_controller.py tests/test_app.py -v`
Expected: PASS for the live API, controller, and tab wiring coverage.

- [ ] **Step 2: Run adjacent regression coverage**

Run: `uv run pytest tests/test_emby_controller.py tests/test_jellyfin_controller.py tests/test_douban_page_ui.py -v`
Expected: PASS to confirm the shared page behavior still matches the existing media-home tabs.

- [ ] **Step 3: Commit verification state**

```bash
git add docs/superpowers/specs/2026-04-16-live-home-tab-design.md docs/superpowers/plans/2026-04-16-live-home-tab.md
git commit -m "docs: add live home tab design and plan"
```
