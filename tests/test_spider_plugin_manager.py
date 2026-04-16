from pathlib import Path

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
            ),
            spider=object(),
            plugin_name="",
            search_enabled=False,
        )


class FailingLoader:
    def load(self, config: SpiderPluginConfig, force_refresh: bool = False) -> LoadedSpiderPlugin:
        raise RuntimeError("network down")


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
