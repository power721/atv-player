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

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        calls.append(f"{url}|follow_redirects={follow_redirects}")
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
        "https://example.com/红果短剧.py|follow_redirects=True",
        "https://example.com/红果短剧.py|follow_redirects=True",
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


def test_loader_supports_plugins_that_use_cache_during_init(tmp_path: Path) -> None:
    plugin_path = tmp_path / "cache_plugin.py"
    plugin_path.write_text(
        """
from base.spider import Spider

class Spider(Spider):
    def init(self, extend=""):
        device_id = self.getCache("did")
        if not device_id:
            self.setCache("did", "device-1")
            device_id = self.getCache("did")
        self.device_id = device_id

    def getName(self):
        return f"缓存:{self.device_id}"
""",
        encoding="utf-8",
    )
    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache")
    config = SpiderPluginConfig(
        id=3,
        source_type="local",
        source_value=str(plugin_path),
        display_name="",
        enabled=True,
        sort_order=0,
    )

    loaded = loader.load(config)

    assert loaded.plugin_name == "缓存:device-1"


def test_loader_follows_redirects_for_remote_plugin_download(tmp_path: Path) -> None:
    calls: list[tuple[str, bool]] = []

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        calls.append((url, follow_redirects))
        if follow_redirects:
            return httpx.Response(200, text=PLUGIN_SOURCE)
        return httpx.Response(302, headers={"Location": "https://cdn.example.com/spider.py"}, text="")

    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache", get=fake_get)
    config = SpiderPluginConfig(
        id=11,
        source_type="remote",
        source_value="https://example.com/redirect.py",
        display_name="",
        enabled=True,
        sort_order=0,
    )

    loaded = loader.load(config, force_refresh=True)

    assert loaded.plugin_name == "红果短剧"
    assert calls == [("https://example.com/redirect.py", True)]


def test_loader_ignores_empty_cached_remote_file_and_redownloads(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_file = cache_dir / "plugin_12.py"
    cached_file.write_text("", encoding="utf-8")
    calls: list[str] = []

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        calls.append(url)
        return httpx.Response(200, text=PLUGIN_SOURCE)

    loader = SpiderPluginLoader(cache_dir=cache_dir, get=fake_get)
    config = SpiderPluginConfig(
        id=12,
        source_type="remote",
        source_value="https://example.com/reload.py",
        display_name="",
        enabled=True,
        sort_order=0,
        cached_file_path=str(cached_file),
    )

    loaded = loader.load(config)

    assert loaded.plugin_name == "红果短剧"
    assert calls == ["https://example.com/reload.py"]
    assert "class Spider(Spider):" in cached_file.read_text(encoding="utf-8")


def test_loader_does_not_fallback_to_empty_cached_remote_file_when_refresh_fails(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_file = cache_dir / "plugin_13.py"
    cached_file.write_text("", encoding="utf-8")

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        raise httpx.ConnectError("network down")

    loader = SpiderPluginLoader(cache_dir=cache_dir, get=fake_get)
    config = SpiderPluginConfig(
        id=13,
        source_type="remote",
        source_value="https://example.com/fail.py",
        display_name="",
        enabled=True,
        sort_order=0,
        cached_file_path=str(cached_file),
    )

    with pytest.raises(httpx.ConnectError, match="network down"):
        loader.load(config, force_refresh=True)
