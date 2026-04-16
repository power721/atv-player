from __future__ import annotations

from dataclasses import dataclass
import time
from pathlib import Path

from atv_player.models import SpiderPluginConfig
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
    def __init__(self, repository: SpiderPluginRepository, loader: SpiderPluginLoader) -> None:
        self._repository = repository
        self._loader = loader

    def list_plugins(self) -> list[SpiderPluginConfig]:
        return self._repository.list_plugins()

    def add_local_plugin(self, path: str) -> None:
        plugin = self._repository.add_plugin("local", path, Path(path).stem)
        self.refresh_plugin(plugin.id)

    def add_remote_plugin(self, url: str) -> None:
        name = Path(url).stem or Path(url).name.removesuffix(".py")
        plugin = self._repository.add_plugin("remote", url, name)
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


__all__ = [
    "LoadedSpiderPlugin",
    "SpiderPluginLoader",
    "SpiderPluginDefinition",
    "SpiderPluginManager",
]
