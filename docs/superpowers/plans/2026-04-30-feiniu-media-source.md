# Feiniu Media Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone `Feiniu` media source that behaves like `Emby`, using `/feiniu` and `/feiniu-play`, with an independently gated main-window tab and independent local playback-history source metadata.

**Architecture:** Mirror the existing `Emby` vertical slice instead of introducing a shared abstraction in this change. Add Feiniu-specific API methods, a dedicated controller, app/main-window wiring, and history/UI recognition, while keeping existing `Emby` and `Jellyfin` behavior unchanged.

**Tech Stack:** Python, PySide6, httpx, pytest, existing `ApiClient`, `OpenPlayerRequest`, `PosterGridPage`, local playback history repository

---

## File Structure

- Create: `src/atv_player/controllers/feiniu_controller.py`
  Add a dedicated Feiniu controller mirroring the existing Emby behavior.
- Create: `tests/test_feiniu_controller.py`
  Add controller-level Feiniu coverage mirroring the existing Emby tests.
- Modify: `src/atv_player/api.py`
  Add `/feiniu` and `/feiniu-play` client methods.
- Modify: `src/atv_player/app.py`
  Import and instantiate `FeiniuController`, wire capability gating, and persist Feiniu playback history with source name `飞牛影视`.
- Modify: `src/atv_player/ui/main_window.py`
  Add the optional Feiniu tab, empty controller fallback, and tab event wiring.
- Modify: `src/atv_player/ui/history_page.py`
  Recognize `source_kind="feiniu"` in source-label formatting.
- Modify: `tests/test_api_client.py`
  Add Feiniu route and capability assertions.
- Modify: `tests/test_app.py`
  Add a fake Feiniu controller plus app/main-window integration tests.
- Modify: `tests/test_storage.py`
  Add a local playback-history round-trip for the Feiniu source kind.
- Modify: `tests/test_history_controller.py`
  Extend delete/merge coverage to include Feiniu local records.
- Modify: `tests/test_browse_page_ui.py`
  Verify the history page renders Feiniu source labels correctly.

### Task 1: Add Feiniu API Client Methods And Capability Parsing

**Files:**
- Modify: `src/atv_player/api.py`
- Modify: `src/atv_player/app.py`
- Test: `tests/test_api_client.py`

- [ ] **Step 1: Write the failing API-client tests**

Add these tests to `tests/test_api_client.py` near the existing Emby and Jellyfin API tests:

```python
def test_api_client_gets_capabilities_with_feiniu() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"emby": True, "jellyfin": False, "feiniu": True, "pansou": True})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        transport=httpx.MockTransport(handler),
    )

    assert client.get_capabilities()["feiniu"] is True


def test_api_client_lists_feiniu_categories() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"class": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.list_feiniu_categories()

    assert seen == {"path": "/feiniu/Harold", "query": ""}


def test_api_client_gets_feiniu_playback_source() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"url": "https://stream.example/1.m3u8"})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_feiniu_playback_source("1-5001")

    assert seen == {"path": "/feiniu-play/Harold", "query": "t=0&id=1-5001"}
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
uv run pytest \
  tests/test_api_client.py::test_api_client_gets_capabilities_with_feiniu \
  tests/test_api_client.py::test_api_client_lists_feiniu_categories \
  tests/test_api_client.py::test_api_client_gets_feiniu_playback_source -q
```

Expected: FAIL with `KeyError` or `AttributeError` because Feiniu capability parsing and `ApiClient.list_feiniu_categories()` / `get_feiniu_playback_source()` do not exist yet.

- [ ] **Step 3: Add the minimal API-client implementation**

Update `src/atv_player/api.py` by mirroring the existing Emby method family:

```python
    def list_feiniu_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/feiniu/{self._vod_token}")

    def list_feiniu_items(
        self,
        category_id: str,
        page: int,
        filters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"t": category_id, "pg": page}
        if filters:
            params.update(filters)
        return self._request("GET", f"/feiniu/{self._vod_token}", params=params)

    def search_feiniu_items(self, keyword: str, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {"wd": keyword}
        if page > 1:
            params["pg"] = page
        return self._request("GET", f"/feiniu/{self._vod_token}", params=params)

    def get_feiniu_detail(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/feiniu/{self._vod_token}", params={"ids": vod_id})

    def get_feiniu_playback_source(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/feiniu-play/{self._vod_token}", params={"t": 0, "id": vod_id})

    def report_feiniu_playback_progress(self, vod_id: str, position_ms: int) -> None:
        self._request("GET", f"/feiniu-play/{self._vod_token}", params={"t": position_ms, "id": vod_id})

    def stop_feiniu_playback(self, vod_id: str) -> None:
        self._request("GET", f"/feiniu-play/{self._vod_token}", params={"t": -1, "id": vod_id})
```

Extend `AppCoordinator._load_capabilities()` in `src/atv_player/app.py` so the default capability map and parsed response both include Feiniu:

```python
        default_capabilities = {"emby": True, "jellyfin": True, "feiniu": True}
        capabilities["feiniu"] = bool(response.get("feiniu", capabilities["feiniu"]))
```

- [ ] **Step 4: Run the API-client tests again**

Run:

```bash
uv run pytest tests/test_api_client.py -q
```

Expected: PASS, including the new Feiniu route tests and the existing Emby/Jellyfin coverage.

- [ ] **Step 5: Commit the API-client slice**

Run:

```bash
git add tests/test_api_client.py src/atv_player/api.py src/atv_player/app.py
git commit -m "feat: add feiniu api client support"
```

### Task 2: Add The Feiniu Controller

**Files:**
- Create: `src/atv_player/controllers/feiniu_controller.py`
- Create: `tests/test_feiniu_controller.py`

- [ ] **Step 1: Write the failing controller tests**

Create `tests/test_feiniu_controller.py` by copying the Emby test structure and renaming the API calls to Feiniu:

```python
from atv_player.controllers.feiniu_controller import FeiniuController
from atv_player.models import CategoryFilter, CategoryFilterOption, DoubanCategory, PlayItem


class FakeApiClient:
    def __init__(self) -> None:
        self.category_payload = {"class": []}
        self.items_payload = {"list": [], "total": 0}
        self.search_payload = {"list": [], "total": 0}
        self.detail_payload = {"list": []}
        self.playback_payload = {"url": ["Episode 1", "http://m/1.mp4"], "header": {"User-Agent": "Feiniu"}}
        self.item_calls: list[tuple[str, int, dict[str, str] | None]] = []
        self.search_calls: list[tuple[str, int]] = []
        self.detail_calls: list[str] = []
        self.playback_source_calls: list[str] = []
        self.playback_progress_calls: list[tuple[str, int]] = []
        self.playback_stop_calls: list[str] = []

    def list_feiniu_categories(self) -> dict:
        return self.category_payload

    def list_feiniu_items(self, category_id: str, page: int, filters: dict[str, str] | None = None) -> dict:
        self.item_calls.append((category_id, page, None if filters is None else dict(filters)))
        return self.items_payload

    def search_feiniu_items(self, keyword: str, page: int) -> dict:
        self.search_calls.append((keyword, page))
        return self.search_payload

    def get_feiniu_detail(self, vod_id: str) -> dict:
        self.detail_calls.append(vod_id)
        return self.detail_payload

    def get_feiniu_playback_source(self, vod_id: str) -> dict:
        self.playback_source_calls.append(vod_id)
        return self.playback_payload

    def report_feiniu_playback_progress(self, vod_id: str, position_ms: int) -> None:
        self.playback_progress_calls.append((vod_id, position_ms))

    def stop_feiniu_playback(self, vod_id: str) -> None:
        self.playback_stop_calls.append(vod_id)
```

Include at least these initial tests in the new file:

```python
def test_load_categories_inserts_recommendation_first() -> None:
    api = FakeApiClient()
    api.category_payload = {"class": [{"type_id": "Series", "type_name": "剧集"}]}
    controller = FeiniuController(api)

    assert controller.load_categories() == [
        DoubanCategory(type_id="0", type_name="推荐"),
        DoubanCategory(type_id="Series", type_name="剧集"),
    ]


def test_build_request_disables_remote_history_and_exposes_local_feiniu_history_hooks() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1-5001",
                "vod_name": "Season 1",
                "vod_pic": "poster.jpg",
                "vod_play_url": "Episode 1$1-5002#Episode 2$1-5003",
            }
        ]
    }
    load_calls: list[str] = []
    save_calls: list[tuple[str, dict[str, object]]] = []
    controller = FeiniuController(
        api,
        playback_history_loader=lambda vod_id: load_calls.append(vod_id) or None,
        playback_history_saver=lambda vod_id, payload: save_calls.append((vod_id, payload)),
    )

    request = controller.build_request("1-5001")
    first_item = request.playlist[0]

    request.playback_history_loader()
    request.playback_history_saver({"position": 45000})
    request.playback_loader(first_item)
    request.playback_progress_reporter(first_item, 2000, False)
    request.playback_stopper(first_item)

    assert request.source_kind == "feiniu"
    assert request.use_local_history is False
    assert load_calls == ["1-5001"]
    assert save_calls == [("1-5001", {"position": 45000})]
    assert api.playback_source_calls == ["1-5002"]
    assert api.playback_progress_calls == [("1-5002", 2000)]
    assert api.playback_stop_calls == ["1-5002"]
```

- [ ] **Step 2: Run the failing controller tests**

Run:

```bash
uv run pytest tests/test_feiniu_controller.py -q
```

Expected: FAIL with `ModuleNotFoundError` because `atv_player.controllers.feiniu_controller` does not exist yet.

- [ ] **Step 3: Add the minimal Feiniu controller**

Create `src/atv_player/controllers/feiniu_controller.py` as a direct Emby-style implementation with Feiniu-specific API method names and `source_kind="feiniu"`:

```python
from __future__ import annotations

import json
from collections.abc import Callable

from atv_player.controllers.browse_controller import _map_vod_item
from atv_player.controllers.douban_controller import _map_categories, _map_item
from atv_player.controllers.telegram_search_controller import _parse_playlist
from atv_player.models import DoubanCategory, HistoryRecord, OpenPlayerRequest, PlayItem, VodItem


class FeiniuController:
    _PAGE_SIZE = 30

    def __init__(
        self,
        api_client,
        playback_history_loader: Callable[[str], HistoryRecord | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._api_client = api_client
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver

    def load_categories(self) -> list[DoubanCategory]:
        payload = self._api_client.list_feiniu_categories()
        categories = _map_categories(payload)
        categories = [category for category in categories if category.type_id != "0"]
        return [DoubanCategory(type_id="0", type_name="推荐"), *categories]

    def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_feiniu_items(category_id, page=page, filters=filters)
        items = [self._decorate_card_subtitle(_map_item(item)) for item in payload.get("list", [])]
        total_raw = payload.get("total")
        total = int(total_raw) if total_raw is not None else int(payload.get("pagecount") or 0) * self._PAGE_SIZE
        return items, total

    def load_folder_items(self, vod_id: str) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_feiniu_items(vod_id, page=1)
        items = [self._decorate_card_subtitle(_map_item(item)) for item in payload.get("list", [])]
        total_raw = payload.get("total")
        total = int(total_raw) if total_raw is not None else len(items)
        return items, total

    def load_playback_item(self, item: PlayItem) -> None:
        payload = self._api_client.get_feiniu_playback_source(item.vod_id)
        raw_url = payload.get("url")
        if isinstance(raw_url, list):
            candidates = [str(value or "").strip() for index, value in enumerate(raw_url) if index % 2 == 1]
            play_url = next((candidate for candidate in candidates if candidate), "")
        else:
            play_url = str(raw_url or "")
        if not play_url:
            raise ValueError(f"没有可用的播放地址: {item.title}")
        headers = payload.get("header") or {}
        if isinstance(headers, str):
            try:
                parsed_headers = json.loads(headers)
            except json.JSONDecodeError:
                parsed_headers = {}
            headers = parsed_headers if isinstance(parsed_headers, dict) else {}
        item.url = play_url
        item.headers = {str(key): str(value) for key, value in headers.items()}

    def report_playback_progress(self, item: PlayItem, position_ms: int, paused: bool) -> None:
        if item.vod_id:
            self._api_client.report_feiniu_playback_progress(item.vod_id, position_ms)

    def stop_playback(self, item: PlayItem) -> None:
        if item.vod_id:
            self._api_client.stop_feiniu_playback(item.vod_id)
```
Add the same `_build_playlist()` and `build_request()` shape used by `EmbyController`, but with Feiniu-specific API calls and metadata:

```python
    def _build_playlist(self, detail: VodItem) -> list[PlayItem]:
        playlist = _parse_playlist(detail.vod_play_url)
        if len(playlist) == 1 and not playlist[0].vod_id:
            playlist[0].title = detail.vod_name or playlist[0].title
            playlist[0].vod_id = detail.vod_play_url.strip() or detail.vod_id
        if not playlist and detail.vod_play_url:
            playlist = [
                PlayItem(
                    title=detail.vod_name or detail.vod_play_url,
                    url="",
                    vod_id=detail.vod_play_url.strip() or detail.vod_id,
                )
            ]
        return playlist

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        payload = self._api_client.get_feiniu_detail(vod_id)
        detail = _map_vod_item(payload["list"][0])
        playlist = self._build_playlist(detail)
        if not playlist and detail.items:
            playlist = list(detail.items)
        if not playlist:
            raise ValueError(f"没有可播放的项目: {detail.vod_name}")
        history_loader = None
        history_saver = None
        if self._playback_history_loader is not None:
            history_loader = lambda source_vod_id=detail.vod_id: self._playback_history_loader(source_vod_id)
        if self._playback_history_saver is not None:
            history_saver = lambda payload, source_vod_id=detail.vod_id: self._playback_history_saver(
                source_vod_id,
                payload,
            )
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            clicked_index=0,
            source_kind="feiniu",
            source_mode="detail",
            source_vod_id=detail.vod_id,
            use_local_history=False,
            detail_resolver=self.resolve_playlist_item,
            playback_loader=self.load_playback_item,
            playback_progress_reporter=self.report_playback_progress,
            playback_stopper=self.stop_playback,
            playback_history_loader=history_loader,
            playback_history_saver=history_saver,
        )
```

- [ ] **Step 4: Run the Feiniu controller tests and the neighboring regression tests**

Run:

```bash
uv run pytest tests/test_feiniu_controller.py tests/test_emby_controller.py tests/test_jellyfin_controller.py -q
```

Expected: PASS, proving the new controller works without regressing the existing mirrored controllers.

- [ ] **Step 5: Commit the controller slice**

Run:

```bash
git add src/atv_player/controllers/feiniu_controller.py tests/test_feiniu_controller.py
git commit -m "feat: add feiniu controller"
```

### Task 3: Wire Feiniu Into App Startup And Main Window

**Files:**
- Modify: `src/atv_player/app.py`
- Modify: `src/atv_player/ui/main_window.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write the failing app and main-window tests**

In `tests/test_app.py`, add a fake controller and Feiniu tab assertions next to the Emby/Jellyfin coverage:

```python
class FakeFeiniuController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self.folder_calls: list[str] = []

    def build_request(self, vod_id: str):
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="Feiniu Movie"),
            playlist=[PlayItem(title="Episode 1", url="", vod_id="ep-feiniu-1")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )

    def load_folder_items(self, vod_id: str):
        self.folder_calls.append(vod_id)
        return [VodItem(vod_id="fn-child-1", vod_name="Episode 1", vod_tag="file")], 1
```

Add or update these tests:

```python
def test_main_window_hides_emby_jellyfin_and_feiniu_tabs_when_disabled(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        feiniu_controller=FakeFeiniuController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_emby_tab=False,
        show_jellyfin_tab=False,
        show_feiniu_tab=False,
    )

    qtbot.addWidget(window)
    window.show()

    assert [window.nav_tabs.tabText(index) for index in range(window.nav_tabs.count())] == [
        "豆瓣电影",
        "电报影视",
        "网络直播",
        "文件浏览",
        "播放记录",
    ]


def test_main_window_enables_search_controls_for_feiniu_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        feiniu_controller=FakeFeiniuController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )

    qtbot.addWidget(window)

    assert window.feiniu_page.keyword_edit.isHidden() is False


def test_main_window_opens_player_from_feiniu_card_signal(qtbot, monkeypatch) -> None:
    controller = FakeFeiniuController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        feiniu_controller=controller,
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[tuple[OpenPlayerRequest, bool]] = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append((request, restore_paused_state)))

    window.feiniu_page.item_open_requested.emit(VodItem(vod_id="1-5001", vod_name="Episode 1", vod_tag="file"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)
    assert opened[0][0].vod.vod_name == "Feiniu Movie"
    assert opened[0][0].source_vod_id == "1-5001"
    assert opened[0][1] is False


def test_main_window_feiniu_folder_click_loads_folder_in_current_tab(qtbot, monkeypatch) -> None:
    controller = FakeFeiniuController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        feiniu_controller=controller,
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
        window.feiniu_page,
        "show_items",
        lambda items, total, page=1, empty_message="当前分类暂无内容": shown.append((items, total, page, empty_message)),
    )

    window.feiniu_page.item_open_requested.emit(VodItem(vod_id="folder-1", vod_name="Season 1", vod_tag="folder"))

    qtbot.waitUntil(lambda: controller.folder_calls == ["folder-1"] and len(shown) == 1, timeout=1000)
    assert opened == []
    assert shown[0][1:] == (1, 1, "当前文件夹暂无内容")
    assert shown[0][0][0].vod_id == "fn-child-1"


def test_main_window_feiniu_breadcrumb_click_loads_category_root(qtbot, monkeypatch) -> None:
    controller = FakeFeiniuController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        feiniu_controller=controller,
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.feiniu_page.ensure_loaded()

    qtbot.waitUntil(lambda: controller.item_calls == [("suggestion", 1)])
    monkeypatch.setattr(window.feiniu_page, "show_items", lambda items, total, page=1, empty_message="当前分类暂无内容": None)
    window.feiniu_page.item_open_requested.emit(VodItem(vod_id="folder-1", vod_name="Season 1", vod_tag="folder"))
    qtbot.waitUntil(lambda: [button.text() for button in window.feiniu_page.breadcrumb_buttons] == ["首页", "推荐", "Season 1"])

    window.feiniu_page.breadcrumb_buttons[1].click()

    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("suggestion", 1))
    qtbot.waitUntil(lambda: [button.text() for button in window.feiniu_page.breadcrumb_buttons] == ["首页", "推荐"])
```

Also extend the coordinator wiring test around `RecordingEmbyController` / `RecordingJellyfinController` with:

```python
    class RecordingFeiniuController:
        def __init__(self, api_client, playback_history_loader=None, playback_history_saver=None) -> None:
            captured["feiniu_loader"] = playback_history_loader
            captured["feiniu_saver"] = playback_history_saver
```

And assert:

```python
    assert callable(captured["feiniu_loader"])
    assert callable(captured["feiniu_saver"])
    assert captured["window_kwargs"]["show_feiniu_tab"] is True
```

- [ ] **Step 2: Run the failing app/main-window tests**

Run:

```bash
uv run pytest \
  tests/test_app.py::test_main_window_hides_emby_jellyfin_and_feiniu_tabs_when_disabled \
  tests/test_app.py::test_main_window_enables_search_controls_for_feiniu_page \
  tests/test_app.py::test_main_window_opens_player_from_feiniu_card_signal \
  tests/test_app.py::test_main_window_feiniu_folder_click_loads_folder_in_current_tab \
  tests/test_app.py::test_main_window_feiniu_breadcrumb_click_loads_category_root \
  tests/test_app.py::test_app_coordinator_shares_playback_history_repository_with_plugins_and_media_sources -q
```

Expected: FAIL because `MainWindow` does not yet accept `feiniu_controller` / `show_feiniu_tab`, and `AppCoordinator` does not yet construct `FeiniuController`.

- [ ] **Step 3: Add the minimal app and main-window wiring**

Update `src/atv_player/app.py`:

```python
from atv_player.controllers.feiniu_controller import FeiniuController

        feiniu_controller = FeiniuController(
            self._api_client,
            playback_history_loader=None
            if self._playback_history_repository is None
            else lambda vod_id: self._playback_history_repository.get_history("feiniu", vod_id),
            playback_history_saver=None
            if self._playback_history_repository is None
            else lambda vod_id, payload: self._playback_history_repository.save_history(
                "feiniu",
                vod_id,
                payload,
                source_name="飞牛影视",
            ),
        )

            feiniu_controller=feiniu_controller,
            show_feiniu_tab=bool(capabilities.get("feiniu")),
```

Update `src/atv_player/ui/main_window.py`:

```python
class _EmptyFeiniuController(_EmptyDoubanController):
    def build_request(self, vod_id: str):
        raise ValueError(f"没有可播放的项目: {vod_id}")


class MainWindow(QMainWindow, AsyncGuardMixin):
    def __init__(
        self,
        browse_controller,
        history_controller,
        player_controller,
        config,
        save_config=None,
        douban_controller=None,
        telegram_controller=None,
        live_controller=None,
        live_source_manager=None,
        emby_controller=None,
        jellyfin_controller=None,
        feiniu_controller=None,
        spider_plugins=None,
        plugin_manager=None,
        drive_detail_loader=None,
        show_emby_tab: bool = True,
        show_jellyfin_tab: bool = True,
        show_feiniu_tab: bool = True,
        m3u8_ad_filter=None,
        playback_parser_service=None,
    ) -> None:
        self.feiniu_page = None
        if show_feiniu_tab:
            self.feiniu_page = PosterGridPage(
                feiniu_controller or _EmptyFeiniuController(),
                click_action="open",
                search_enabled=True,
                folder_navigation_enabled=True,
            )
        self.feiniu_controller = feiniu_controller or _EmptyFeiniuController()
        if self.feiniu_page is not None:
            self.nav_tabs.addTab(self.feiniu_page, "Feiniu")
```

Add the same signal wiring pattern used by Emby and Jellyfin:

```python
        if self.feiniu_page is not None:
            feiniu_page = self.feiniu_page
            feiniu_page.item_open_requested.connect(self._handle_feiniu_item_open_requested)
            feiniu_page.folder_breadcrumb_requested.connect(
                lambda node_id, kind, index, page=feiniu_page: self._handle_media_breadcrumb_requested(
                    page,
                    self.feiniu_controller,
                    node_id,
                    kind,
                    index,
                )
            )
            feiniu_page.unauthorized.connect(self.logout_requested.emit)
```

Add a Feiniu item-open handler that mirrors `_handle_emby_item_open_requested()`:

```python
    def _handle_feiniu_item_open_requested(self, item: VodItem) -> None:
        if item.vod_tag == "folder":
            self._load_media_folder(self.feiniu_page, self.feiniu_controller, item)
            return
        self._open_media_item(self.feiniu_controller, item.vod_id)
```

- [ ] **Step 4: Run the app and main-window tests again**

Run:

```bash
uv run pytest tests/test_app.py -q
```

Expected: PASS, including the new Feiniu tab visibility, open-player, folder-navigation, breadcrumb, and coordinator-wiring tests.

- [ ] **Step 5: Commit the app/main-window slice**

Run:

```bash
git add src/atv_player/app.py src/atv_player/ui/main_window.py tests/test_app.py
git commit -m "feat: add feiniu tab wiring"
```

### Task 4: Add Feiniu History And Label Coverage

**Files:**
- Modify: `src/atv_player/ui/history_page.py`
- Modify: `tests/test_storage.py`
- Modify: `tests/test_history_controller.py`
- Modify: `tests/test_browse_page_ui.py`

- [ ] **Step 1: Write the failing history and storage tests**

Add a Feiniu storage round-trip to `tests/test_storage.py`:

```python
def test_local_playback_history_repository_round_trip_feiniu_source_metadata(tmp_path: Path) -> None:
    from atv_player.local_playback_history import LocalPlaybackHistoryRepository

    repo = LocalPlaybackHistoryRepository(tmp_path / "app.db")
    repo.save_history(
        "feiniu",
        "fn-1",
        {
            "vodName": "Feiniu Movie",
            "vodPic": "poster",
            "vodRemarks": "Episode 2",
            "episode": 1,
            "episodeUrl": "2.m3u8",
            "position": 45000,
            "opening": 0,
            "ending": 0,
            "speed": 1.25,
            "playlistIndex": 1,
            "createTime": 1713206400000,
        },
        source_name="飞牛影视",
    )

    history = repo.get_history("feiniu", "fn-1")

    assert history is not None
    assert history.source_kind == "feiniu"
    assert history.source_name == "飞牛影视"
```

Extend `tests/test_history_controller.py` by adding a Feiniu record to the existing merge/delete flows:

```python
            HistoryRecord(
                id=0,
                key="feiniu-1",
                vod_name="Feiniu Movie",
                vod_pic="fn-pic",
                vod_remarks="Episode 2",
                episode=1,
                episode_url="fn-2.m3u8",
                position=45000,
                opening=0,
                ending=0,
                speed=1.0,
                create_time=275000,
                source_kind="feiniu",
                source_name="飞牛影视",
            ),
```

Update the expectations accordingly, for example:

```python
    assert [record.key for record in records] == ["emby-1", "feiniu-1", "jellyfin-1", "plugin-1", "movie-1"]
```

Extend `tests/test_browse_page_ui.py` to include Feiniu in the history source-label test:

```python
                HistoryRecord(
                    id=0,
                    key="fn-1",
                    vod_name="Feiniu Movie",
                    vod_pic="",
                    vod_remarks="Episode 3",
                    episode=2,
                    episode_url="",
                    position=30000,
                    opening=0,
                    ending=0,
                    speed=1.0,
                    create_time=3,
                    source_kind="feiniu",
                    source_name="飞牛影视",
                ),
```

and assert:

```python
    assert page.table.item(2, 5).text() == "飞牛影视"
```

- [ ] **Step 2: Run the failing history tests**

Run:

```bash
uv run pytest \
  tests/test_storage.py::test_local_playback_history_repository_round_trip_feiniu_source_metadata \
  tests/test_history_controller.py::test_history_controller_merges_remote_and_emby_jellyfin_local_histories \
  tests/test_browse_page_ui.py::test_history_page_formats_emby_and_jellyfin_source_labels -q
```

Expected: FAIL because the history-page label formatter does not yet recognize `source_kind="feiniu"` and the merge expectations have not been updated yet.

- [ ] **Step 3: Add the minimal history-label implementation**

Update `src/atv_player/ui/history_page.py`:

```python
    def _source_label(self, record: HistoryRecord) -> str:
        if record.source_kind == "spider_plugin":
            return record.source_name or record.source_plugin_name or "插件"
        if record.source_kind == "emby":
            return record.source_name or "Emby"
        if record.source_kind == "jellyfin":
            return record.source_name or "Jellyfin"
        if record.source_kind == "feiniu":
            return record.source_name or "飞牛影视"
        return "远程"
```

Keep `HistoryController` and the local playback repository generic; only extend the existing tests so Feiniu proves the current generic code path works.

- [ ] **Step 4: Run the history-focused regression suite**

Run:

```bash
uv run pytest tests/test_storage.py tests/test_history_controller.py tests/test_browse_page_ui.py -q
```

Expected: PASS, including the new Feiniu source-kind coverage.

- [ ] **Step 5: Commit the history slice**

Run:

```bash
git add src/atv_player/ui/history_page.py tests/test_storage.py tests/test_history_controller.py tests/test_browse_page_ui.py
git commit -m "feat: add feiniu history labels"
```

## Self-Review

- Spec coverage:
  - `/feiniu` and `/feiniu-play` API methods are covered in Task 1.
  - Dedicated `FeiniuController` is covered in Task 2.
  - Capability gating, tab creation, startup wiring, and folder/search/playback UI behavior are covered in Task 3.
  - Independent history source labeling and persistence are covered in Task 4.
- Placeholder scan:
  - No `TODO`, `TBD`, or “similar to” shortcuts remain.
  - Every task includes concrete file paths, tests, commands, and code snippets.
- Type consistency:
  - The plan consistently uses `FeiniuController`, `list_feiniu_*`, `get_feiniu_*`, `source_kind="feiniu"`, `show_feiniu_tab`, and `feiniu_controller`.
