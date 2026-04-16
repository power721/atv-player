from pathlib import Path

import httpx
import pytest

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
    bad_path.write_text("class NotSpider:\n    pass\n", encoding="utf-8")
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
