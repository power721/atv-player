# Spider Plugin Drive Playlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let spider-plugin play items that contain supported drive-share links resolve through `/tg-search/{token}?ac=gui&id={link}` and play the backend-returned playlist without breaking existing direct URLs or `playerContent()` playback.

**Architecture:** Reuse the existing Telegram-search detail parsing rules instead of inventing a second playlist format. Inject a drive-detail loader from the app layer into spider-plugin controllers, then branch plugin playback resolution so direct media URLs stay unchanged, ordinary plugin ids still use `playerContent()`, and supported drive links resolve lazily through the backend only when the clicked item actually needs playback.

**Tech Stack:** Python, httpx, pytest, existing `ApiClient`, spider-plugin controller flow, Telegram-search detail mapping helpers

---

## File Structure

- Modify: `src/atv_player/api.py`
  Add a dedicated API method for drive-share detail resolution through `/tg-search/{vod_token}`.
- Modify: `src/atv_player/controllers/telegram_search_controller.py`
  Extract a reusable helper that turns a backend detail payload into a playlist using the existing Telegram-search rules.
- Modify: `src/atv_player/plugins/controller.py`
  Detect supported drive-share links, store the injected backend loader, and branch playback resolution between backend drive detail and `playerContent()`.
- Modify: `src/atv_player/plugins/__init__.py`
  Pass the injected drive-detail loader into each `SpiderPluginController`.
- Modify: `src/atv_player/app.py`
  Supply `ApiClient.get_drive_share_detail` when loading enabled spider plugins.
- Modify: `tests/test_api_client.py`
  Cover the new backend request shape for drive-share detail loading.
- Modify: `tests/test_app.py`
  Cover passing the backend drive-detail loader into the plugin-loading path.
- Modify: `tests/test_spider_plugin_controller.py`
  Cover lazy drive-link resolution, mixed playlist behavior, and unchanged `playerContent()` fallback.

### Task 1: Add The Backend Drive-Detail API And Shared Detail-Playlist Helper

**Files:**
- Modify: `src/atv_player/api.py`
- Modify: `src/atv_player/controllers/telegram_search_controller.py`
- Test: `tests/test_api_client.py`
- Test: `tests/test_telegram_search_controller.py`

- [ ] **Step 1: Write the failing API-client test**

Add this test to `tests/test_api_client.py`:

```python
def test_api_client_gets_drive_share_detail() -> None:
    seen = {"path": "", "query": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, json={"list": []})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="auth-123",
        vod_token="Harold",
        transport=httpx.MockTransport(handler),
    )

    client.get_drive_share_detail("https://pan.quark.cn/s/f518510ef92a")

    assert seen == {
        "path": "/tg-search/Harold",
        "query": "id=https%3A%2F%2Fpan.quark.cn%2Fs%2Ff518510ef92a&ac=gui",
    }
```

- [ ] **Step 2: Run the API-client test to verify it fails**

Run:

```bash
uv run pytest tests/test_api_client.py::test_api_client_gets_drive_share_detail -q
```

Expected: FAIL with `AttributeError` because `ApiClient` does not yet expose `get_drive_share_detail()`.

- [ ] **Step 3: Add the API method and shared Telegram-detail playlist helper**

Update `src/atv_player/api.py`:

```python
    def get_drive_share_detail(self, link: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/tg-search/{self._vod_token}",
            params={"id": link, "ac": "gui"},
        )
```

Update `src/atv_player/controllers/telegram_search_controller.py`:

```python
def build_detail_playlist(detail: VodItem) -> list[PlayItem]:
    playlist = _parse_playlist(detail.vod_play_url)
    if not playlist and detail.items:
        playlist = list(detail.items)
    return playlist


class TelegramSearchController:
    ...

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        payload = self._api_client.get_telegram_search_detail(vod_id)
        detail = _map_vod_item(payload["list"][0])
        playlist = build_detail_playlist(detail)
        if not playlist:
            raise ValueError(f"没有可播放的项目: {detail.vod_name}")
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            clicked_index=0,
            source_kind="browse",
            source_mode="detail",
            source_vod_id=detail.vod_id,
            detail_resolver=self.resolve_playlist_item,
        )
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run:

```bash
uv run pytest tests/test_api_client.py::test_api_client_gets_drive_share_detail tests/test_telegram_search_controller.py -q
```

Expected: PASS

- [ ] **Step 5: Commit the API/helper changes**

Run:

```bash
git add tests/test_api_client.py src/atv_player/api.py src/atv_player/controllers/telegram_search_controller.py
git commit -m "feat: add drive share detail api helper"
```

### Task 2: Inject The Drive-Detail Loader Into Spider Plugin Controllers

**Files:**
- Modify: `src/atv_player/plugins/controller.py`
- Modify: `src/atv_player/plugins/__init__.py`
- Modify: `src/atv_player/app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing app wiring test**

Update `tests/test_app.py` by replacing the existing plugin-manager fake inside `test_app_coordinator_passes_loaded_spider_plugins_into_main_window()` with this version:

```python
    captured_loader = {"value": None}

    class FakePluginManager:
        def load_enabled_plugins(self, drive_detail_loader=None):
            captured_loader["value"] = drive_detail_loader
            return loaded_plugins
```

Then add this assertion at the end of the test:

```python
    assert callable(captured_loader["value"])
```

- [ ] **Step 2: Run the app test to verify it fails**

Run:

```bash
uv run pytest tests/test_app.py::test_app_coordinator_passes_loaded_spider_plugins_into_main_window -q
```

Expected: FAIL because `AppCoordinator._show_main()` still calls `load_enabled_plugins()` without the drive-detail loader argument.

- [ ] **Step 3: Thread the backend loader through app and plugin manager**

Update `src/atv_player/plugins/controller.py`:

```python
class SpiderPluginController:
    def __init__(
        self,
        spider,
        plugin_name: str,
        search_enabled: bool,
        drive_detail_loader: Callable[[str], dict] | None = None,
        playback_history_loader: Callable[[str], object | None] | None = None,
        playback_history_saver: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._spider = spider
        self._plugin_name = plugin_name
        self.supports_search = search_enabled
        self._drive_detail_loader = drive_detail_loader
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver
        self._home_loaded = False
        self._home_categories = []
        self._home_items = []
```

Update `src/atv_player/plugins/__init__.py`:

```python
    def load_enabled_plugins(self, drive_detail_loader=None) -> list[SpiderPluginDefinition]:
        definitions: list[SpiderPluginDefinition] = []
        for plugin in self._repository.list_plugins():
            ...
            controller = SpiderPluginController(
                loaded.spider,
                plugin_name=title,
                search_enabled=loaded.search_enabled,
                drive_detail_loader=drive_detail_loader,
                playback_history_loader=lambda vod_id, plugin_id=plugin.id: self._repository.get_playback_history(
                    plugin_id,
                    vod_id,
                ),
                playback_history_saver=lambda vod_id, payload, plugin_id=plugin.id: self._repository.save_playback_history(
                    plugin_id,
                    vod_id,
                    payload,
                ),
            )
```

Update `src/atv_player/app.py`:

```python
class _NullPluginManager:
    def load_enabled_plugins(self, drive_detail_loader=None) -> list:
        del drive_detail_loader
        return []


class AppCoordinator(QObject):
    ...

    def _show_main(self):
        self._api_client = self._build_api_client()
        config = self.repo.load_config()
        capabilities = self._load_capabilities(self._api_client)
        spider_plugins = self._plugin_manager.load_enabled_plugins(
            drive_detail_loader=self._api_client.get_drive_share_detail,
        )
        ...
```

- [ ] **Step 4: Run the app and plugin-manager tests to verify they pass**

Run:

```bash
uv run pytest tests/test_app.py::test_app_coordinator_passes_loaded_spider_plugins_into_main_window tests/test_spider_plugin_manager.py -q
```

Expected: PASS

- [ ] **Step 5: Commit the loader-injection changes**

Run:

```bash
git add tests/test_app.py src/atv_player/app.py src/atv_player/plugins/__init__.py src/atv_player/plugins/controller.py
git commit -m "feat: inject drive detail loader into spider plugins"
```

### Task 3: Resolve Supported Drive Links Lazily In Spider Plugin Playback

**Files:**
- Modify: `src/atv_player/plugins/controller.py`
- Test: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Write the failing spider-plugin tests**

Add these helpers and tests to `tests/test_spider_plugin_controller.py`:

```python
class DriveLinkSpider(FakeSpider):
    def __init__(self) -> None:
        self.player_calls: list[tuple[str, str]] = []

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "网盘剧集",
                    "vod_pic": "poster-detail",
                    "vod_play_from": "网盘线$$$直链线",
                    "vod_play_url": (
                        "第1集$https://pan.quark.cn/s/f518510ef92a$$$"
                        "第2集$https://media.example/2.m3u8"
                    ),
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        self.player_calls.append((flag, id))
        return super().playerContent(flag, id, vipFlags)


def test_controller_resolves_supported_drive_links_via_backend_detail_loader() -> None:
    spider = DriveLinkSpider()
    drive_calls: list[str] = []

    def load_drive_detail(link: str) -> dict:
        drive_calls.append(link)
        return {
            "list": [
                {
                    "vod_id": link,
                    "vod_name": "夸克资源",
                    "vod_play_url": "正片$https://media.example/quark-1.m3u8",
                }
            ]
        }

    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
    )

    request = controller.build_request("/detail/drive")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert drive_calls == ["https://pan.quark.cn/s/f518510ef92a"]
    assert spider.player_calls == []
    assert first.url == "https://media.example/quark-1.m3u8"


def test_controller_keeps_player_content_for_non_drive_plugin_ids() -> None:
    spider = FakeSpider()
    drive_calls: list[str] = []
    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=lambda link: drive_calls.append(link) or {"list": []},
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert drive_calls == []
    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.headers == {"Referer": "https://site.example"}
```

- [ ] **Step 2: Run the spider-plugin tests to verify they fail**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py -q
```

Expected: FAIL because `SpiderPluginController._resolve_play_item()` still always routes unresolved values through `playerContent()`.

- [ ] **Step 3: Add supported-drive detection and lazy backend resolution**

Update `src/atv_player/plugins/controller.py`:

```python
from urllib.parse import urlparse

from atv_player.controllers.telegram_search_controller import build_detail_playlist

_SUPPORTED_DRIVE_DOMAINS = (
    "alipan.com",
    "aliyundrive.com",
    "mypikpak.com",
    "xunlei.com",
    "123pan.com",
    "123pan.cn",
    "123684.com",
    "123865.com",
    "123912.com",
    "123592.com",
    "quark.cn",
    "139.com",
    "uc.cn",
    "115.com",
    "115cdn.com",
    "anxia.com",
    "189.cn",
    "baidu.com",
)


def _looks_like_drive_share_link(value: str) -> bool:
    candidate = value.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return False
    hostname = urlparse(candidate).hostname or ""
    hostname = hostname.lower()
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in _SUPPORTED_DRIVE_DOMAINS)
```

Then branch `_resolve_play_item()` like this:

```python
    def _resolve_play_item(self, item: PlayItem) -> None:
        if item.url or not item.vod_id:
            return
        if _looks_like_drive_share_link(item.vod_id):
            if self._drive_detail_loader is None:
                raise ValueError("当前插件未配置网盘解析")
            try:
                payload = self._drive_detail_loader(item.vod_id)
                detail = _map_vod_item(payload["list"][0])
            except (KeyError, IndexError) as exc:
                raise ValueError(f"没有可播放的项目: {item.title or item.vod_id}") from exc
            playlist = build_detail_playlist(detail)
            playable = next((entry for entry in playlist if entry.url), None)
            if playable is None:
                raise ValueError(f"没有可播放的项目: {detail.vod_name or item.title}")
            item.url = playable.url
            item.headers = dict(playable.headers)
            return
        try:
            payload = self._spider.playerContent(item.play_source, item.vod_id, []) or {}
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        url = str(payload.get("url") or "").strip()
        if not _looks_like_media_url(url):
            raise ValueError("插件未返回可播放地址")
        item.url = url
        item.headers = _normalize_headers(payload.get("header"))
```

- [ ] **Step 4: Run the spider-plugin tests to verify they pass**

Run:

```bash
uv run pytest tests/test_spider_plugin_controller.py -q
```

Expected: PASS

- [ ] **Step 5: Run the full related regression suite**

Run:

```bash
uv run pytest tests/test_api_client.py tests/test_app.py tests/test_spider_plugin_controller.py tests/test_telegram_search_controller.py tests/test_spider_plugin_manager.py -q
```

Expected: PASS

- [ ] **Step 6: Commit the playback-resolution changes**

Run:

```bash
git add tests/test_spider_plugin_controller.py src/atv_player/plugins/controller.py
git commit -m "feat: resolve spider drive links via tg search"
```
