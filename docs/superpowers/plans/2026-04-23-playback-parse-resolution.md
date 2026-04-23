# Built-In Playback Parse Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five built-in playback parsers for spider-plugin `parse=1` playback, persist the preferred parser key in app settings, and expose a parser combo box in the player window without adding parser management UI.

**Architecture:** Keep parser definitions in code under a new shared service module, let `SpiderPluginController` delegate unresolved `parse=1` payloads into that service, and thread the same service into `PlayerWindow` so the UI and playback flow use one fixed parser list. Persist only the preferred parser key in `AppConfig` and `SettingsRepository`.

**Tech Stack:** Python 3.12, PySide6, sqlite3, httpx, pytest

---

### Task 1: Persist The Preferred Built-In Parser Key

**Files:**
- Modify: `src/atv_player/models.py:5-25`
- Modify: `src/atv_player/storage.py:20-215`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_settings_repository_round_trip_persists_preferred_parse_key(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        vod_token="vod-123",
        preferred_parse_key="jx2",
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved.preferred_parse_key == "jx2"
    assert saved == config


def test_settings_repository_migrates_missing_preferred_parse_key_column(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE app_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                base_url TEXT NOT NULL,
                username TEXT NOT NULL,
                token TEXT NOT NULL,
                vod_token TEXT NOT NULL,
                last_path TEXT NOT NULL,
                last_active_window TEXT NOT NULL DEFAULT 'main',
                last_playback_source TEXT NOT NULL DEFAULT 'browse',
                last_playback_source_key TEXT NOT NULL DEFAULT '',
                last_playback_mode TEXT NOT NULL DEFAULT '',
                last_playback_path TEXT NOT NULL DEFAULT '',
                last_playback_vod_id TEXT NOT NULL DEFAULT '',
                last_playback_clicked_vod_id TEXT NOT NULL DEFAULT '',
                last_player_paused INTEGER NOT NULL DEFAULT 0,
                player_volume INTEGER NOT NULL DEFAULT 100,
                player_muted INTEGER NOT NULL DEFAULT 0,
                main_window_geometry BLOB,
                player_window_geometry BLOB,
                player_main_splitter_state BLOB,
                browse_content_splitter_state BLOB
            )
            """
        )
        conn.execute(
            """
            INSERT INTO app_config (
                id, base_url, username, token, vod_token, last_path,
                last_active_window, last_playback_source, last_playback_source_key,
                last_playback_mode, last_playback_path, last_playback_vod_id,
                last_playback_clicked_vod_id, last_player_paused, player_volume,
                player_muted, main_window_geometry, player_window_geometry,
                player_main_splitter_state, browse_content_splitter_state
            )
            VALUES (1, 'http://127.0.0.1:4567', 'alice', '', '', '/', 'main', 'browse', '', '', '', '', '', 0, 100, 0, NULL, NULL, NULL, NULL)
            """
        )

    repo = SettingsRepository(db_path)

    assert repo.load_config().preferred_parse_key == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage.py::test_settings_repository_round_trip_persists_preferred_parse_key tests/test_storage.py::test_settings_repository_migrates_missing_preferred_parse_key_column -v`

Expected: FAIL with `TypeError` for unexpected `preferred_parse_key` or missing column handling in `SettingsRepository`.

- [ ] **Step 3: Write minimal implementation**

```python
@dataclass(slots=True)
class AppConfig:
    base_url: str = "http://127.0.0.1:4567"
    username: str = ""
    token: str = ""
    vod_token: str = ""
    last_path: str = "/"
    last_active_window: str = "main"
    last_playback_source: str = "browse"
    last_playback_source_key: str = ""
    last_playback_mode: str = ""
    last_playback_path: str = ""
    last_playback_vod_id: str = ""
    last_playback_clicked_vod_id: str = ""
    last_player_paused: bool = False
    player_volume: int = 100
    player_muted: bool = False
    preferred_parse_key: str = ""
    main_window_geometry: bytes | None = None
    player_window_geometry: bytes | None = None
    player_main_splitter_state: bytes | None = None
    browse_content_splitter_state: bytes | None = None
```

```python
if "preferred_parse_key" not in columns:
    conn.execute(
        "ALTER TABLE app_config ADD COLUMN preferred_parse_key TEXT NOT NULL DEFAULT ''"
    )
```

```python
SELECT
    base_url,
    username,
    token,
    vod_token,
    last_path,
    last_active_window,
    last_playback_source,
    last_playback_source_key,
    last_playback_mode,
    last_playback_path,
    last_playback_vod_id,
    last_playback_clicked_vod_id,
    last_player_paused,
    player_volume,
    player_muted,
    preferred_parse_key,
    main_window_geometry,
    player_window_geometry,
    player_main_splitter_state,
    browse_content_splitter_state
FROM app_config
WHERE id = 1
```

```python
UPDATE app_config
SET
    base_url = ?,
    username = ?,
    token = ?,
    vod_token = ?,
    last_path = ?,
    last_active_window = ?,
    last_playback_source = ?,
    last_playback_source_key = ?,
    last_playback_mode = ?,
    last_playback_path = ?,
    last_playback_vod_id = ?,
    last_playback_clicked_vod_id = ?,
    last_player_paused = ?,
    player_volume = ?,
    player_muted = ?,
    preferred_parse_key = ?,
    main_window_geometry = ?,
    player_window_geometry = ?,
    player_main_splitter_state = ?,
    browse_content_splitter_state = ?
WHERE id = 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage.py::test_settings_repository_round_trip_persists_preferred_parse_key tests/test_storage.py::test_settings_repository_migrates_missing_preferred_parse_key_column -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_storage.py src/atv_player/models.py src/atv_player/storage.py
git commit -m "feat: persist preferred playback parser key"
```

### Task 2: Add The Built-In Playback Parser Service

**Files:**
- Create: `src/atv_player/playback_parsers.py`
- Test: `tests/test_playback_parsers.py`

- [ ] **Step 1: Write the failing tests**

```python
import httpx

from atv_player.playback_parsers import BuiltInPlaybackParserService


def test_parser_service_tries_saved_parser_first_and_falls_back() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: float, follow_redirects: bool):
        calls.append((url, headers))
        if "sspa8.top:8100/api/?key=1060089351&" in url:
            return httpx.Response(200, json={"parse": 1, "jx": 1, "url": "https://page.example/watch"})
        if "kalbim.xatut.top/kalbim2025/781718/play/video_player.php" in url:
            return httpx.Response(200, json={"parse": 1, "jx": 1, "url": "https://page.example/watch"})
        return httpx.Response(200, json={"parse": 0, "jx": 0, "url": "https://media.example/real.m3u8"})

    service = BuiltInPlaybackParserService(get=fake_get)

    result = service.resolve("qq", "https://site.example/play?id=1", preferred_key="jx1")

    assert result.parser_key == "jx2"
    assert result.url == "https://media.example/real.m3u8"
    assert [url for url, _headers in calls][:3] == [
        "http://sspa8.top:8100/api/?key=1060089351&",
        "https://kalbim.xatut.top/kalbim2025/781718/play/video_player.php",
        "http://sspa8.top:8100/api/?cat_ext=eyJmbGFnIjpbInFxIiwi6IW+6K6vIiwicWl5aSIsIueIseWlh+iJuiIsIuWlh+iJuiIsInlvdWt1Iiwi5LyY6YW3Iiwic29odSIsIuaQnOeLkCIsImxldHYiLCLkuZDop4YiLCJtZ3R2Iiwi6IqS5p6cIiwidG5tYiIsInNldmVuIiwiYmlsaWJpbGkiLCIxOTA1Il0sImhlYWRlciI6eyJVc2VyLUFnZW50Ijoib2todHRwLzQuOS4xIn19&key=星睿4k&",
    ]


def test_parser_service_uses_response_headers_payload() -> None:
    def fake_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: float, follow_redirects: bool):
        return httpx.Response(
            200,
            json={
                "parse": 0,
                "jx": 0,
                "url": "https://media.example/real.m3u8",
                "header": {"Referer": "https://site.example"},
            },
        )

    service = BuiltInPlaybackParserService(get=fake_get)

    result = service.resolve("qq", "https://site.example/play?id=2", preferred_key="fish")

    assert result.parser_key == "fish"
    assert result.headers == {"Referer": "https://site.example"}


def test_parser_service_raises_when_all_parsers_fail() -> None:
    def fake_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: float, follow_redirects: bool):
        return httpx.Response(200, json={"parse": 1, "jx": 1, "url": "https://page.example/watch"})

    service = BuiltInPlaybackParserService(get=fake_get)

    with pytest.raises(ValueError, match="解析失败"):
        service.resolve("qq", "https://site.example/play?id=3")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_playback_parsers.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'atv_player.playback_parsers'`.

- [ ] **Step 3: Write minimal implementation**

```python
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx


def _looks_like_media_url(value: str) -> bool:
    candidate = value.strip().lower()
    return candidate.startswith(("http://", "https://", "rtmp://", "rtsp://")) or any(
        candidate.endswith(ext) or f"{ext}?" in candidate for ext in (".m3u8", ".mp4", ".flv")
    )


def _normalize_headers(raw_headers) -> dict[str, str]:
    if not raw_headers:
        return {}
    if isinstance(raw_headers, Mapping):
        return {str(key): str(value) for key, value in raw_headers.items()}
    if isinstance(raw_headers, str):
        try:
            parsed = json.loads(raw_headers)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return {str(key): str(value) for key, value in parsed.items()}
    return {}


@dataclass(frozen=True, slots=True)
class BuiltInPlaybackParser:
    key: str
    label: str
    api: str
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class BuiltInPlaybackParserResult:
    parser_key: str
    parser_label: str
    url: str
    headers: dict[str, str]


class BuiltInPlaybackParserService:
    def __init__(self, get: Callable[..., httpx.Response] = httpx.get) -> None:
        self._get = get
        self._parsers = [
            BuiltInPlaybackParser(
                key="fish",
                label="fish",
                api="https://kalbim.xatut.top/kalbim2025/781718/play/video_player.php",
                headers={"user-agent": "Mozilla/5.0 (Linux; Android 10; MI 8 Build/QKQ1.190828.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/83.0.4103.101 Mobile Safari/537.36 bsl/1.0;webank/h5face;webank/2.0"},
            ),
            BuiltInPlaybackParser(
                key="jx1",
                label="jx1",
                api="http://sspa8.top:8100/api/?key=1060089351&",
                headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.57"},
            ),
            BuiltInPlaybackParser(
                key="jx2",
                label="jx2",
                api="http://sspa8.top:8100/api/?cat_ext=eyJmbGFnIjpbInFxIiwi6IW+6K6vIiwicWl5aSIsIueIseWlh+iJuiIsIuWlh+iJuiIsInlvdWt1Iiwi5LyY6YW3Iiwic29odSIsIuaQnOeLkCIsImxldHYiLCLkuZDop4YiLCJtZ3R2Iiwi6IqS5p6cIiwidG5tYiIsInNldmVuIiwiYmlsaWJpbGkiLCIxOTA1Il0sImhlYWRlciI6eyJVc2VyLUFnZW50Ijoib2todHRwLzQuOS4xIn19&key=星睿4k&",
                headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.57"},
            ),
            BuiltInPlaybackParser(
                key="mg1",
                label="mg1",
                api="http://shybot.top/v2/video/jx/?shykey=4595a71a4e7712568edcfa43949236b42fcfcb04997788ebe7984d6da2c6a51c&qn=max&",
                headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.57"},
            ),
            BuiltInPlaybackParser(
                key="tx1",
                label="tx1",
                api="http://shybot.top/v2/video/jx/?shykey=4595a71a4e7712568edcfa43949236b42fcfcb04997788ebe7984d6da2c6a51c&",
                headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.57"},
            ),
        ]

    def parsers(self) -> list[BuiltInPlaybackParser]:
        return list(self._parsers)

    def resolve(self, flag: str, url: str, preferred_key: str = "") -> BuiltInPlaybackParserResult:
        if not url.strip():
            raise ValueError("解析失败: 缺少待解析地址")
        ordered = self._ordered_parsers(preferred_key)
        errors: list[str] = []
        for parser in ordered:
            try:
                response = self._get(
                    parser.api,
                    params={"flag": flag, "url": url},
                    headers=dict(parser.headers),
                    timeout=15.0,
                    follow_redirects=True,
                )
                payload = response.json()
                media_url = str(payload.get("url") or "").strip()
                if payload.get("parse") == 0 or payload.get("jx") == 0 or _looks_like_media_url(media_url):
                    if not _looks_like_media_url(media_url):
                        raise ValueError("返回地址不可播放")
                    return BuiltInPlaybackParserResult(
                        parser_key=parser.key,
                        parser_label=parser.label,
                        url=media_url,
                        headers=_normalize_headers(payload.get("header") or payload.get("headers")),
                    )
                raise ValueError("返回结果仍需解析")
            except Exception as exc:
                errors.append(f"{parser.key}: {exc}")
        raise ValueError(f"解析失败: {'; '.join(errors)}")

    def _ordered_parsers(self, preferred_key: str) -> list[BuiltInPlaybackParser]:
        if not preferred_key:
            return self.parsers()
        preferred = [parser for parser in self._parsers if parser.key == preferred_key]
        remaining = [parser for parser in self._parsers if parser.key != preferred_key]
        return [*preferred, *remaining]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_playback_parsers.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_playback_parsers.py src/atv_player/playback_parsers.py
git commit -m "feat: add built-in playback parser service"
```

### Task 3: Route Spider Plugin `parse=1` Playback Through The Parser Service

**Files:**
- Modify: `src/atv_player/plugins/controller.py:112-340`
- Test: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Write the failing tests**

```python
class ParseRequiredSpider(FakeSpider):
    def playerContent(self, flag, id, vipFlags):
        return {"parse": 1, "url": f"https://page.example{id}"}


def test_controller_resolves_parse_required_player_content_via_parser_service() -> None:
    parser_calls: list[tuple[str, str, str]] = []

    class FakeParserService:
        def resolve(self, flag: str, url: str, preferred_key: str = ""):
            parser_calls.append((flag, url, preferred_key))
            return type(
                "Result",
                (),
                {
                    "parser_key": "jx2",
                    "parser_label": "jx2",
                    "url": "https://media.example/resolved.m3u8",
                    "headers": {"Referer": "https://page.example"},
                },
            )()

    controller = SpiderPluginController(
        ParseRequiredSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        playback_parser_service=FakeParserService(),
        preferred_parse_key_loader=lambda: "jx1",
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]
    assert request.playback_loader is not None

    request.playback_loader(first)

    assert parser_calls == [("备用线", "https://page.example/play/1", "jx1")]
    assert first.url == "https://media.example/resolved.m3u8"
    assert first.headers == {"Referer": "https://page.example"}


def test_controller_raises_when_parse_required_without_parser_service() -> None:
    controller = SpiderPluginController(ParseRequiredSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("/detail/1")

    with pytest.raises(ValueError, match="当前插件未配置内置解析"):
        assert request.playback_loader is not None
        request.playback_loader(request.playlist[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_spider_plugin_controller.py::test_controller_resolves_parse_required_player_content_via_parser_service tests/test_spider_plugin_controller.py::test_controller_raises_when_parse_required_without_parser_service -v`

Expected: FAIL because `SpiderPluginController.__init__()` does not accept parser service hooks and `parse=1` currently falls through to `插件未返回可播放地址`.

- [ ] **Step 3: Write minimal implementation**

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
        playback_parser_service=None,
        preferred_parse_key_loader: Callable[[], str] | None = None,
    ) -> None:
        self._spider = spider
        self._plugin_name = plugin_name
        self.supports_search = search_enabled
        self._drive_detail_loader = drive_detail_loader
        self._playback_history_loader = playback_history_loader
        self._playback_history_saver = playback_history_saver
        self._playback_parser_service = playback_parser_service
        self._preferred_parse_key_loader = preferred_parse_key_loader
        self._home_loaded = False
        self._home_categories = []
        self._home_items = []
```

```python
payload = self._spider.playerContent(item.play_source, item.vod_id, []) or {}
parse_required = int(payload.get("parse") or 0) == 1
url = str(payload.get("url") or "").strip()
if parse_required:
    if self._playback_parser_service is None:
        raise ValueError("当前插件未配置内置解析")
    result = self._playback_parser_service.resolve(
        item.play_source,
        url,
        preferred_key="" if self._preferred_parse_key_loader is None else self._preferred_parse_key_loader(),
    )
    item.url = result.url
    item.headers = dict(result.headers)
    return None
if not _looks_like_media_url(url):
    raise ValueError("插件未返回可播放地址")
item.url = url
item.headers = _normalize_headers(payload.get("header"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_spider_plugin_controller.py::test_controller_resolves_parse_required_player_content_via_parser_service tests/test_spider_plugin_controller.py::test_controller_raises_when_parse_required_without_parser_service -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_spider_plugin_controller.py src/atv_player/plugins/controller.py
git commit -m "feat: resolve spider parse playback via built-in parsers"
```

### Task 4: Wire The Parser Service Into App Startup And Player Window UI

**Files:**
- Modify: `src/atv_player/app.py:111-240`
- Modify: `src/atv_player/ui/main_window.py:124-240`
- Modify: `src/atv_player/ui/player_window.py:260-520`
- Test: `tests/test_main_window_ui.py`
- Test: `tests/test_player_window_ui.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_main_window_keeps_existing_header_buttons_without_parse_manager(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.plugin_manager_button.text() == "插件管理"
    assert window.live_source_manager_button.text() == "直播源管理"
    assert not hasattr(window, "parse_manager_button")
```

```python
def test_player_window_exposes_parse_combo_with_builtin_entries(qtbot) -> None:
    class FakeParserService:
        def parsers(self):
            return [
                type("Parser", (), {"key": "fish", "label": "fish"})(),
                type("Parser", (), {"key": "jx1", "label": "jx1"})(),
                type("Parser", (), {"key": "jx2", "label": "jx2"})(),
                type("Parser", (), {"key": "mg1", "label": "mg1"})(),
                type("Parser", (), {"key": "tx1", "label": "tx1"})(),
            ]

    window = PlayerWindow(FakePlayerController(), config=AppConfig(), playback_parser_service=FakeParserService())

    qtbot.addWidget(window)

    assert window.parse_combo.count() == 6
    assert window.parse_combo.itemText(0) == "解析"
    assert [window.parse_combo.itemText(index) for index in range(1, window.parse_combo.count())] == [
        "fish",
        "jx1",
        "jx2",
        "mg1",
        "tx1",
    ]
```

```python
def test_player_window_saves_preferred_parse_key_when_user_selects_parser(qtbot) -> None:
    saved = {"called": 0}
    config = AppConfig()

    class FakeParserService:
        def parsers(self):
            return [
                type("Parser", (), {"key": "fish", "label": "fish"})(),
                type("Parser", (), {"key": "jx1", "label": "jx1"})(),
            ]

    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("called", saved["called"] + 1),
        playback_parser_service=FakeParserService(),
    )
    qtbot.addWidget(window)

    window.parse_combo.setCurrentIndex(2)

    assert config.preferred_parse_key == "jx1"
    assert saved["called"] == 1
```

```python
def test_app_coordinator_passes_playback_parser_service_into_main_window(qtbot, monkeypatch, tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(base_url="http://127.0.0.1:4567", token="token-123", vod_token="vod-123"))
    captured = {"parser_service": None}

    class FakeMainWindow:
        def __init__(self, *args, **kwargs) -> None:
            captured["parser_service"] = kwargs.get("playback_parser_service")

    class FakePluginManager:
        def load_enabled_plugins(self, drive_detail_loader=None):
            return []

    def api_factory(*args, **kwargs):
        return ApiClient(
            "http://127.0.0.1:4567",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"token": "vod-123"})),
        )

    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(app_module, "ApiClient", api_factory)
    monkeypatch.setattr(app_module, "SpiderPluginManager", lambda repository, loader, playback_history_repository: FakePluginManager())
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())
    monkeypatch.setattr(app_module, "LocalPlaybackHistoryRepository", lambda db_path: object())

    coordinator = AppCoordinator(repo)
    coordinator._show_main()

    assert captured["parser_service"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main_window_ui.py::test_main_window_keeps_existing_header_buttons_without_parse_manager tests/test_player_window_ui.py::test_player_window_exposes_parse_combo_with_builtin_entries tests/test_player_window_ui.py::test_player_window_saves_preferred_parse_key_when_user_selects_parser tests/test_app.py::test_app_coordinator_passes_playback_parser_service_into_main_window -v`

Expected: FAIL because `PlayerWindow` and `MainWindow` do not accept `playback_parser_service`, and no parse combo exists.

- [ ] **Step 3: Write minimal implementation**

```python
class MainWindow(QMainWindow):
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
        spider_plugins=None,
        plugin_manager=None,
        drive_detail_loader=None,
        show_emby_tab: bool = True,
        show_jellyfin_tab: bool = True,
        m3u8_ad_filter=None,
        playback_parser_service=None,
    ) -> None:
        super().__init__()
        self._playback_parser_service = playback_parser_service
```

```python
if self.player_window is None:
    self.player_window = PlayerWindow(
        self.player_controller,
        config=self.config,
        save_config=self._save_config,
        m3u8_ad_filter=self._m3u8_ad_filter,
        playback_parser_service=self._playback_parser_service,
    )
```

```python
class PlayerWindow(QWidget):
    def __init__(self, controller, config=None, save_config=None, m3u8_ad_filter=None, playback_parser_service=None) -> None:
        super().__init__()
        self.controller = controller
        self.config = config
        self._save_config = save_config or (lambda: None)
        self._m3u8_ad_filter = m3u8_ad_filter or M3U8AdFilter()
        self._playback_parser_service = playback_parser_service
        self.parse_combo = QComboBox()
        self.parse_combo.addItem("解析", "")
        for parser in [] if self._playback_parser_service is None else self._playback_parser_service.parsers():
            self.parse_combo.addItem(parser.label, parser.key)
        preferred_parse_key = "" if self.config is None else getattr(self.config, "preferred_parse_key", "")
        index = self.parse_combo.findData(preferred_parse_key)
        self.parse_combo.setCurrentIndex(index if index >= 0 else 0)
```

```python
control_group_layout.addWidget(self.audio_combo)
control_group_layout.addWidget(self.parse_combo)
control_group_layout.addWidget(self.opening_spin)
```

```python
self.parse_combo.currentIndexChanged.connect(self._change_parse_selection)

def _change_parse_selection(self, index: int) -> None:
    if self.config is None:
        return
    parser_key = str(self.parse_combo.itemData(index) or "")
    if getattr(self.config, "preferred_parse_key", "") == parser_key:
        return
    self.config.preferred_parse_key = parser_key
    self._save_config()
```

```python
self._playback_parser_service = BuiltInPlaybackParserService()
```

```python
self.main_window = MainWindow(
    browse_controller=browse_controller,
    history_controller=history_controller,
    player_controller=player_controller,
    config=config,
    save_config=lambda: self.repo.save_config(config),
    douban_controller=douban_controller,
    telegram_controller=telegram_controller,
    live_controller=live_controller,
    live_source_manager=live_source_manager,
    emby_controller=emby_controller,
    jellyfin_controller=jellyfin_controller,
    spider_plugins=spider_plugins,
    plugin_manager=self._plugin_manager,
    drive_detail_loader=drive_detail_loader,
    show_emby_tab=bool(capabilities.get("emby")),
    show_jellyfin_tab=bool(capabilities.get("jellyfin")),
    m3u8_ad_filter=self._m3u8_ad_filter,
    playback_parser_service=self._playback_parser_service,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main_window_ui.py::test_main_window_keeps_existing_header_buttons_without_parse_manager tests/test_player_window_ui.py::test_player_window_exposes_parse_combo_with_builtin_entries tests/test_player_window_ui.py::test_player_window_saves_preferred_parse_key_when_user_selects_parser tests/test_app.py::test_app_coordinator_passes_playback_parser_service_into_main_window -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_main_window_ui.py tests/test_player_window_ui.py tests/test_app.py src/atv_player/app.py src/atv_player/ui/main_window.py src/atv_player/ui/player_window.py
git commit -m "feat: expose built-in playback parser preference in player"
```

### Task 5: Run Focused Regression Verification

**Files:**
- Test: `tests/test_storage.py`
- Test: `tests/test_playback_parsers.py`
- Test: `tests/test_spider_plugin_controller.py`
- Test: `tests/test_main_window_ui.py`
- Test: `tests/test_player_window_ui.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Run the focused parser and UI suite**

Run: `uv run pytest tests/test_storage.py tests/test_playback_parsers.py tests/test_spider_plugin_controller.py tests/test_main_window_ui.py tests/test_player_window_ui.py tests/test_app.py -v`

Expected: PASS

- [ ] **Step 2: If a regression appears, add the narrowest failing test first**

```python
def test_player_window_restores_saved_preferred_parse_key_on_init(qtbot) -> None:
    class FakeParserService:
        def parsers(self):
            return [
                type("Parser", (), {"key": "fish", "label": "fish"})(),
                type("Parser", (), {"key": "jx1", "label": "jx1"})(),
            ]

    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(preferred_parse_key="jx1"),
        playback_parser_service=FakeParserService(),
    )
    qtbot.addWidget(window)

    assert window.parse_combo.currentData() == "jx1"
```

- [ ] **Step 3: Re-run the narrow and focused suite until green**

Run: `uv run pytest tests/test_storage.py tests/test_playback_parsers.py tests/test_spider_plugin_controller.py tests/test_main_window_ui.py tests/test_player_window_ui.py tests/test_app.py -v`

Expected: PASS

- [ ] **Step 4: Commit the final verified state**

```bash
git add tests/test_storage.py tests/test_playback_parsers.py tests/test_spider_plugin_controller.py tests/test_main_window_ui.py tests/test_player_window_ui.py tests/test_app.py src/atv_player/models.py src/atv_player/storage.py src/atv_player/playback_parsers.py src/atv_player/plugins/controller.py src/atv_player/app.py src/atv_player/ui/main_window.py src/atv_player/ui/player_window.py
git commit -m "feat: add built-in spider playback parsers"
```
