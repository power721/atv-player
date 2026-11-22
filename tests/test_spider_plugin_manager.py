from pathlib import Path

import httpx
import pytest

import atv_player.danmaku.cache as danmaku_cache_module
import atv_player.danmaku.preferences as danmaku_preferences_module
import atv_player.plugins.controller as spider_controller_module
from atv_player.local_playback_history import LocalPlaybackHistoryRepository
from atv_player.models import PlayItem
from atv_player.danmaku.models import DanmakuSourceGroup, DanmakuSourceOption, DanmakuSourceSearchResult
from atv_player.models import (
    DoubanCategory,
    SpiderPluginAction,
    SpiderPluginConfig,
    SpiderPluginImportCancelled,
    SpiderPluginImportResult,
)
from atv_player.plugins import SpiderPluginManager
from atv_player.plugins.loader import LoadedSpiderPlugin
from atv_player.plugins.repository import SpiderPluginRepository


class FakeLoader:
    def load(
        self,
        config: SpiderPluginConfig,
        force_refresh: bool = False,
        initialize: bool = True,
    ) -> LoadedSpiderPlugin:
        del initialize
        return LoadedSpiderPlugin(
            config=SpiderPluginConfig(
                id=config.id,
                source_type=config.source_type,
                source_value=config.source_value,
                display_name=config.display_name,
                enabled=config.enabled,
                sort_order=config.sort_order,
                cached_file_path=config.cached_file_path or "/tmp/plugin.py",
                last_loaded_at=config.last_loaded_at,
                last_error=config.last_error,
                config_text=config.config_text,
                manifest_id=config.manifest_id,
            ),
            spider=object(),
            plugin_name="",
            search_enabled=False,
        )


class FailingLoader:
    def load(
        self,
        config: SpiderPluginConfig,
        force_refresh: bool = False,
        initialize: bool = True,
    ) -> LoadedSpiderPlugin:
        del config, force_refresh, initialize
        raise RuntimeError("network down")


class FakeSpider:
    def init(self, extend: str = "") -> None:
        return None

    def homeContent(self, filter):
        return {
            "class": [{"type_id": "tv", "type_name": "剧场"}],
            "list": [],
        }

    def categoryContent(self, tid, pg, filter, extend):
        return {
            "list": [
                {"vod_id": f"/detail/{tid}-{pg}", "vod_name": f"{tid}-{pg}"},
            ],
            "total": 1,
        }

    def searchContent(self, key, quick, pg=1, category=""):
        return {
            "list": [{"vod_id": f"/detail/{key}", "vod_name": key}],
            "total": 1,
        }

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "红果短剧",
                    "vod_play_from": "默认线",
                    "vod_play_url": "第1集$/play/1",
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "url": f"https://stream.example{id}.m3u8"}


class ParseRequiredSpider(FakeSpider):
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "红果短剧",
                    "vod_play_from": "备用线",
                    "vod_play_url": "第1集$/play/1",
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        return {"parse": 1, "url": f"https://page.example/{id}"}


class ParseLoader(FakeLoader):
    def load(
        self,
        config: SpiderPluginConfig,
        force_refresh: bool = False,
        initialize: bool = True,
    ) -> LoadedSpiderPlugin:
        loaded = super().load(config, force_refresh=force_refresh, initialize=initialize)
        return LoadedSpiderPlugin(
            config=loaded.config,
            spider=ParseRequiredSpider(),
            plugin_name="红果短剧",
            search_enabled=False,
        )


class HistoryLoader(FakeLoader):
    def load(
        self,
        config: SpiderPluginConfig,
        force_refresh: bool = False,
        initialize: bool = True,
    ) -> LoadedSpiderPlugin:
        loaded = super().load(config, force_refresh=force_refresh, initialize=initialize)
        return LoadedSpiderPlugin(
            config=loaded.config,
            spider=FakeSpider(),
            plugin_name="红果短剧",
            search_enabled=False,
        )


class SearchHistoryLoader(FakeLoader):
    def load(
        self,
        config: SpiderPluginConfig,
        force_refresh: bool = False,
        initialize: bool = True,
    ) -> LoadedSpiderPlugin:
        loaded = super().load(config, force_refresh=force_refresh, initialize=initialize)
        return LoadedSpiderPlugin(
            config=loaded.config,
            spider=FakeSpider(),
            plugin_name="红果短剧",
            search_enabled=True,
        )


class ActionSpider(FakeSpider):
    def getManagerActions(self):
        return [
            {"id": "qr_login", "label": "扫码登录"},
            {
                "id": "refresh_cookie",
                "label": "刷新 Cookie",
                "enabled": False,
                "tooltip": "需要先扫码登录",
            },
            {"id": "hidden_action", "label": "隐藏动作", "visible": False},
        ]


class InvalidActionSpider(FakeSpider):
    def getManagerActions(self):
        return [
            "bad-payload",
            {"id": "", "label": "缺少 id"},
            {"id": "missing_label"},
        ]


class ActionLoader(FakeLoader):
    def load(
        self,
        config: SpiderPluginConfig,
        force_refresh: bool = False,
        initialize: bool = True,
    ) -> LoadedSpiderPlugin:
        loaded = super().load(config, force_refresh=force_refresh, initialize=initialize)
        return LoadedSpiderPlugin(
            config=loaded.config,
            spider=ActionSpider(),
            plugin_name="红果短剧",
            search_enabled=False,
        )


class InvalidActionLoader(FakeLoader):
    def load(
        self,
        config: SpiderPluginConfig,
        force_refresh: bool = False,
        initialize: bool = True,
    ) -> LoadedSpiderPlugin:
        loaded = super().load(config, force_refresh=force_refresh, initialize=initialize)
        return LoadedSpiderPlugin(
            config=loaded.config,
            spider=InvalidActionSpider(),
            plugin_name="坏动作插件",
            search_enabled=False,
        )


class RunnableActionSpider(FakeSpider):
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def getManagerActions(self):
        return [{"id": "qr_login", "label": "扫码登录"}]

    def runManagerAction(self, action_id: str, context) -> None:
        self.calls.append((action_id, context.parent))
        context.log("info", f"执行动作: {action_id}")
        context.set_config_text("token=updated\ncookie=1\n")
        context.refresh_plugin()


class RunnableActionLoader(FakeLoader):
    def __init__(self) -> None:
        self.spider = RunnableActionSpider()
        self.force_refresh_calls: list[bool] = []

    def load(
        self,
        config: SpiderPluginConfig,
        force_refresh: bool = False,
        initialize: bool = True,
    ) -> LoadedSpiderPlugin:
        self.force_refresh_calls.append(force_refresh)
        loaded = super().load(config, force_refresh=force_refresh, initialize=initialize)
        return LoadedSpiderPlugin(
            config=loaded.config,
            spider=self.spider,
            plugin_name="红果短剧",
            search_enabled=False,
        )


class CategoryLoader(FakeLoader):
    def load(
        self,
        config: SpiderPluginConfig,
        force_refresh: bool = False,
        initialize: bool = True,
    ) -> LoadedSpiderPlugin:
        loaded = super().load(config, force_refresh=force_refresh, initialize=initialize)
        spider = FakeSpider()
        spider.homeContent = lambda filter: {  # type: ignore[method-assign]
            "class": [
                {"type_id": "hot", "type_name": "热门"},
                {"type_id": "tv", "type_name": "剧场"},
            ],
            "list": [],
        }
        return LoadedSpiderPlugin(
            config=loaded.config,
            spider=spider,
            plugin_name="分类插件",
            search_enabled=True,
        )


def test_manager_load_plugins_loads_only_requested_enabled_plugins(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    repository.add_plugin("local", "/plugins/a.py", "插件A", enabled=True)
    second = repository.add_plugin("local", "/plugins/b.py", "插件B", enabled=True)
    repository.add_plugin("local", "/plugins/c.py", "插件C", enabled=False)
    manager = SpiderPluginManager(repository, FakeLoader())

    definitions = manager.load_plugins([second.id, "999", "3"])

    assert [definition.id for definition in definitions] == [second.id]


def test_manager_add_remote_plugin_uses_decoded_url_filename_as_default_name(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader())

    manager.add_remote_plugin("https://example.com/plugins/%E7%BA%A2%E6%9E%9C%E7%9F%AD%E5%89%A7.py?raw=1#download")

    plugins = repository.list_plugins()

    assert len(plugins) == 1
    assert plugins[0].source_type == "remote"
    assert plugins[0].display_name == "红果短剧"


def test_manager_add_remote_txt_plugin_overwrites_existing_plugin_with_same_id(tmp_path: Path) -> None:
    first_url = "https://example.com/plugins/old.txt"
    second_url = "https://example.com/plugins/new.txt"
    responses = {
        first_url: httpx.Response(200, text="//@id:same-plugin\n//@version:1\nclass Spider: pass\n"),
        second_url: httpx.Response(200, text="//@id:same-plugin\n//@version:2\nclass Spider: pass\n"),
    }

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader(), get=fake_get)

    manager.add_remote_plugin(first_url)
    manager.add_remote_plugin(second_url)

    plugins = repository.list_plugins()

    assert len(plugins) == 1
    assert plugins[0].manifest_id == "same-plugin"
    assert plugins[0].source_type == "remote"
    assert plugins[0].source_value == second_url
    assert plugins[0].plugin_version == 2


def test_manager_add_local_txt_plugin_overwrites_existing_plugin_with_same_id(tmp_path: Path) -> None:
    first_path = tmp_path / "old.txt"
    second_path = tmp_path / "new.txt"
    first_path.write_text("//@id:same-plugin\n//@version:1\nclass Spider: pass\n", encoding="utf-8")
    second_path.write_text("//@id:same-plugin\n//@version:2\nclass Spider: pass\n", encoding="utf-8")
    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader())

    manager.add_local_plugin(str(first_path))
    manager.add_local_plugin(str(second_path))

    plugins = repository.list_plugins()

    assert len(plugins) == 1
    assert plugins[0].manifest_id == "same-plugin"
    assert plugins[0].source_type == "local"
    assert plugins[0].source_value == str(second_path)
    assert plugins[0].plugin_version == 2


def test_manager_reads_stable_id_from_plain_python_plugin_metadata(tmp_path: Path) -> None:
    plugin_path = tmp_path / "木偶.py"
    plugin_path.write_text(
        'PLUGIN_ID = "02544b320a6d45de997bc0bd3975d0c060b8"\nclass Spider: pass\n',
        encoding="utf-8",
    )
    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader())

    manager.add_local_plugin(str(plugin_path))

    plugin = repository.list_plugins()[0]
    assert plugin.manifest_id == "02544b320a6d45de997bc0bd3975d0c060b8"


def test_manager_backfills_stable_id_for_existing_plain_python_plugin(tmp_path: Path) -> None:
    stable_id = "02544b320a6d45de997bc0bd3975d0c060b8"
    plugin_path = tmp_path / "木偶.py"
    plugin_path.write_text(
        f'PLUGIN_ID = "{stable_id}"\nclass Spider: pass\n', encoding="utf-8"
    )
    repository = SpiderPluginRepository(tmp_path / "app.db")
    old_plugin = repository.add_plugin("local", str(plugin_path), "木偶")
    manager = SpiderPluginManager(repository, FakeLoader())

    assert manager.backfill_source_metadata() == 1

    plugin = repository.get_plugin(old_plugin.id)
    assert plugin.id == old_plugin.id
    assert plugin.manifest_id == stable_id


def test_manager_import_github_repository_imports_manifest_plugins_and_disables_invalid_entries(tmp_path: Path) -> None:
    responses = {
        "https://api.github.com/repos/har01d5/tvbox": httpx.Response(
            200,
            json={"default_branch": "master"},
        ),
        "https://raw.githubusercontent.com/har01d5/tvbox/master/spiders_v2.json": httpx.Response(
            200,
            json=[
                {"file": "py/潮流APP.txt", "valid": True, "version": 8},
                {"file": "py/双星.txt", "valid": False, "version": 3},
                {"file": "py/无版本.txt", "version": 5},
            ],
        ),
        "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E6%BD%AE%E6%B5%81APP.txt": httpx.Response(
            200,
            text="//@version:1\nprint('a')\n",
        ),
        "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E5%8F%8C%E6%98%9F.txt": httpx.Response(
            200,
            text="  //@version:99\nprint('b')\n",
        ),
        "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E6%97%A0%E7%89%88%E6%9C%AC.txt": httpx.Response(
            200,
            text="//@version:42\nprint('c')\n",
        ),
    }
    progress_events: list[tuple[str, int, int, str]] = []

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader(), get=fake_get)

    result = manager.import_plugins(
        "https://github.com/har01d5/tvbox",
        progress_callback=lambda event: progress_events.append(
            (event.stage, event.current, event.total, event.message)
        ),
    )

    plugins = repository.list_plugins()

    assert result == SpiderPluginImportResult(imported_count=3, updated_count=0, skipped_count=0)
    assert [plugin.source_value for plugin in plugins] == [
        "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E6%BD%AE%E6%B5%81APP.txt",
        "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E5%8F%8C%E6%98%9F.txt",
        "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E6%97%A0%E7%89%88%E6%9C%AC.txt",
    ]
    assert [plugin.plugin_version for plugin in plugins] == [8, 3, 5]
    assert [plugin.enabled for plugin in plugins] == [True, False, True]
    assert progress_events == [
        ("resolve_repo", 0, 0, "正在解析仓库信息"),
        ("fetch_manifest", 0, 0, "正在读取 spiders_v2.json"),
        ("import_plugin", 1, 3, "正在导入 py/潮流APP.txt"),
        ("import_plugin", 2, 3, "正在导入 py/双星.txt"),
        ("import_plugin", 3, 3, "正在导入 py/无版本.txt"),
    ]


def test_manager_import_github_repository_falls_back_to_repo_page_when_api_is_forbidden(tmp_path: Path) -> None:
    api_url = "https://api.github.com/repos/har01d5/tvbox"
    repo_url = "https://github.com/har01d5/tvbox"
    manifest_url = "https://raw.githubusercontent.com/har01d5/tvbox/master/spiders_v2.json"
    plugin_url = "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E5%8F%8C%E6%98%9F.txt"
    responses = {
        api_url: httpx.Response(
            403,
            request=httpx.Request("GET", api_url),
            text="forbidden",
        ),
        repo_url: httpx.Response(
            200,
            text='<html><head><meta name="octolytics-dimension-repository_default_branch" content="master"></head></html>',
        ),
        manifest_url: httpx.Response(
            200,
            json=[
                {"file": "py/双星.txt", "valid": True, "version": 3},
            ],
        ),
        plugin_url: httpx.Response(
            200,
            text="//@version:3\nprint('ok')\n",
        ),
    }
    requested_urls: list[str] = []

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        requested_urls.append(url)
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader(), get=fake_get)

    result = manager.import_plugins("https://github.com/har01d5/tvbox")

    plugins = repository.list_plugins()

    assert result == SpiderPluginImportResult(imported_count=1, updated_count=0, skipped_count=0)
    assert [plugin.source_value for plugin in plugins] == [plugin_url]
    assert requested_urls == [
        api_url,
        repo_url,
        manifest_url,
        plugin_url,
    ]


def test_manager_import_github_repository_skips_same_version_and_updates_existing_version(tmp_path: Path) -> None:
    responses = {
        "https://api.github.com/repos/har01d5/tvbox": httpx.Response(
            200,
            json={"default_branch": "master"},
        ),
        "https://raw.githubusercontent.com/har01d5/tvbox/master/spiders_v2.json": httpx.Response(
            200,
            json=[
                {"file": "py/潮流APP.txt", "valid": True, "version": 6},
                {"file": "py/双星.txt", "valid": True, "version": 9},
            ],
        ),
        "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E6%BD%AE%E6%B5%81APP.txt": httpx.Response(
            200,
            text="//@version:100\nprint('same')\n",
        ),
        "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E5%8F%8C%E6%98%9F.txt": httpx.Response(
            200,
            text="//@version:1\nprint('new')\n",
        ),
    }

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    repository = SpiderPluginRepository(tmp_path / "app.db")
    repository.add_plugin(
        "remote",
        "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E6%BD%AE%E6%B5%81APP.txt",
        "潮流APP",
        enabled=True,
        plugin_version=6,
    )
    existing = repository.add_plugin(
        "remote",
        "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E5%8F%8C%E6%98%9F.txt",
        "双星",
        enabled=False,
        plugin_version=6,
    )
    repository.update_plugin(
        existing.id,
        display_name="双星自定义",
        enabled=False,
        cached_file_path=existing.cached_file_path,
        last_loaded_at=existing.last_loaded_at,
        last_error=existing.last_error,
        config_text="token=keep\n",
        plugin_version=existing.plugin_version,
    )
    manager = SpiderPluginManager(repository, FakeLoader(), get=fake_get)

    result = manager.import_plugins("https://github.com/har01d5/tvbox")

    plugins = repository.list_plugins()
    updated = next(
        plugin
        for plugin in plugins
        if plugin.source_value == "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E5%8F%8C%E6%98%9F.txt"
    )

    assert result == SpiderPluginImportResult(imported_count=0, updated_count=1, skipped_count=1)
    assert len(plugins) == 2
    assert updated.plugin_version == 9
    assert updated.enabled is False
    assert updated.display_name == "双星自定义"
    assert updated.config_text == "token=keep\n"


def test_manager_import_github_repository_overwrites_existing_plugin_with_same_manifest_id(tmp_path: Path) -> None:
    first_url = "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E6%97%A7%E6%BA%90.txt"
    second_url = "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E6%96%B0%E6%BA%90.txt"
    responses = {
        "https://api.github.com/repos/har01d5/tvbox": httpx.Response(
            200,
            json={"default_branch": "master"},
        ),
        "https://raw.githubusercontent.com/har01d5/tvbox/master/spiders_v2.json": httpx.Response(
            200,
            json=[
                {"id": "same-plugin", "file": "py/旧源.txt", "valid": True, "version": 6},
                {"id": "same-plugin", "file": "py/新源.txt", "valid": False, "version": 9},
            ],
        ),
        first_url: httpx.Response(
            200,
            text="//@version:6\nprint('old')\n",
        ),
        second_url: httpx.Response(
            200,
            text="//@version:9\nprint('new')\n",
        ),
    }

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader(), get=fake_get)

    result = manager.import_plugins("https://github.com/har01d5/tvbox")

    plugins = repository.list_plugins()

    assert result == SpiderPluginImportResult(imported_count=1, updated_count=1, skipped_count=0)
    assert len(plugins) == 1
    assert plugins[0].manifest_id == "same-plugin"
    assert plugins[0].source_value == second_url
    assert plugins[0].plugin_version == 9
    assert plugins[0].enabled is False


def test_manager_import_github_repository_skips_same_id_version_valid_state(
    tmp_path: Path,
) -> None:
    plugin_url = "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E5%90%8C%E6%BA%90.txt"
    responses = {
        "https://api.github.com/repos/har01d5/tvbox": httpx.Response(
            200,
            json={"default_branch": "master"},
        ),
        "https://raw.githubusercontent.com/har01d5/tvbox/master/spiders_v2.json": httpx.Response(
            200,
            json=[
                {"id": "same-plugin", "file": "py/同源.txt", "valid": False, "version": 6},
            ],
        ),
        plugin_url: httpx.Response(
            200,
            text="//@version:6\nprint('same')\n",
        ),
    }

    requested_urls: list[str] = []

    def fake_get(
        url: str,
        timeout: float = 15.0,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        requested_urls.append(url)
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    repository = SpiderPluginRepository(tmp_path / "app.db")
    existing = repository.add_plugin(
        "remote",
        plugin_url,
        "同源",
        enabled=True,
        plugin_version=6,
        manifest_id="same-plugin",
    )
    manager = SpiderPluginManager(repository, FakeLoader(), get=fake_get)

    result = manager.import_plugins("https://github.com/har01d5/tvbox")

    plugins = repository.list_plugins()

    assert result == SpiderPluginImportResult(
        imported_count=0,
        updated_count=0,
        skipped_count=1,
    )
    assert [plugin.id for plugin in plugins] == [existing.id]
    assert plugins[0].enabled is True
    assert plugins[0].plugin_version == 6
    assert requested_urls == [
        "https://api.github.com/repos/har01d5/tvbox",
        "https://raw.githubusercontent.com/har01d5/tvbox/master/spiders_v2.json",
    ]


def test_manager_import_github_repository_skips_entries_with_invalid_manifest_version(tmp_path: Path) -> None:
    responses = {
        "https://api.github.com/repos/har01d5/tvbox": httpx.Response(
            200,
            json={"default_branch": "master"},
        ),
        "https://raw.githubusercontent.com/har01d5/tvbox/master/spiders_v2.json": httpx.Response(
            200,
            json=[
                {"file": "py/潮流APP.txt", "version": "bad"},
            ],
        ),
    }

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader(), get=fake_get)

    result = manager.import_plugins("https://github.com/har01d5/tvbox")

    assert result == SpiderPluginImportResult(imported_count=0, updated_count=0, skipped_count=1)
    assert repository.list_plugins() == []


def test_manager_import_github_repository_stops_after_cancellation_and_preserves_completed_changes(
    tmp_path: Path,
) -> None:
    first_url = "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E6%BD%AE%E6%B5%81APP.txt"
    second_url = "https://raw.githubusercontent.com/har01d5/tvbox/master/py/%E5%8F%8C%E6%98%9F.txt"
    responses = {
        "https://api.github.com/repos/har01d5/tvbox": httpx.Response(
            200,
            json={"default_branch": "master"},
        ),
        "https://raw.githubusercontent.com/har01d5/tvbox/master/spiders_v2.json": httpx.Response(
            200,
            json=[
                {"file": "py/潮流APP.txt", "valid": True, "version": 6},
                {"file": "py/双星.txt", "valid": True, "version": 7},
            ],
        ),
        first_url: httpx.Response(
            200,
            text="//@version:6\nprint('a')\n",
        ),
        second_url: httpx.Response(
            200,
            text="//@version:7\nprint('b')\n",
        ),
    }

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader(), get=fake_get)

    def cancel_callback() -> bool:
        return repository.find_plugin_by_source_value(first_url) is not None

    with pytest.raises(SpiderPluginImportCancelled) as exc_info:
        manager.import_plugins(
            "https://github.com/har01d5/tvbox",
            cancel_callback=cancel_callback,
        )

    plugins = repository.list_plugins()

    assert exc_info.value.result == SpiderPluginImportResult(
        imported_count=1,
        updated_count=0,
        skipped_count=0,
    )
    assert [plugin.source_value for plugin in plugins] == [first_url]
    assert plugins[0].plugin_version == 6


def test_manager_import_plugins_accepts_direct_manifest_url_with_relative_files(tmp_path: Path) -> None:
    responses = {
        "https://d.har01d.cn/spiders_v2.json": httpx.Response(
            200,
            json=[
                {"file": "py/红果.txt", "valid": True, "version": 8},
                {"file": "nested/双星.txt", "valid": False, "version": 3},
            ],
        ),
        "https://d.har01d.cn/py/%E7%BA%A2%E6%9E%9C.txt": httpx.Response(
            200,
            text="//@version:1\nprint('a')\n",
        ),
        "https://d.har01d.cn/nested/%E5%8F%8C%E6%98%9F.txt": httpx.Response(
            200,
            text="//@version:2\nprint('b')\n",
        ),
    }
    progress_events: list[tuple[str, int, int, str]] = []

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader(), get=fake_get)

    result = manager.import_plugins(
        "https://d.har01d.cn/spiders_v2.json",
        progress_callback=lambda event: progress_events.append(
            (event.stage, event.current, event.total, event.message)
        ),
    )

    plugins = repository.list_plugins()

    assert result == SpiderPluginImportResult(imported_count=2, updated_count=0, skipped_count=0)
    assert [plugin.source_value for plugin in plugins] == [
        "https://d.har01d.cn/py/%E7%BA%A2%E6%9E%9C.txt",
        "https://d.har01d.cn/nested/%E5%8F%8C%E6%98%9F.txt",
    ]
    assert [plugin.enabled for plugin in plugins] == [True, False]
    assert progress_events == [
        ("fetch_manifest", 0, 0, "正在读取 spiders_v2.json"),
        ("import_plugin", 1, 2, "正在导入 py/红果.txt"),
        ("import_plugin", 2, 2, "正在导入 nested/双星.txt"),
    ]


def test_manager_import_plugins_accepts_direct_manifest_url_with_absolute_files(tmp_path: Path) -> None:
    plugin_url = "https://cdn.example.com/plugins/%E6%BD%AE%E6%B5%81APP.txt"
    responses = {
        "https://example.com/spiders_v2.json": httpx.Response(
            200,
            json=[{"file": plugin_url, "valid": True, "version": 6}],
        ),
        plugin_url: httpx.Response(200, text="//@version:1\nprint('a')\n"),
    }

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader(), get=fake_get)

    result = manager.import_plugins("https://example.com/spiders_v2.json")

    plugins = repository.list_plugins()

    assert result == SpiderPluginImportResult(imported_count=1, updated_count=0, skipped_count=0)
    assert [plugin.source_value for plugin in plugins] == [plugin_url]


def test_manager_import_plugins_skips_same_ids_versions_across_sources(
    tmp_path: Path,
) -> None:
    direct_manifest_url = "https://d.har01d.cn/spiders_v2.json"
    direct_hongguo_url = "https://d.har01d.cn/py/%E7%BA%A2%E6%9E%9C.txt"
    direct_shuangxing_url = "https://d.har01d.cn/py/%E5%8F%8C%E6%98%9F.txt"
    github_manifest_url = (
        "https://raw.githubusercontent.com/har01d5/tvbox/master/spiders_v2.json"
    )
    github_hongguo_url = (
        "https://raw.githubusercontent.com/har01d5/tvbox/master/py/"
        "%E7%BA%A2%E6%9E%9C.txt"
    )
    github_shuangxing_url = (
        "https://raw.githubusercontent.com/har01d5/tvbox/master/py/"
        "%E5%8F%8C%E6%98%9F.txt"
    )
    responses = {
        direct_manifest_url: httpx.Response(
            200,
            json=[
                {"id": "hongguo", "file": "py/红果.txt", "valid": True, "version": 8},
                {"id": "shuangxing", "file": "py/双星.txt", "valid": False, "version": 3},
            ],
        ),
        direct_hongguo_url: httpx.Response(
            200,
            text="//@version:8\nprint('hongguo')\n",
        ),
        direct_shuangxing_url: httpx.Response(
            200,
            text="//@version:3\nprint('shuangxing')\n",
        ),
        "https://api.github.com/repos/har01d5/tvbox": httpx.Response(
            200,
            json={"default_branch": "master"},
        ),
        github_manifest_url: httpx.Response(
            200,
            json=[
                {"id": "hongguo", "file": "py/红果.txt", "valid": True, "version": 8},
                {"id": "shuangxing", "file": "py/双星.txt", "valid": False, "version": 3},
            ],
        ),
        github_hongguo_url: httpx.Response(
            200,
            text="//@version:8\nprint('hongguo')\n",
        ),
        github_shuangxing_url: httpx.Response(
            200,
            text="//@version:3\nprint('shuangxing')\n",
        ),
    }

    def fake_get(
        url: str,
        timeout: float = 15.0,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    class RecordingLoader(FakeLoader):
        def __init__(self) -> None:
            self.loaded_plugin_ids: list[int] = []

        def load(
            self,
            config: SpiderPluginConfig,
            force_refresh: bool = False,
            initialize: bool = True,
        ) -> LoadedSpiderPlugin:
            self.loaded_plugin_ids.append(config.id)
            return super().load(
                config,
                force_refresh=force_refresh,
                initialize=initialize,
            )

    repository = SpiderPluginRepository(tmp_path / "app.db")
    loader = RecordingLoader()
    manager = SpiderPluginManager(repository, loader, get=fake_get)

    first_result = manager.import_plugins(direct_manifest_url)
    first_plugins = repository.list_plugins()

    second_result = manager.import_plugins("https://github.com/har01d5/tvbox")
    second_plugins = repository.list_plugins()

    assert first_result == SpiderPluginImportResult(
        imported_count=2,
        updated_count=0,
        skipped_count=0,
    )
    assert second_result == SpiderPluginImportResult(
        imported_count=0,
        updated_count=0,
        skipped_count=2,
    )
    assert [plugin.id for plugin in second_plugins] == [
        plugin.id for plugin in first_plugins
    ]
    assert [plugin.source_value for plugin in second_plugins] == [
        direct_hongguo_url,
        direct_shuangxing_url,
    ]
    assert [plugin.plugin_version for plugin in second_plugins] == [8, 3]
    assert loader.loaded_plugin_ids == [
        plugin.id for plugin in first_plugins
    ]


def test_manager_import_plugins_skips_relative_entries_with_parent_traversal(tmp_path: Path) -> None:
    responses = {
        "https://example.com/plugins/spiders_v2.json": httpx.Response(
            200,
            json=[{"file": "../escape.txt", "valid": True, "version": 2}],
        ),
    }

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        response = responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response

    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader(), get=fake_get)

    result = manager.import_plugins("https://example.com/plugins/spiders_v2.json")

    assert result == SpiderPluginImportResult(imported_count=0, updated_count=0, skipped_count=1)
    assert repository.list_plugins() == []


def test_manager_import_plugins_rejects_invalid_source_url(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader())

    with pytest.raises(ValueError, match="请输入 GitHub 仓库地址或 spiders_v2.json URL"):
        manager.import_plugins("not-a-url")


def test_manager_iter_enabled_plugins_prioritizes_requested_plugin_ids(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader())
    plugin1 = repository.add_plugin("local", "/tmp/1.py", "插件1", enabled=True)
    plugin2 = repository.add_plugin("local", "/tmp/2.py", "插件2", enabled=True)
    plugin3 = repository.add_plugin("local", "/tmp/3.py", "插件3", enabled=True)
    repository.move_plugin(plugin3.id, -1)
    repository.move_plugin(plugin3.id, -1)

    definitions = list(
        manager.iter_enabled_plugins(
            prioritized_plugin_ids=(str(plugin2.id), str(plugin1.id)),
        )
    )

    assert [definition.id for definition in definitions] == [plugin2.id, plugin1.id, plugin3.id]


def test_manager_reorder_plugins_persists_requested_final_order(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader())
    plugin1 = repository.add_plugin("local", "/tmp/1.py", "插件1")
    plugin2 = repository.add_plugin("local", "/tmp/2.py", "插件2")
    plugin3 = repository.add_plugin("local", "/tmp/3.py", "插件3")

    manager.reorder_plugins([plugin3.id, plugin1.id, plugin2.id])

    assert [plugin.id for plugin in repository.list_plugins()] == [plugin3.id, plugin1.id, plugin2.id]


def test_manager_reorder_plugins_raises_when_plugin_snapshot_is_stale(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader())
    plugin1 = repository.add_plugin("local", "/tmp/1.py", "插件1")
    plugin2 = repository.add_plugin("local", "/tmp/2.py", "插件2")

    with pytest.raises(ValueError, match="插件列表已变化"):
        manager.reorder_plugins([plugin1.id])

    assert [plugin.id for plugin in repository.list_plugins()] == [plugin1.id, plugin2.id]


def test_manager_list_plugin_actions_normalizes_visible_actions(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")
    manager = SpiderPluginManager(repository, ActionLoader())

    actions = manager.list_plugin_actions(plugin.id)

    assert actions == [
        SpiderPluginAction(id="qr_login", label="扫码登录"),
        SpiderPluginAction(
            id="refresh_cookie",
            label="刷新 Cookie",
            enabled=False,
            tooltip="需要先扫码登录",
        ),
    ]


def test_manager_list_plugin_actions_ignores_invalid_payloads_and_logs_reasons(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", "/plugins/bad.py", "坏动作插件")
    manager = SpiderPluginManager(repository, InvalidActionLoader())

    actions = manager.list_plugin_actions(plugin.id)
    logs = repository.list_logs(plugin.id)

    assert actions == []
    assert any("插件动作声明无效" in entry.message for entry in logs)


def test_manager_list_plugin_actions_returns_empty_for_plugins_without_action_api(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", "/plugins/plain.py", "普通插件")
    manager = SpiderPluginManager(repository, FakeLoader())

    assert manager.list_plugin_actions(plugin.id) == []


def test_manager_run_plugin_action_provides_context_and_persists_side_effects(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")
    loader = RunnableActionLoader()
    manager = SpiderPluginManager(repository, loader)

    parent = object()

    manager.run_plugin_action(plugin.id, "qr_login", parent=parent)

    saved = repository.get_plugin(plugin.id)
    logs = repository.list_logs(plugin.id)
    assert loader.spider.calls == [("qr_login", parent)]
    assert saved.config_text == "token=updated\ncookie=1\n"
    assert any(entry.message == "执行动作: qr_login" for entry in logs)
    assert loader.force_refresh_calls[-1] is True


def test_manager_run_plugin_action_rejects_undeclared_action(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")
    manager = SpiderPluginManager(repository, ActionLoader())

    with pytest.raises(ValueError, match="插件动作未注册: missing_action"):
        manager.run_plugin_action(plugin.id, "missing_action")


def test_manager_set_plugin_category_overrides_persists_json_and_survives_refresh(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", "/plugins/demo.py", "演示插件")
    manager = SpiderPluginManager(repository, FakeLoader())

    manager.set_plugin_category_overrides(plugin.id, '{"renames":{"movie":"影片"}}')
    manager.refresh_plugin(plugin.id)

    saved = repository.get_plugin(plugin.id)

    assert saved.category_overrides_json == '{"renames":{"movie":"影片"}}'


def test_manager_load_plugin_categories_returns_raw_plugin_categories(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", "/plugins/demo.py", "演示插件")
    repository.set_plugin_category_overrides(plugin.id, '{"renames":{"hot":"热门推荐"}}')
    manager = SpiderPluginManager(repository, CategoryLoader())

    categories = manager.load_plugin_categories(plugin.id)

    assert categories == [
        DoubanCategory(type_id="hot", type_name="热门"),
        DoubanCategory(type_id="tv", type_name="剧场"),
    ]


def test_manager_refresh_plugin_records_error_and_log_instead_of_raising(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("remote", "https://example.com/bad.py", "坏插件")
    manager = SpiderPluginManager(repository, FailingLoader())

    manager.refresh_plugin(plugin.id)

    saved = repository.get_plugin(plugin.id)
    logs = repository.list_logs(plugin.id)

    assert saved.last_error == "network down"
    assert logs[0].level == "error"
    assert logs[0].message == "network down"


class CountingLoader(FakeLoader):
    def __init__(self) -> None:
        self.load_count = 0
        self.spiders: list[object] = []

    def load(
        self,
        config: SpiderPluginConfig,
        force_refresh: bool = False,
        initialize: bool = True,
    ) -> LoadedSpiderPlugin:
        self.load_count += 1
        loaded = super().load(config, force_refresh=force_refresh, initialize=initialize)
        spider = FakeSpider()
        self.spiders.append(spider)
        return LoadedSpiderPlugin(
            config=loaded.config,
            spider=spider,
            plugin_name="红果短剧",
            search_enabled=False,
        )


def test_manager_refresh_plugin_reuses_loaded_spider_for_tab_reload(tmp_path: Path) -> None:
    source = tmp_path / "demo.py"
    source.write_text("# demo v1\n", encoding="utf-8")
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", str(source), "演示插件", enabled=True)
    loader = CountingLoader()
    manager = SpiderPluginManager(repository, loader)

    manager.refresh_plugin(plugin.id)
    definitions = manager.load_plugins([str(plugin.id)])

    assert loader.load_count == 1
    assert definitions[0].controller._spider is loader.spiders[0]


def test_manager_refresh_plugin_stale_config_not_reused_for_tab_reload(tmp_path: Path) -> None:
    source = tmp_path / "demo.py"
    source.write_text("# demo v1\n", encoding="utf-8")
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", str(source), "演示插件", enabled=True)
    loader = CountingLoader()
    manager = SpiderPluginManager(repository, loader)

    manager.refresh_plugin(plugin.id)
    manager.set_plugin_config(plugin.id, "token=abc\n")
    definitions = manager.load_plugins([str(plugin.id)])

    assert loader.load_count == 2
    assert definitions[0].controller._spider is loader.spiders[1]


def test_manager_refresh_plugin_changed_source_file_not_reused_for_tab_reload(tmp_path: Path) -> None:
    source = tmp_path / "demo.py"
    source.write_text("# demo v1\n", encoding="utf-8")
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", str(source), "演示插件", enabled=True)
    loader = CountingLoader()
    manager = SpiderPluginManager(repository, loader)

    manager.refresh_plugin(plugin.id)
    source.write_text("# demo v2 with different size\n", encoding="utf-8")
    definitions = manager.load_plugins([str(plugin.id)])

    assert loader.load_count == 2
    assert definitions[0].controller._spider is loader.spiders[1]


def test_manager_refresh_plugin_stale_stash_falls_back_to_fresh_load(tmp_path: Path) -> None:
    source = tmp_path / "demo.py"
    source.write_text("# demo v1\n", encoding="utf-8")
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", str(source), "演示插件", enabled=True)
    loader = CountingLoader()
    manager = SpiderPluginManager(repository, loader)

    manager.refresh_plugin(plugin.id)
    manager.load_plugins([str(plugin.id)])
    definitions = manager.load_plugins([str(plugin.id)])

    # 暂存是一次性的:第二次 load_plugins 不再复用,重新加载
    assert loader.load_count == 2
    assert definitions[0].controller._spider is loader.spiders[1]


def test_manager_load_enabled_plugins_wires_local_repository_playback_history_callbacks(tmp_path: Path) -> None:
    plugin_repository = SpiderPluginRepository(tmp_path / "app.db")
    local_history_repository = LocalPlaybackHistoryRepository(tmp_path / "app.db")
    plugin = plugin_repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧", enabled=True)
    local_history_repository.save_history(
        "spider_plugin",
        "detail-1",
        {
            "vodName": "红果短剧",
            "vodPic": "poster",
            "vodRemarks": "第1集",
            "episode": 0,
            "episodeUrl": "https://media.example/1.m3u8",
            "position": 45000,
            "opening": 0,
            "ending": 0,
            "speed": 1.0,
            "playlistIndex": 1,
            "createTime": 1713206400000,
        },
        source_key=str(plugin.id),
        source_name="红果短剧",
    )
    manager = SpiderPluginManager(plugin_repository, HistoryLoader(), local_history_repository)

    definitions = manager.load_enabled_plugins()
    request = definitions[0].controller.build_request("detail-1")

    assert request.playback_history_loader is not None
    loaded = request.playback_history_loader()
    assert loaded is not None
    assert loaded.position == 45000
    assert loaded.playlist_index == 1

    assert request.playback_history_saver is not None
    request.playback_history_saver(
        {
            "vodName": "红果短剧",
            "vodPic": "poster",
            "vodRemarks": "第1集",
            "episode": 0,
            "episodeUrl": "https://media.example/1.m3u8",
            "position": 90000,
            "opening": 0,
            "ending": 0,
            "speed": 1.0,
            "playlistIndex": 0,
            "createTime": 1713206500000,
        }
    )
    updated = local_history_repository.get_history("spider_plugin", "detail-1", source_key=str(plugin.id))
    assert updated is not None
    assert updated.position == 90000
    assert updated.playlist_index == 0


def test_manager_set_plugin_config_persists_raw_text_and_survives_other_updates(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")
    manager = SpiderPluginManager(repository, FakeLoader())

    manager.set_plugin_config(plugin.id, "token=abc\ncookie = 1\n")
    manager.rename_plugin(plugin.id, "红果短剧新版")
    manager.refresh_plugin(plugin.id)

    saved = repository.get_plugin(plugin.id)

    assert saved.display_name == "红果短剧新版"
    assert saved.config_text == "token=abc\ncookie = 1\n"


def test_manager_load_enabled_plugins_wires_built_in_parser_service(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧", enabled=True)

    class FakeParserService:
        def resolve(self, flag: str, url: str, preferred_key: str = ""):
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

    manager = SpiderPluginManager(repository, ParseLoader())
    manager._playback_parser_service = FakeParserService()
    manager._preferred_parse_key_loader = lambda: "jx1"

    definitions = manager.load_enabled_plugins()
    request = definitions[0].controller.build_request("detail-1")

    assert request.playback_loader is not None
    item = request.playlist[0]
    request.playback_loader(item)

    assert item.url == "https://media.example/resolved.m3u8"
    assert item.headers == {"Referer": "https://page.example"}


def test_manager_persists_spider_method_call_logs_to_plugin_logs(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    plugin = repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧", enabled=True)
    manager = SpiderPluginManager(repository, SearchHistoryLoader())

    definitions = manager.load_enabled_plugins()
    controller = definitions[0].controller

    controller.load_items("tv", 2, {"area": "CN"})
    controller.search_items("庆余年", 3, "tv")
    request = controller.build_request("detail-1")
    assert request.playback_loader is not None
    request.playback_loader(request.playlist[0])

    messages = [entry.message for entry in repository.list_logs(plugin.id)]

    assert any("method=categoryContent" in message and "tid='tv'" in message for message in messages)
    assert any("method=searchContent" in message and "key='庆余年'" in message for message in messages)
    assert any("method=detailContent" in message and "ids=['detail-1']" in message for message in messages)
    assert any("method=playerContent" in message and "id='/play/1'" in message for message in messages)


def test_manager_load_enabled_plugins_wires_danmaku_service(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧", enabled=True)

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            return []

        def resolve_danmu(self, page_url: str) -> str:
            return ""

    manager = SpiderPluginManager(repository, FakeLoader())
    manager._danmaku_service = FakeDanmakuService()

    definitions = manager.load_enabled_plugins()

    assert getattr(definitions[0].controller, "_danmaku_service", None) is manager._danmaku_service


def test_manager_load_enabled_plugins_wires_ytdlp_service(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧", enabled=True)

    class FakeYtdlpService:
        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

    manager = SpiderPluginManager(repository, FakeLoader())
    manager._yt_dlp_service = FakeYtdlpService()

    definitions = manager.load_enabled_plugins()

    assert getattr(definitions[0].controller, "_yt_dlp_service", None) is manager._yt_dlp_service


def test_manager_load_enabled_plugins_persists_manual_danmaku_source_preference_across_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(danmaku_preferences_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(
        spider_controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        spider_controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    repository = SpiderPluginRepository(tmp_path / "app.db")
    repository.add_plugin("local", "/plugins/玄界之门3D版.py", "玄界之门3D版", enabled=True)
    result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[
                    DanmakuSourceOption(provider="tencent", name="默认结果", url="https://v.qq.com/default"),
                    DanmakuSourceOption(provider="tencent", name="手工选择", url="https://v.qq.com/manual"),
                ],
            )
        ],
        default_option_url="https://v.qq.com/default",
        default_provider="tencent",
    )

    class FakeDanmakuService:
        def _preferred_result(self) -> DanmakuSourceSearchResult:
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[
                            DanmakuSourceOption(provider="tencent", name="手工选择", url="https://v.qq.com/manual"),
                            DanmakuSourceOption(provider="tencent", name="默认结果", url="https://v.qq.com/default"),
                        ],
                    )
                ],
                default_option_url="https://v.qq.com/manual",
                default_provider="tencent",
            )

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            if preferred_page_url == "https://v.qq.com/manual":
                return self._preferred_result()
            return result

        def rerank_danmaku_source_search_result(
            self,
            cached_result,
            *,
            query_name: str = "",
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            if preferred_page_url == "https://v.qq.com/manual":
                return self._preferred_result()
            return cached_result

        def resolve_danmu(self, page_url: str) -> str:
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">ok</d></i>'

    first_manager = SpiderPluginManager(repository, FakeLoader())
    first_manager._danmaku_service = FakeDanmakuService()
    first_controller = first_manager.load_enabled_plugins()[0].controller
    first_item = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="玄界之门3D版")

    first_controller.refresh_danmaku_sources(first_item, query_override="玄界之门 1集", force_refresh=True)
    first_controller.switch_danmaku_source(first_item, "https://v.qq.com/manual")

    second_manager = SpiderPluginManager(repository, FakeLoader())
    second_manager._danmaku_service = FakeDanmakuService()
    second_controller = second_manager.load_enabled_plugins()[0].controller
    restarted_item = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="玄界之门3D版")

    second_controller.refresh_danmaku_sources(restarted_item)

    assert restarted_item.selected_danmaku_url == "https://v.qq.com/manual"
