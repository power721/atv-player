from pathlib import Path

import atv_player.danmaku.cache as danmaku_cache_module
import atv_player.danmaku.preferences as danmaku_preferences_module
import atv_player.plugins.controller as spider_controller_module
from atv_player.local_playback_history import LocalPlaybackHistoryRepository
from atv_player.models import PlayItem
from atv_player.danmaku.models import DanmakuSourceGroup, DanmakuSourceOption, DanmakuSourceSearchResult
from atv_player.models import SpiderPluginConfig
from atv_player.plugins import SpiderPluginManager
from atv_player.plugins.loader import LoadedSpiderPlugin
from atv_player.plugins.repository import SpiderPluginRepository


class FakeLoader:
    def load(self, config: SpiderPluginConfig, force_refresh: bool = False) -> LoadedSpiderPlugin:
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
            ),
            spider=object(),
            plugin_name="",
            search_enabled=False,
        )


class FailingLoader:
    def load(self, config: SpiderPluginConfig, force_refresh: bool = False) -> LoadedSpiderPlugin:
        raise RuntimeError("network down")


class FakeSpider:
    def init(self, extend: str = "") -> None:
        return None

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "红果短剧",
                    "vod_play_url": "第1集$https://media.example/1.m3u8",
                }
            ]
        }


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
    def load(self, config: SpiderPluginConfig, force_refresh: bool = False) -> LoadedSpiderPlugin:
        loaded = super().load(config, force_refresh=force_refresh)
        return LoadedSpiderPlugin(
            config=loaded.config,
            spider=ParseRequiredSpider(),
            plugin_name="红果短剧",
            search_enabled=False,
        )


class HistoryLoader(FakeLoader):
    def load(self, config: SpiderPluginConfig, force_refresh: bool = False) -> LoadedSpiderPlugin:
        loaded = super().load(config, force_refresh=force_refresh)
        return LoadedSpiderPlugin(
            config=loaded.config,
            spider=FakeSpider(),
            plugin_name="红果短剧",
            search_enabled=False,
        )


def test_manager_add_remote_plugin_uses_decoded_url_filename_as_default_name(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    manager = SpiderPluginManager(repository, FakeLoader())

    manager.add_remote_plugin("https://example.com/plugins/%E7%BA%A2%E6%9E%9C%E7%9F%AD%E5%89%A7.py?raw=1#download")

    plugins = repository.list_plugins()

    assert len(plugins) == 1
    assert plugins[0].source_type == "remote"
    assert plugins[0].display_name == "红果短剧"


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


def test_manager_load_enabled_plugins_wires_local_repository_playback_history_callbacks(tmp_path: Path) -> None:
    plugin_repository = SpiderPluginRepository(tmp_path / "app.db")
    local_history_repository = LocalPlaybackHistoryRepository(tmp_path / "app.db")
    plugin = plugin_repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")
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
    repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")

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


def test_manager_load_enabled_plugins_wires_danmaku_service(tmp_path: Path) -> None:
    repository = SpiderPluginRepository(tmp_path / "app.db")
    repository.add_plugin("local", "/plugins/红果短剧.py", "红果短剧")

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            return []

        def resolve_danmu(self, page_url: str) -> str:
            return ""

    manager = SpiderPluginManager(repository, FakeLoader())
    manager._danmaku_service = FakeDanmakuService()

    definitions = manager.load_enabled_plugins()

    assert getattr(definitions[0].controller, "_danmaku_service", None) is manager._danmaku_service


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
    repository.add_plugin("local", "/plugins/玄界之门3D版.py", "玄界之门3D版")
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
