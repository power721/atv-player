# Python Spider Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add TVBox-style Python spider plugins that can be loaded from local files or remote URLs, managed inside the app, and exposed as dynamic home tabs with category browsing, search, detail playback, and deferred `playerContent` resolution.

**Architecture:** Persist plugin metadata and logs in SQLite, load plugin code through a compatibility shim plus a dedicated loader, adapt spider methods into the existing `PosterGridPage` and `OpenPlayerRequest` flow, and wire a plugin manager dialog plus dynamic tab assembly through `AppCoordinator` and `MainWindow`.

**Tech Stack:** Python, PySide6, httpx, sqlite3, pytest, pytest-qt

---

## File Structure

- `src/atv_player/models.py`
  Add plugin config and log dataclasses, and add a `play_source` field to `PlayItem` so spider playlists can preserve the `vod_play_from` route used by `playerContent`.
- `src/atv_player/storage.py`
  Expose the SQLite database path so the plugin repository can share the same app database.
- `src/atv_player/plugins/__init__.py`
  Export plugin runtime types and a small manager service that coordinates the repository, loader, and controller creation.
- `src/atv_player/plugins/repository.py`
  Create and manage `spider_plugins` and `spider_plugin_logs` tables, plugin CRUD, ordering, enable state, and log persistence.
- `src/atv_player/plugins/compat/__init__.py`
  Package marker for the spider compatibility runtime.
- `src/atv_player/plugins/compat/base/__init__.py`
  Package marker for the `base.spider` import path.
- `src/atv_player/plugins/compat/base/spider.py`
  Provide the TVBox-compatible `Spider` base class with the helper methods used by supported plugins.
- `src/atv_player/plugins/loader.py`
  Load local or remote plugin source files, manage remote cache files, install the `base.spider` compatibility alias, and return live spider instances.
- `src/atv_player/plugins/controller.py`
  Adapt `homeContent`, `categoryContent`, `detailContent`, `playerContent`, and `searchContent` into `DoubanCategory`, `VodItem`, `PlayItem`, and `OpenPlayerRequest`.
- `src/atv_player/ui/plugin_manager_dialog.py`
  Add the plugin management dialog plus a simple per-plugin log viewer.
- `src/atv_player/app.py`
  Construct the plugin repository, loader, and manager; load enabled plugin tabs; pass them into the main window.
- `src/atv_player/ui/main_window.py`
  Add the plugin manager button, build dynamic plugin tabs, rebuild tabs after dialog changes, and open plugin playback requests.
- `tests/test_storage.py`
  Cover repository migration, plugin CRUD, order swapping, and log persistence.
- `tests/test_spider_plugin_loader.py`
  Cover local loading, remote download and cache, refresh fallback, compatibility aliasing, and import failures.
- `tests/test_spider_plugin_controller.py`
  Cover home/category/search/detail mapping, deferred `playerContent` playback resolution, and route-label playlist parsing.
- `tests/test_plugin_manager_dialog.py`
  Cover add, rename, enable or disable, reorder, refresh, and log viewing behavior.
- `tests/test_app.py`
  Cover plugin-manager construction and dynamic plugin definitions flowing into `MainWindow`.
- `tests/test_main_window_ui.py`
  Cover plugin tab ordering, manager-button behavior, and tab rebuilds after dialog-driven changes.

### Task 1: Persist plugin metadata and logs

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/storage.py`
- Create: `src/atv_player/plugins/repository.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_storage.py`:

```python
from atv_player.models import SpiderPluginConfig
from atv_player.plugins.repository import SpiderPluginRepository


def test_spider_plugin_repository_round_trip_and_logs(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SpiderPluginRepository(db_path)

    local_plugin = repo.add_plugin(
        source_type="local",
        source_value="/plugins/红果短剧.py",
        display_name="红果短剧",
    )
    remote_plugin = repo.add_plugin(
        source_type="remote",
        source_value="https://example.com/spiders/hg.py",
        display_name="红果短剧远程",
    )

    repo.update_plugin(
        local_plugin.id,
        display_name="红果短剧本地",
        enabled=False,
        cached_file_path="",
        last_loaded_at=1713206400,
        last_error="缺少依赖: pyquery",
    )
    repo.append_log(local_plugin.id, "error", "缺少依赖: pyquery", created_at=1713206401)
    repo.move_plugin(remote_plugin.id, direction=-1)

    plugins = repo.list_plugins()
    logs = repo.list_logs(local_plugin.id)

    assert [(item.display_name, item.sort_order, item.enabled) for item in plugins] == [
        ("红果短剧远程", 0, True),
        ("红果短剧本地", 1, False),
    ]
    assert plugins[1].last_error == "缺少依赖: pyquery"
    assert logs[0].message == "缺少依赖: pyquery"

    repo.delete_plugin(remote_plugin.id)

    assert [item.display_name for item in repo.list_plugins()] == ["红果短剧本地"]


def test_spider_plugin_repository_migrates_tables_into_existing_settings_db(tmp_path: Path) -> None:
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
                last_path TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO app_config (id, base_url, username, token, vod_token, last_path)
            VALUES (1, 'http://127.0.0.1:4567', '', '', '', '/')
            """
        )

    repo = SpiderPluginRepository(db_path)
    created = repo.add_plugin(
        source_type="local",
        source_value="/plugins/红果短剧.py",
        display_name="红果短剧",
    )

    assert created.id > 0
    assert repo.list_plugins()[0].source_value == "/plugins/红果短剧.py"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage.py -k "spider_plugin" -v`
Expected: FAIL with `ModuleNotFoundError` because `atv_player.plugins.repository` and the plugin dataclasses do not exist yet.

- [ ] **Step 3: Write minimal implementation**

Add these dataclasses to `src/atv_player/models.py`:

```python
@dataclass(slots=True)
class SpiderPluginConfig:
    id: int = 0
    source_type: str = ""
    source_value: str = ""
    display_name: str = ""
    enabled: bool = True
    sort_order: int = 0
    cached_file_path: str = ""
    last_loaded_at: int = 0
    last_error: str = ""


@dataclass(slots=True)
class SpiderPluginLogEntry:
    id: int = 0
    plugin_id: int = 0
    level: str = "info"
    message: str = ""
    created_at: int = 0
```

Extend `PlayItem` in `src/atv_player/models.py` so spider playlists can remember the route passed into `playerContent`:

```python
@dataclass(slots=True)
class PlayItem:
    title: str
    url: str
    path: str = ""
    index: int = 0
    size: int = 0
    vod_id: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    play_source: str = ""
```

Expose the settings database path in `src/atv_player/storage.py`:

```python
@property
def database_path(self) -> Path:
    return self._db_path
```

Create `src/atv_player/plugins/repository.py` with:

```python
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from atv_player.models import SpiderPluginConfig, SpiderPluginLogEntry


class SpiderPluginRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS spider_plugins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_value TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL,
                    cached_file_path TEXT NOT NULL DEFAULT '',
                    last_loaded_at INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS spider_plugin_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plugin_id INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )

    def add_plugin(self, source_type: str, source_value: str, display_name: str) -> SpiderPluginConfig:
        with self._connect() as conn:
            next_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM spider_plugins"
            ).fetchone()[0]
            cursor = conn.execute(
                """
                INSERT INTO spider_plugins (
                    source_type, source_value, display_name, enabled, sort_order,
                    cached_file_path, last_loaded_at, last_error
                )
                VALUES (?, ?, ?, 1, ?, '', 0, '')
                """,
                (source_type, source_value, display_name, next_order),
            )
        return self.get_plugin(int(cursor.lastrowid))

    def get_plugin(self, plugin_id: int) -> SpiderPluginConfig:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, source_type, source_value, display_name, enabled, sort_order,
                       cached_file_path, last_loaded_at, last_error
                FROM spider_plugins
                WHERE id = ?
                """,
                (plugin_id,),
            ).fetchone()
        assert row is not None
        values = list(row)
        values[4] = bool(values[4])
        return SpiderPluginConfig(*values)

    def list_plugins(self) -> list[SpiderPluginConfig]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source_type, source_value, display_name, enabled, sort_order,
                       cached_file_path, last_loaded_at, last_error
                FROM spider_plugins
                ORDER BY sort_order ASC, id ASC
                """
            ).fetchall()
        plugins: list[SpiderPluginConfig] = []
        for row in rows:
            values = list(row)
            values[4] = bool(values[4])
            plugins.append(SpiderPluginConfig(*values))
        return plugins

    def update_plugin(
        self,
        plugin_id: int,
        *,
        display_name: str,
        enabled: bool,
        cached_file_path: str,
        last_loaded_at: int,
        last_error: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE spider_plugins
                SET display_name = ?, enabled = ?, cached_file_path = ?,
                    last_loaded_at = ?, last_error = ?
                WHERE id = ?
                """,
                (display_name, int(enabled), cached_file_path, last_loaded_at, last_error, plugin_id),
            )

    def move_plugin(self, plugin_id: int, direction: int) -> None:
        plugins = self.list_plugins()
        index = next(i for i, item in enumerate(plugins) if item.id == plugin_id)
        target = index + direction
        if not (0 <= target < len(plugins)):
            return
        plugins[index], plugins[target] = plugins[target], plugins[index]
        with self._connect() as conn:
            for order, item in enumerate(plugins):
                conn.execute(
                    "UPDATE spider_plugins SET sort_order = ? WHERE id = ?",
                    (order, item.id),
                )

    def delete_plugin(self, plugin_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM spider_plugin_logs WHERE plugin_id = ?", (plugin_id,))
            conn.execute("DELETE FROM spider_plugins WHERE id = ?", (plugin_id,))

    def append_log(self, plugin_id: int, level: str, message: str, created_at: int | None = None) -> None:
        timestamp = int(time.time()) if created_at is None else created_at
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO spider_plugin_logs (plugin_id, level, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (plugin_id, level, message, timestamp),
            )

    def list_logs(self, plugin_id: int) -> list[SpiderPluginLogEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, plugin_id, level, message, created_at
                FROM spider_plugin_logs
                WHERE plugin_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (plugin_id,),
            ).fetchall()
        return [SpiderPluginLogEntry(*row) for row in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage.py -k "spider_plugin" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_storage.py src/atv_player/models.py src/atv_player/storage.py src/atv_player/plugins/repository.py
git commit -m "test: add spider plugin repository"
```

### Task 2: Load local and remote spider files through the compatibility shim

**Files:**
- Create: `src/atv_player/plugins/compat/__init__.py`
- Create: `src/atv_player/plugins/compat/base/__init__.py`
- Create: `src/atv_player/plugins/compat/base/spider.py`
- Create: `src/atv_player/plugins/loader.py`
- Modify: `src/atv_player/plugins/__init__.py`
- Create: `tests/test_spider_plugin_loader.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_spider_plugin_loader.py` with:

```python
from pathlib import Path

import httpx

from atv_player.models import SpiderPluginConfig
from atv_player.plugins.loader import SpiderPluginLoader


PLUGIN_SOURCE = """
from base.spider import Spider

class Spider(Spider):
    def init(self, extend=""):
        self.extend = extend

    def getName(self):
        return "红果短剧"

    def homeContent(self, filter):
        return {
            "class": [{"type_id": "hot", "type_name": "热门"}],
            "list": [{"vod_id": "/detail/1", "vod_name": "短剧 1"}],
        }
"""


def test_loader_loads_local_plugin_and_installs_base_spider_alias(tmp_path: Path) -> None:
    plugin_path = tmp_path / "红果短剧.py"
    plugin_path.write_text(PLUGIN_SOURCE, encoding="utf-8")
    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache")
    config = SpiderPluginConfig(
        id=1,
        source_type="local",
        source_value=str(plugin_path),
        display_name="",
        enabled=True,
        sort_order=0,
    )

    loaded = loader.load(config)

    assert loaded.plugin_name == "红果短剧"
    assert loaded.spider.homeContent(False)["class"][0]["type_name"] == "热门"
    assert loaded.search_enabled is False


def test_loader_downloads_remote_plugin_and_reuses_cached_file_on_refresh_failure(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_get(url: str, timeout: float = 15.0) -> httpx.Response:
        calls.append(url)
        if len(calls) == 1:
            return httpx.Response(200, text=PLUGIN_SOURCE)
        raise httpx.ConnectError("network down")

    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache", get=fake_get)
    config = SpiderPluginConfig(
        id=7,
        source_type="remote",
        source_value="https://example.com/红果短剧.py",
        display_name="",
        enabled=True,
        sort_order=0,
    )

    first = loader.load(config, force_refresh=True)
    second = loader.load(first.config, force_refresh=True)

    assert first.plugin_name == "红果短剧"
    assert second.plugin_name == "红果短剧"
    assert calls == [
        "https://example.com/红果短剧.py",
        "https://example.com/红果短剧.py",
    ]


def test_loader_reports_missing_spider_class(tmp_path: Path) -> None:
    bad_path = tmp_path / "bad.py"
    bad_path.write_text("class NotSpider:\\n    pass\\n", encoding="utf-8")
    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache")
    config = SpiderPluginConfig(
        id=2,
        source_type="local",
        source_value=str(bad_path),
        display_name="坏插件",
        enabled=True,
        sort_order=0,
    )

    with pytest.raises(ValueError, match="缺少 Spider 类"):
        loader.load(config)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_spider_plugin_loader.py -v`
Expected: FAIL with `ModuleNotFoundError` because the loader and compatibility files do not exist yet.

- [ ] **Step 3: Write minimal implementation**

Create `src/atv_player/plugins/compat/base/spider.py` with:

```python
from __future__ import annotations

import json
import re
from abc import ABCMeta

import httpx


class Spider(metaclass=ABCMeta):
    def __init__(self) -> None:
        self.extend = ""

    def init(self, extend: str = "") -> None:
        self.extend = extend

    def homeContent(self, filter):
        return {"class": [], "list": []}

    def categoryContent(self, tid, pg, filter, extend):
        return {"list": [], "page": pg, "pagecount": 1, "total": 0}

    def detailContent(self, ids):
        return {"list": []}

    def searchContent(self, key, quick, pg="1"):
        raise NotImplementedError

    def playerContent(self, flag, id, vipFlags):
        raise NotImplementedError

    def getName(self):
        return ""

    def fetch(self, url, params=None, cookies=None, headers=None, timeout=5, verify=True, stream=False, allow_redirects=True):
        response = httpx.get(
            url,
            params=params,
            cookies=cookies,
            headers=headers,
            timeout=timeout,
            follow_redirects=allow_redirects,
        )
        response.encoding = "utf-8"
        return response

    def post(self, url, params=None, data=None, json=None, cookies=None, headers=None, timeout=5, verify=True, stream=False, allow_redirects=True):
        response = httpx.post(
            url,
            params=params,
            data=data,
            json=json,
            cookies=cookies,
            headers=headers,
            timeout=timeout,
            follow_redirects=allow_redirects,
        )
        response.encoding = "utf-8"
        return response

    def regStr(self, reg, src, group=1):
        match = re.search(reg, src)
        return match.group(group) if match else ""

    def removeHtmlTags(self, src):
        return re.sub(re.compile("<.*?>"), "", src)

    def cleanText(self, src):
        return re.sub(
            "[\\U0001F600-\\U0001F64F\\U0001F300-\\U0001F5FF\\U0001F680-\\U0001F6FF\\U0001F1E0-\\U0001F1FF]",
            "",
            src,
        )

    def log(self, msg):
        if isinstance(msg, (dict, list)):
            print(json.dumps(msg, ensure_ascii=False))
            return
        print(str(msg))
```

Create `src/atv_player/plugins/loader.py` with:

```python
from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import httpx

from atv_player.models import SpiderPluginConfig
from atv_player.plugins.compat.base.spider import Spider as CompatSpider


@dataclass(slots=True)
class LoadedSpiderPlugin:
    config: SpiderPluginConfig
    spider: object
    plugin_name: str
    search_enabled: bool


class SpiderPluginLoader:
    def __init__(self, cache_dir: Path, get=httpx.get) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._get = get

    def load(self, config: SpiderPluginConfig, force_refresh: bool = False) -> LoadedSpiderPlugin:
        self._install_compat_modules()
        source_path = self._resolve_source_path(config, force_refresh=force_refresh)
        module_name = f"spider_plugin_{config.id}_{source_path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, source_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"无法加载插件文件: {source_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules.pop(module_name, None)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except ModuleNotFoundError as exc:
            raise ValueError(f"缺少依赖: {exc.name}") from exc
        spider_cls = getattr(module, "Spider", None)
        if spider_cls is None:
            raise ValueError("缺少 Spider 类")
        spider = spider_cls()
        if hasattr(spider, "init"):
            spider.init("")
        plugin_name = str(getattr(spider, "getName", lambda: "")() or "")
        search_enabled = type(spider).searchContent is not CompatSpider.searchContent
        updated_config = SpiderPluginConfig(
            id=config.id,
            source_type=config.source_type,
            source_value=config.source_value,
            display_name=config.display_name,
            enabled=config.enabled,
            sort_order=config.sort_order,
            cached_file_path=str(source_path) if config.source_type == "remote" else config.cached_file_path,
            last_loaded_at=config.last_loaded_at,
            last_error=config.last_error,
        )
        return LoadedSpiderPlugin(updated_config, spider, plugin_name, search_enabled)

    def _install_compat_modules(self) -> None:
        base_package = types.ModuleType("base")
        spider_module = sys.modules["atv_player.plugins.compat.base.spider"]
        base_package.spider = spider_module
        sys.modules["base"] = base_package
        sys.modules["base.spider"] = spider_module

    def _resolve_source_path(self, config: SpiderPluginConfig, force_refresh: bool) -> Path:
        if config.source_type == "local":
            return Path(config.source_value)
        cache_path = self._cache_dir / f"plugin_{config.id}.py"
        if not force_refresh and config.cached_file_path:
            cached = Path(config.cached_file_path)
            if cached.is_file():
                return cached
        try:
            response = self._get(config.source_value, timeout=15.0)
            response.raise_for_status()
            cache_path.write_text(response.text, encoding="utf-8")
            return cache_path
        except Exception:
            if cache_path.is_file():
                return cache_path
            raise
```

Use `src/atv_player/plugins/__init__.py` to export these types:

```python
from atv_player.plugins.loader import LoadedSpiderPlugin, SpiderPluginLoader

__all__ = ["LoadedSpiderPlugin", "SpiderPluginLoader"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_spider_plugin_loader.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_spider_plugin_loader.py src/atv_player/plugins/__init__.py src/atv_player/plugins/loader.py src/atv_player/plugins/compat/__init__.py src/atv_player/plugins/compat/base/__init__.py src/atv_player/plugins/compat/base/spider.py
git commit -m "test: add spider plugin loader"
```

### Task 3: Adapt spider methods into poster-grid browsing and deferred playback

**Files:**
- Create: `src/atv_player/plugins/controller.py`
- Create: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_spider_plugin_controller.py` with:

```python
from atv_player.models import PlayItem
from atv_player.plugins.controller import SpiderPluginController


class FakeSpider:
    def homeContent(self, filter):
        return {
            "class": [
                {"type_id": "hot", "type_name": "热门"},
                {"type_id": "tv", "type_name": "剧场"},
            ],
            "list": [
                {"vod_id": "/detail/home-1", "vod_name": "首页推荐", "vod_pic": "poster-home"},
            ],
        }

    def categoryContent(self, tid, pg, filter, extend):
        return {
            "list": [
                {"vod_id": f"/detail/{tid}-{pg}", "vod_name": f"{tid}-{pg}", "vod_pic": "poster-cat", "vod_remarks": "更新中"},
            ],
            "page": pg,
            "pagecount": 3,
            "total": 90,
        }

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "红果短剧",
                    "vod_pic": "poster-detail",
                    "vod_play_from": "备用线$$$极速线",
                    "vod_play_url": "第1集$/play/1#第2集$https://media.example/2.m3u8$$$第3集$/play/3",
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "url": f"https://stream.example{ id }.m3u8", "header": {"Referer": "https://site.example"}}

    def searchContent(self, key, quick, pg="1"):
        return {
            "list": [{"vod_id": f"/detail/{key}", "vod_name": key, "vod_pic": "poster-search"}],
            "total": 1,
        }


def test_controller_load_categories_prepends_home_when_home_list_exists() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    categories = controller.load_categories()
    items, total = controller.load_items("home", 1)

    assert [item.type_name for item in categories] == ["推荐", "热门", "剧场"]
    assert [item.vod_name for item in items] == ["首页推荐"]
    assert total == 1


def test_controller_search_and_category_mapping() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    items, total = controller.search_items("庆余年", 1)
    category_items, category_total = controller.load_items("tv", 2)

    assert total == 1
    assert items[0].vod_name == "庆余年"
    assert category_total == 90
    assert category_items[0].vod_name == "tv-2"


def test_controller_build_request_defers_player_content_until_episode_load() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    request = controller.build_request("/detail/1")
    first = request.playlist[0]
    second = request.playlist[1]

    assert first.title == "备用线 | 第1集"
    assert first.url == ""
    assert first.play_source == "备用线"
    assert first.vod_id == "/play/1"
    assert second.url == "https://media.example/2.m3u8"

    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.headers == {"Referer": "https://site.example"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_spider_plugin_controller.py -v`
Expected: FAIL with `ModuleNotFoundError` because the spider controller does not exist yet.

- [ ] **Step 3: Write minimal implementation**

Create `src/atv_player/plugins/controller.py` with:

```python
from __future__ import annotations

from atv_player.api import ApiError
from atv_player.controllers.douban_controller import _map_category, _map_item
from atv_player.controllers.browse_controller import _map_vod_item
from atv_player.models import DoubanCategory, OpenPlayerRequest, PlayItem, VodItem


def _looks_like_media_url(value: str) -> bool:
    candidate = value.strip().lower()
    return candidate.startswith(("http://", "https://", "rtmp://", "rtsp://")) or any(
        ext in candidate for ext in (".m3u8", ".mp4", ".flv")
    )


class SpiderPluginController:
    def __init__(self, spider, plugin_name: str, search_enabled: bool) -> None:
        self._spider = spider
        self._plugin_name = plugin_name
        self.supports_search = search_enabled
        self._home_loaded = False
        self._home_categories: list[DoubanCategory] = []
        self._home_items: list[VodItem] = []

    def _map_items(self, payload: dict) -> list[VodItem]:
        return [_map_item(item) for item in payload.get("list", [])]

    def _ensure_home_loaded(self) -> None:
        if self._home_loaded:
            return
        try:
            payload = self._spider.homeContent(False) or {}
        except Exception as exc:
            raise ApiError(str(exc)) from exc
        categories = [_map_category(item) for item in payload.get("class", [])]
        items = self._map_items(payload)
        if items:
            categories = [DoubanCategory(type_id="home", type_name="推荐"), *categories]
        self._home_categories = categories
        self._home_items = items
        self._home_loaded = True

    def load_categories(self) -> list[DoubanCategory]:
        self._ensure_home_loaded()
        return list(self._home_categories)

    def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
        self._ensure_home_loaded()
        if category_id == "home":
            return list(self._home_items), len(self._home_items)
        try:
            payload = self._spider.categoryContent(category_id, str(page), False, {}) or {}
        except Exception as exc:
            raise ApiError(str(exc)) from exc
        items = self._map_items(payload)
        total = int(payload.get("total") or 0)
        if total <= 0:
            total = len(items)
        return items, total

    def search_items(self, keyword: str, page: int) -> tuple[list[VodItem], int]:
        if not self.supports_search:
            raise ApiError("当前插件不支持搜索")
        try:
            payload = self._spider.searchContent(keyword, False, str(page)) or {}
        except Exception as exc:
            raise ApiError(str(exc)) from exc
        items = self._map_items(payload)
        total = int(payload.get("total") or len(items))
        return items, total

    def _build_playlist(self, detail: VodItem) -> list[PlayItem]:
        routes = [item.strip() for item in (detail.vod_play_from or "").split("$$$")]
        groups = (detail.vod_play_url or "").split("$$$")
        playlist: list[PlayItem] = []
        for group_index, group in enumerate(groups):
            route = routes[group_index] if group_index < len(routes) else ""
            for raw_chunk in group.split("#"):
                chunk = raw_chunk.strip()
                if not chunk:
                    continue
                title, separator, value = chunk.partition("$")
                if not separator:
                    title = chunk
                    value = chunk
                display = title.strip() or value.strip() or f"选集 {len(playlist) + 1}"
                if route:
                    display = f"{route} | {display}"
                playlist.append(
                    PlayItem(
                        title=display,
                        url=value.strip() if _looks_like_media_url(value) else "",
                        vod_id="" if _looks_like_media_url(value) else value.strip(),
                        index=len(playlist),
                        play_source=route,
                    )
                )
        return playlist

    def _resolve_play_item(self, item: PlayItem) -> None:
        if item.url or not item.vod_id:
            return
        try:
            payload = self._spider.playerContent(item.play_source, item.vod_id, []) or {}
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        url = str(payload.get("url") or "").strip()
        if not _looks_like_media_url(url):
            raise ValueError("插件未返回可播放地址")
        item.url = url
        item.headers = dict(payload.get("header") or {})

    def build_request(self, vod_id: str) -> OpenPlayerRequest:
        try:
            payload = self._spider.detailContent([vod_id]) or {}
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        try:
            detail = _map_vod_item(payload["list"][0])
        except (KeyError, IndexError) as exc:
            raise ValueError(f"没有可播放的项目: {vod_id}") from exc
        playlist = self._build_playlist(detail)
        if not playlist:
            raise ValueError(f"没有可播放的项目: {detail.vod_name}")
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            clicked_index=0,
            source_mode="detail",
            source_vod_id=detail.vod_id,
            playback_loader=self._resolve_play_item,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_spider_plugin_controller.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_spider_plugin_controller.py src/atv_player/plugins/controller.py
git commit -m "test: add spider plugin controller"
```

### Task 4: Add the in-app plugin manager dialog

**Files:**
- Modify: `src/atv_player/plugins/__init__.py`
- Create: `src/atv_player/ui/plugin_manager_dialog.py`
- Create: `tests/test_plugin_manager_dialog.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_plugin_manager_dialog.py` with:

```python
from atv_player.models import SpiderPluginConfig, SpiderPluginLogEntry
from atv_player.ui.plugin_manager_dialog import PluginManagerDialog


class FakePluginManager:
    def __init__(self) -> None:
        self.plugins = [
            SpiderPluginConfig(id=1, source_type="local", source_value="/plugins/a.py", display_name="本地A", enabled=True, sort_order=0),
            SpiderPluginConfig(id=2, source_type="remote", source_value="https://example.com/b.py", display_name="远程B", enabled=False, sort_order=1, last_error="下载失败"),
        ]
        self.logs = {
            2: [SpiderPluginLogEntry(id=1, plugin_id=2, level="error", message="下载失败", created_at=1713206400)]
        }
        self.rename_calls: list[tuple[int, str]] = []
        self.toggle_calls: list[tuple[int, bool]] = []
        self.move_calls: list[tuple[int, int]] = []
        self.refresh_calls: list[int] = []
        self.add_local_calls: list[str] = []
        self.add_remote_calls: list[str] = []
        self.delete_calls: list[int] = []

    def list_plugins(self):
        return list(self.plugins)

    def add_local_plugin(self, path: str) -> None:
        self.add_local_calls.append(path)

    def add_remote_plugin(self, url: str) -> None:
        self.add_remote_calls.append(url)

    def rename_plugin(self, plugin_id: int, display_name: str) -> None:
        self.rename_calls.append((plugin_id, display_name))

    def set_plugin_enabled(self, plugin_id: int, enabled: bool) -> None:
        self.toggle_calls.append((plugin_id, enabled))

    def move_plugin(self, plugin_id: int, direction: int) -> None:
        self.move_calls.append((plugin_id, direction))

    def refresh_plugin(self, plugin_id: int) -> None:
        self.refresh_calls.append(plugin_id)

    def delete_plugin(self, plugin_id: int) -> None:
        self.delete_calls.append(plugin_id)

    def list_logs(self, plugin_id: int):
        return self.logs.get(plugin_id, [])


def test_plugin_manager_dialog_renders_rows_and_status(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    assert dialog.plugin_table.rowCount() == 2
    assert dialog.plugin_table.item(0, 0).text() == "本地A"
    assert dialog.plugin_table.item(1, 4).text() == "下载失败"


def test_plugin_manager_dialog_actions_call_manager(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(1)

    monkeypatch.setattr(dialog, "_prompt_display_name", lambda current: "远程重命名")
    monkeypatch.setattr(dialog, "_pick_local_plugin_path", lambda: "/plugins/红果短剧.py")
    monkeypatch.setattr(dialog, "_prompt_remote_url", lambda: "https://example.com/红果短剧.py")
    dialog._add_local_plugin()
    dialog._add_remote_plugin()
    dialog._rename_selected()
    dialog._toggle_selected_enabled()
    dialog._move_selected(-1)
    dialog._refresh_selected()
    dialog._delete_selected()

    assert manager.add_local_calls == ["/plugins/红果短剧.py"]
    assert manager.add_remote_calls == ["https://example.com/红果短剧.py"]
    assert manager.rename_calls == [(2, "远程重命名")]
    assert manager.toggle_calls == [(2, True)]
    assert manager.move_calls == [(2, -1)]
    assert manager.refresh_calls == [2]
    assert manager.delete_calls == [2]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_plugin_manager_dialog.py -v`
Expected: FAIL with `ModuleNotFoundError` because the dialog does not exist yet.

- [ ] **Step 3: Write minimal implementation**

Extend `src/atv_player/plugins/__init__.py` with a small manager service:

```python
from __future__ import annotations

import time
from pathlib import Path

from atv_player.models import SpiderPluginConfig
from atv_player.plugins.controller import SpiderPluginController
from atv_player.plugins.loader import LoadedSpiderPlugin, SpiderPluginLoader
from atv_player.plugins.repository import SpiderPluginRepository


class SpiderPluginManager:
    def __init__(self, repository: SpiderPluginRepository, loader: SpiderPluginLoader) -> None:
        self._repository = repository
        self._loader = loader

    def list_plugins(self) -> list[SpiderPluginConfig]:
        return self._repository.list_plugins()

    def add_local_plugin(self, path: str) -> None:
        plugin = self._repository.add_plugin("local", path, Path(path).stem)
        self.refresh_plugin(plugin.id)

    def add_remote_plugin(self, url: str) -> None:
        plugin = self._repository.add_plugin("remote", url, Path(url).stem.removesuffix(".py"))
        self.refresh_plugin(plugin.id)

    def rename_plugin(self, plugin_id: int, display_name: str) -> None:
        plugin = self._repository.get_plugin(plugin_id)
        self._repository.update_plugin(
            plugin_id,
            display_name=display_name,
            enabled=plugin.enabled,
            cached_file_path=plugin.cached_file_path,
            last_loaded_at=plugin.last_loaded_at,
            last_error=plugin.last_error,
        )

    def set_plugin_enabled(self, plugin_id: int, enabled: bool) -> None:
        plugin = self._repository.get_plugin(plugin_id)
        self._repository.update_plugin(
            plugin_id,
            display_name=plugin.display_name,
            enabled=enabled,
            cached_file_path=plugin.cached_file_path,
            last_loaded_at=plugin.last_loaded_at,
            last_error=plugin.last_error,
        )

    def move_plugin(self, plugin_id: int, direction: int) -> None:
        self._repository.move_plugin(plugin_id, direction)

    def refresh_plugin(self, plugin_id: int) -> None:
        plugin = self._repository.get_plugin(plugin_id)
        loaded = self._loader.load(plugin, force_refresh=True)
        self._repository.update_plugin(
            plugin_id,
            display_name=plugin.display_name,
            enabled=plugin.enabled,
            cached_file_path=loaded.config.cached_file_path,
            last_loaded_at=int(time.time()),
            last_error="",
        )

    def delete_plugin(self, plugin_id: int) -> None:
        self._repository.delete_plugin(plugin_id)

    def list_logs(self, plugin_id: int):
        return self._repository.list_logs(plugin_id)
```

Create `src/atv_player/ui/plugin_manager_dialog.py` with:

```python
from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class PluginManagerDialog(QDialog):
    def __init__(self, plugin_manager, parent=None) -> None:
        super().__init__(parent)
        self.plugin_manager = plugin_manager
        self.setWindowTitle("插件管理")
        self.resize(920, 520)
        self.warning_label = QLabel("远程插件会执行本地 Python 代码，请只加载受信任来源。")

        self.plugin_table = QTableWidget(0, 6, self)
        self.plugin_table.setHorizontalHeaderLabels(["名称", "来源", "地址", "启用", "状态", "最近加载"])
        self.add_local_button = QPushButton("添加本地插件")
        self.add_remote_button = QPushButton("添加远程插件")
        self.rename_button = QPushButton("编辑名称")
        self.toggle_button = QPushButton("启用/禁用")
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.refresh_button = QPushButton("刷新")
        self.logs_button = QPushButton("查看日志")
        self.delete_button = QPushButton("删除")

        actions = QHBoxLayout()
        for button in (
            self.add_local_button,
            self.add_remote_button,
            self.rename_button,
            self.toggle_button,
            self.up_button,
            self.down_button,
            self.refresh_button,
            self.logs_button,
            self.delete_button,
        ):
            actions.addWidget(button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.warning_label)
        layout.addLayout(actions)
        layout.addWidget(self.plugin_table)

        self.add_local_button.clicked.connect(self._add_local_plugin)
        self.add_remote_button.clicked.connect(self._add_remote_plugin)
        self.rename_button.clicked.connect(self._rename_selected)
        self.toggle_button.clicked.connect(self._toggle_selected_enabled)
        self.up_button.clicked.connect(lambda: self._move_selected(-1))
        self.down_button.clicked.connect(lambda: self._move_selected(1))
        self.refresh_button.clicked.connect(self._refresh_selected)
        self.logs_button.clicked.connect(self._show_logs)
        self.delete_button.clicked.connect(self._delete_selected)

        self.reload_plugins()

    def reload_plugins(self) -> None:
        plugins = self.plugin_manager.list_plugins()
        self.plugin_table.setRowCount(len(plugins))
        for row, plugin in enumerate(plugins):
            self.plugin_table.setItem(row, 0, QTableWidgetItem(plugin.display_name or ""))
            self.plugin_table.setItem(row, 1, QTableWidgetItem(plugin.source_type))
            self.plugin_table.setItem(row, 2, QTableWidgetItem(plugin.source_value))
            self.plugin_table.setItem(row, 3, QTableWidgetItem("是" if plugin.enabled else "否"))
            self.plugin_table.setItem(row, 4, QTableWidgetItem(plugin.last_error or "正常"))
            loaded_at = ""
            if plugin.last_loaded_at:
                loaded_at = datetime.fromtimestamp(plugin.last_loaded_at).strftime("%Y-%m-%d %H:%M:%S")
            self.plugin_table.setItem(row, 5, QTableWidgetItem(loaded_at))
            self.plugin_table.item(row, 0).setData(256, plugin.id)

    def _selected_plugin_id(self) -> int | None:
        row = self.plugin_table.currentRow()
        if row < 0:
            return None
        item = self.plugin_table.item(row, 0)
        if item is None:
            return None
        return int(item.data(256))

    def _prompt_display_name(self, current: str) -> str:
        value, accepted = QInputDialog.getText(self, "编辑名称", "显示名称", text=current)
        return value.strip() if accepted else ""

    def _pick_local_plugin_path(self) -> str:
        path, _ = QFileDialog.getOpenFileName(self, "选择 Python 插件", "", "Python Files (*.py)")
        return path.strip()

    def _prompt_remote_url(self) -> str:
        value, accepted = QInputDialog.getText(self, "添加远程插件", "Python 文件 URL")
        return value.strip() if accepted else ""

    def _add_local_plugin(self) -> None:
        path = self._pick_local_plugin_path()
        if not path:
            return
        self.plugin_manager.add_local_plugin(path)
        self.reload_plugins()

    def _add_remote_plugin(self) -> None:
        url = self._prompt_remote_url()
        if not url:
            return
        self.plugin_manager.add_remote_plugin(url)
        self.reload_plugins()

    def _rename_selected(self) -> None:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return
        current = self.plugin_table.item(self.plugin_table.currentRow(), 0).text()
        display_name = self._prompt_display_name(current)
        if not display_name:
            return
        self.plugin_manager.rename_plugin(plugin_id, display_name)
        self.reload_plugins()

    def _toggle_selected_enabled(self) -> None:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return
        enabled_text = self.plugin_table.item(self.plugin_table.currentRow(), 3).text()
        self.plugin_manager.set_plugin_enabled(plugin_id, enabled_text != "是")
        self.reload_plugins()

    def _move_selected(self, direction: int) -> None:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return
        self.plugin_manager.move_plugin(plugin_id, direction)
        self.reload_plugins()

    def _refresh_selected(self) -> None:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return
        self.plugin_manager.refresh_plugin(plugin_id)
        self.reload_plugins()

    def _delete_selected(self) -> None:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return
        self.plugin_manager.delete_plugin(plugin_id)
        self.reload_plugins()

    def _show_logs(self) -> None:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("插件日志")
        dialog.resize(680, 420)
        view = QTextEdit(dialog)
        view.setReadOnly(True)
        lines = []
        for entry in self.plugin_manager.list_logs(plugin_id):
            lines.append(f"[{entry.level}] {entry.message}")
        view.setPlainText("\n".join(lines))
        layout = QVBoxLayout(dialog)
        layout.addWidget(view)
        dialog.exec()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_plugin_manager_dialog.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_plugin_manager_dialog.py src/atv_player/plugins/__init__.py src/atv_player/ui/plugin_manager_dialog.py
git commit -m "test: add plugin manager dialog"
```

### Task 5: Wire plugin manager and dynamic plugin tabs into the app

**Files:**
- Modify: `src/atv_player/app.py`
- Modify: `src/atv_player/ui/main_window.py`
- Modify: `tests/test_app.py`
- Create: `tests/test_main_window_ui.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_main_window_ui.py` with:

```python
from atv_player.models import AppConfig, OpenPlayerRequest, PlayItem, VodItem
from atv_player.ui.main_window import MainWindow


class FakeStaticController:
    def load_categories(self):
        return []

    def load_items(self, category_id: str, page: int):
        return [], 0


class FakeSpiderController:
    def __init__(self, name: str) -> None:
        self.name = name
        self.open_calls: list[str] = []

    def load_categories(self):
        return []

    def load_items(self, category_id: str, page: int):
        return [], 0

    def build_request(self, vod_id: str):
        self.open_calls.append(vod_id)
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name=self.name),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )


class FakePluginManager:
    def __init__(self) -> None:
        self.dialog_opened = 0


def test_main_window_inserts_dynamic_spider_tabs_before_browse(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=object(),
        config=AppConfig(),
        spider_plugins=[
            {"title": "红果短剧", "controller": FakeSpiderController("红果短剧"), "search_enabled": True},
            {"title": "短剧二号", "controller": FakeSpiderController("短剧二号"), "search_enabled": False},
        ],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
        "豆瓣电影",
        "电报影视",
        "网络直播",
        "Emby",
        "Jellyfin",
        "红果短剧",
        "短剧二号",
        "文件浏览",
        "播放记录",
    ]
    assert window.plugin_manager_button.text() == "插件管理"
```

Extend `tests/test_app.py` with:

```python
def test_app_coordinator_passes_loaded_spider_plugins_into_main_window(monkeypatch, tmp_path) -> None:
    repo = SettingsRepository(tmp_path / "app.db")
    repo.save_config(
        AppConfig(
            base_url="http://127.0.0.1:4567",
            username="alice",
            token="token-123",
            vod_token="vod-123",
        )
    )

    loaded_plugins = [
        {"title": "红果短剧", "controller": object(), "search_enabled": True},
    ]

    class FakePluginManager:
        def load_enabled_plugins(self):
            return loaded_plugins

    monkeypatch.setattr(app_module, "ApiClient", lambda *args, **kwargs: ApiClient("http://127.0.0.1:4567", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"token": "vod-123"}))))
    monkeypatch.setattr(app_module, "SpiderPluginManager", lambda repository, loader: FakePluginManager())
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())

    coordinator = AppCoordinator(repo)
    widget = coordinator._show_main()

    assert widget.nav_tabs.tabText(5) == "红果短剧"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main_window_ui.py tests/test_app.py -k "spider or plugin" -v`
Expected: FAIL because `MainWindow` and `AppCoordinator` do not accept spider plugin definitions or a plugin manager yet.

- [ ] **Step 3: Write minimal implementation**

Update the imports in `src/atv_player/app.py`:

```python
from atv_player.plugins import SpiderPluginLoader, SpiderPluginManager
from atv_player.plugins.repository import SpiderPluginRepository
```

Update `AppCoordinator.__init__` in `src/atv_player/app.py`:

```python
class AppCoordinator(QObject):
    def __init__(self, repo: SettingsRepository) -> None:
        super().__init__()
        self.repo = repo
        self.login_window: LoginWindow | None = None
        self.main_window: MainWindow | None = None
        self._api_client: ApiClient | None = None
        self._plugin_repository = SpiderPluginRepository(repo.database_path)
        cache_dir = repo.database_path.parent / "plugins" / "cache"
        self._plugin_loader = SpiderPluginLoader(cache_dir)
        self._plugin_manager = SpiderPluginManager(self._plugin_repository, self._plugin_loader)
```

Replace `_show_main` in `src/atv_player/app.py` with:

```python
def _show_main(self):
    self._api_client = self._build_api_client()
    config = self.repo.load_config()
    capabilities = self._load_capabilities(self._api_client)
    spider_plugins = self._plugin_manager.load_enabled_plugins()
    douban_controller = DoubanController(self._api_client)
    telegram_controller = TelegramSearchController(self._api_client)
    live_controller = LiveController(self._api_client)
    emby_controller = EmbyController(self._api_client)
    jellyfin_controller = JellyfinController(self._api_client)
    browse_controller = BrowseController(self._api_client)
    history_controller = HistoryController(self._api_client)
    player_controller = PlayerController(self._api_client)
    self.main_window = MainWindow(
        browse_controller=browse_controller,
        history_controller=history_controller,
        player_controller=player_controller,
        config=config,
        save_config=lambda: self.repo.save_config(config),
        douban_controller=douban_controller,
        telegram_controller=telegram_controller,
        live_controller=live_controller,
        emby_controller=emby_controller,
        jellyfin_controller=jellyfin_controller,
        spider_plugins=spider_plugins,
        plugin_manager=self._plugin_manager,
        show_emby_tab=bool(capabilities.get("emby")),
        show_jellyfin_tab=bool(capabilities.get("jellyfin")),
    )
    self.main_window.logout_requested.connect(self._handle_logout_requested)
    if self.login_window is not None:
        self.login_window.close()
        self.login_window = None
    if config.last_active_window == "player":
        try:
            restored = self.main_window.restore_last_player()
        except Exception:
            config.last_active_window = "main"
            self.repo.save_config(config)
        else:
            if restored is not None:
                return restored
    return self.main_window
```

Update `src/atv_player/plugins/__init__.py` so the manager can load enabled plugins into dynamic tab definitions:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from atv_player.plugins.controller import SpiderPluginController
from atv_player.plugins.loader import LoadedSpiderPlugin, SpiderPluginLoader
from atv_player.plugins.repository import SpiderPluginRepository


@dataclass(slots=True)
class SpiderPluginDefinition:
    id: int
    title: str
    controller: object
    search_enabled: bool


class SpiderPluginManager:
    def load_enabled_plugins(self) -> list[SpiderPluginDefinition]:
        definitions: list[SpiderPluginDefinition] = []
        for plugin in self._repository.list_plugins():
            if not plugin.enabled:
                continue
            try:
                loaded = self._loader.load(plugin)
            except Exception as exc:
                self._repository.update_plugin(
                    plugin.id,
                    display_name=plugin.display_name,
                    enabled=plugin.enabled,
                    cached_file_path=plugin.cached_file_path,
                    last_loaded_at=plugin.last_loaded_at,
                    last_error=str(exc),
                )
                self._repository.append_log(plugin.id, "error", str(exc))
                continue
            title = plugin.display_name or loaded.plugin_name or Path(plugin.source_value).stem
            controller = SpiderPluginController(
                loaded.spider,
                plugin_name=title,
                search_enabled=loaded.search_enabled,
            )
            definitions.append(
                SpiderPluginDefinition(
                    id=plugin.id,
                    title=title,
                    controller=controller,
                    search_enabled=loaded.search_enabled,
                )
            )
        return definitions
```

Update the imports in `src/atv_player/ui/main_window.py`:

```python
from atv_player.ui.plugin_manager_dialog import PluginManagerDialog
```

Update the `MainWindow.__init__` signature and tab-building block in `src/atv_player/ui/main_window.py`:

```python
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
    emby_controller=None,
    jellyfin_controller=None,
    spider_plugins=None,
    plugin_manager=None,
    show_emby_tab: bool = True,
    show_jellyfin_tab: bool = True,
) -> None:
    super().__init__()
    self._save_config = save_config or (lambda: None)
    self._plugin_definitions = list(spider_plugins or [])
    self._plugin_manager = plugin_manager
    self._plugin_pages: list[tuple[PosterGridPage, object]] = []
    self.nav_tabs = QTabWidget()
    self.plugin_manager_button = QPushButton("插件管理")
    self.logout_button = QPushButton("退出登录")
    self.browse_page = BrowsePage(browse_controller, config=config, save_config=self._save_config)
    self.douban_page = PosterGridPage(douban_controller or _EmptyDoubanController())
    self.telegram_page = PosterGridPage(
        telegram_controller or _EmptyTelegramController(),
        click_action="open",
        search_enabled=True,
    )
    self.live_page = PosterGridPage(
        live_controller or _EmptyLiveController(),
        click_action="open",
        folder_navigation_enabled=True,
    )
    self.emby_page = None
    if show_emby_tab:
        self.emby_page = PosterGridPage(
            emby_controller or _EmptyEmbyController(),
            click_action="open",
            search_enabled=True,
            folder_navigation_enabled=True,
        )
    self.jellyfin_page = None
    if show_jellyfin_tab:
        self.jellyfin_page = PosterGridPage(
            jellyfin_controller or _EmptyJellyfinController(),
            click_action="open",
            search_enabled=True,
            folder_navigation_enabled=True,
        )
    self.history_page = HistoryPage(history_controller)
    self.browse_controller = browse_controller
    self.telegram_controller = telegram_controller or _EmptyTelegramController()
    self.live_controller = live_controller or _EmptyLiveController()
    self.emby_controller = emby_controller or _EmptyEmbyController()
    self.jellyfin_controller = jellyfin_controller or _EmptyJellyfinController()
    self.player_controller = player_controller
    self.player_window: PlayerWindow | None = None
    self.help_dialog: ShortcutHelpDialog | None = None
    self.config = config

    self.nav_tabs.addTab(self.douban_page, "豆瓣电影")
    self.nav_tabs.addTab(self.telegram_page, "电报影视")
    self.nav_tabs.addTab(self.live_page, "网络直播")
    if self.emby_page is not None:
        self.nav_tabs.addTab(self.emby_page, "Emby")
    if self.jellyfin_page is not None:
        self.nav_tabs.addTab(self.jellyfin_page, "Jellyfin")
    self.nav_tabs.addTab(self.browse_page, "文件浏览")
    self.nav_tabs.addTab(self.history_page, "播放记录")

    self.logout_button.clicked.connect(self.logout_requested.emit)
    self.plugin_manager_button.clicked.connect(self._open_plugin_manager)
    header_layout = QHBoxLayout()
    header_layout.addStretch(1)
    header_layout.addWidget(self.plugin_manager_button)
    header_layout.addWidget(self.logout_button)

    container = QWidget()
    container_layout = QVBoxLayout(container)
    container_layout.addLayout(header_layout)
    container_layout.addWidget(self.nav_tabs)
    self.setCentralWidget(container)
    self._rebuild_spider_plugin_tabs()
```

Add these methods to `src/atv_player/ui/main_window.py`:

```python
def _rebuild_spider_plugin_tabs(self) -> None:
    for page, _controller in self._plugin_pages:
        index = self.nav_tabs.indexOf(page)
        if index >= 0:
            self.nav_tabs.removeTab(index)
        page.deleteLater()
    self._plugin_pages = []
    insert_index = self.nav_tabs.indexOf(self.browse_page)
    for definition in self._plugin_definitions:
        page = PosterGridPage(
            definition.controller,
            click_action="open",
            search_enabled=definition.search_enabled,
        )
        page.open_requested.connect(
            lambda vod_id, controller=definition.controller: self._open_spider_request(controller, vod_id)
        )
        page.unauthorized.connect(self.logout_requested.emit)
        self.nav_tabs.insertTab(insert_index, page, definition.title)
        self._plugin_pages.append((page, definition.controller))
        insert_index += 1


def _open_spider_request(self, controller, vod_id: str) -> None:
    try:
        request = controller.build_request(vod_id)
    except Exception as exc:
        self.show_error(str(exc))
        return
    self.open_player(request)


def _open_plugin_manager(self) -> None:
    if self._plugin_manager is None:
        return
    dialog = PluginManagerDialog(self._plugin_manager, self)
    dialog.exec()
    self._plugin_definitions = self._plugin_manager.load_enabled_plugins()
    self._rebuild_spider_plugin_tabs()
```

Add plugin page loading to `_handle_tab_changed` in `src/atv_player/ui/main_window.py`:

```python
def _handle_tab_changed(self, index: int) -> None:
    widget = self.nav_tabs.widget(index)
    if widget is None:
        return
    if widget is self.douban_page:
        self.douban_page.ensure_loaded()
        return
    if widget is self.telegram_page:
        self.telegram_page.ensure_loaded()
        return
    if widget is self.live_page:
        self.live_page.ensure_loaded()
        return
    if widget is self.emby_page and self.emby_page is not None:
        self.emby_page.ensure_loaded()
        return
    if widget is self.jellyfin_page and self.jellyfin_page is not None:
        self.jellyfin_page.ensure_loaded()
        return
    for page, _controller in self._plugin_pages:
        if widget is page:
            page.ensure_loaded()
            return
    if widget is self.browse_page:
        if hasattr(self.browse_controller, "load_folder"):
            self.browse_page.ensure_loaded(self.config.last_path or "/")
        return
    if widget is self.history_page:
        if hasattr(self.history_page.controller, "load_page"):
            self.history_page.ensure_loaded()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main_window_ui.py tests/test_app.py -k "spider or plugin" -v`
Expected: PASS

Then run the broader suites touched by the feature:

Run: `uv run pytest tests/test_storage.py tests/test_spider_plugin_loader.py tests/test_spider_plugin_controller.py tests/test_plugin_manager_dialog.py tests/test_main_window_ui.py tests/test_app.py tests/test_poster_grid_page_ui.py tests/test_player_window_ui.py tests/test_player_controller.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_app.py tests/test_main_window_ui.py src/atv_player/app.py src/atv_player/ui/main_window.py
git commit -m "feat: add python spider plugin tabs"
```

## Self-Review Checklist

- [ ] The plan covers all approved scope from the spec: local and remote loading, plugin manager actions, dynamic tabs, TVBox compatibility, logging, and deferred `playerContent`.
- [ ] Every referenced file path is exact and either already exists or is explicitly created by a task.
- [ ] No step says `TODO`, `TBD`, `later`, or defers concrete code or commands.
- [ ] The plan keeps TDD order in every task: failing test, failing run, minimal implementation, passing run, commit.
