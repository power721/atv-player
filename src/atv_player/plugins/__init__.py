from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
import time
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlparse

import httpx

from atv_player.danmaku.preferences import DanmakuSeriesPreferenceStore
from atv_player.models import (
    SpiderPluginAction,
    SpiderPluginActionContext,
    SpiderPluginConfig,
    SpiderPluginImportCancelled,
    SpiderPluginImportProgress,
    SpiderPluginImportResult,
)
from atv_player.plugins.controller import SpiderPluginController
from atv_player.plugins.loader import LoadedSpiderPlugin, SpiderPluginLoader
from atv_player.plugins.repository import SpiderPluginRepository


@dataclass(slots=True)
class SpiderPluginDefinition:
    id: int
    title: str
    controller: object
    search_enabled: bool
    sort_order: int = 0


_PLUGIN_VERSION_PATTERN = re.compile(r"^\s*//@version:(\d+)\s*$")


def _default_plugin_name(source_type: str, source_value: str) -> str:
    if source_type == "remote":
        parsed = urlparse(source_value)
        path = unquote(parsed.path or "")
        name = Path(path).stem or Path(path).name.removesuffix(".py")
        if name:
            return name
    return Path(source_value).stem or Path(source_value).name.removesuffix(".py")


def _coerce_plugin_action(payload: object) -> SpiderPluginAction | None:
    if not isinstance(payload, dict):
        return None
    action_id = str(payload.get("id") or "").strip()
    label = str(payload.get("label") or "").strip()
    if not action_id or not label:
        return None
    return SpiderPluginAction(
        id=action_id,
        label=label,
        enabled=bool(payload.get("enabled", True)),
        visible=bool(payload.get("visible", True)),
        tooltip=str(payload.get("tooltip") or "").strip(),
    )


def _parse_github_repo(value: str) -> tuple[str, str]:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise ValueError("请输入 GitHub 仓库地址")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError("请输入 GitHub 仓库地址")
    return parts[0], parts[1]


def _parse_plugin_version(source_text: str) -> int:
    for line in source_text.splitlines()[:16]:
        matched = _PLUGIN_VERSION_PATTERN.match(line)
        if matched:
            return int(matched.group(1))
    return 1


def _raw_github_url(owner: str, repo: str, branch: str, relative_path: str) -> str:
    encoded_parts = [quote(part) for part in PurePosixPath(relative_path).parts]
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{'/'.join(encoded_parts)}"


class SpiderPluginManager:
    def __init__(
        self,
        repository: SpiderPluginRepository,
        loader: SpiderPluginLoader,
        playback_history_repository=None,
        *,
        get=httpx.get,
    ) -> None:
        self._repository = repository
        self._loader = loader
        self._playback_history_repository = playback_history_repository
        self._get = get
        self._playback_parser_service = None
        self._preferred_parse_key_loader = None
        self._base_url_loader = None
        self._danmaku_service = None
        self._danmaku_preference_store = DanmakuSeriesPreferenceStore()

    def list_plugins(self) -> list[SpiderPluginConfig]:
        return self._repository.list_plugins()

    def add_local_plugin(self, path: str) -> None:
        plugin = self._repository.add_plugin("local", path, Path(path).stem)
        self.refresh_plugin(plugin.id)

    def add_remote_plugin(self, url: str) -> None:
        name = _default_plugin_name("remote", url)
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
            config_text=plugin.config_text,
            plugin_version=plugin.plugin_version,
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
            config_text=plugin.config_text,
            plugin_version=plugin.plugin_version,
        )

    def set_plugin_config(self, plugin_id: int, config_text: str) -> None:
        plugin = self._repository.get_plugin(plugin_id)
        self._repository.update_plugin(
            plugin_id,
            display_name=plugin.display_name,
            enabled=plugin.enabled,
            cached_file_path=plugin.cached_file_path,
            last_loaded_at=plugin.last_loaded_at,
            last_error=plugin.last_error,
            config_text=config_text,
            plugin_version=plugin.plugin_version,
        )

    def move_plugin(self, plugin_id: int, direction: int) -> None:
        self._repository.move_plugin(plugin_id, direction)

    def refresh_plugin(self, plugin_id: int) -> None:
        plugin = self._repository.get_plugin(plugin_id)
        try:
            loaded = self._loader.load(plugin, force_refresh=True)
        except Exception as exc:
            self._repository.update_plugin(
                plugin_id,
                display_name=plugin.display_name,
                enabled=plugin.enabled,
                cached_file_path=plugin.cached_file_path,
                last_loaded_at=plugin.last_loaded_at,
                last_error=str(exc),
                config_text=plugin.config_text,
                plugin_version=plugin.plugin_version,
            )
            self._repository.append_log(plugin.id, "error", str(exc))
            return
        self._repository.update_plugin(
            plugin_id,
            display_name=plugin.display_name,
            enabled=plugin.enabled,
            cached_file_path=loaded.config.cached_file_path,
            last_loaded_at=int(time.time()),
            last_error="",
            config_text=plugin.config_text,
            plugin_version=plugin.plugin_version,
        )

    def delete_plugin(self, plugin_id: int) -> None:
        self._repository.delete_plugin(plugin_id)

    def list_logs(self, plugin_id: int):
        return self._repository.list_logs(plugin_id)

    def _emit_import_progress(
        self,
        callback: Callable[[SpiderPluginImportProgress], None] | None,
        *,
        stage: str,
        current: int = 0,
        total: int = 0,
        message: str,
    ) -> None:
        if callback is None:
            return
        callback(
            SpiderPluginImportProgress(
                stage=stage,
                current=current,
                total=total,
                message=message,
            )
        )

    def _raise_if_import_cancelled(
        self,
        cancel_callback: Callable[[], bool] | None,
        result: SpiderPluginImportResult,
    ) -> None:
        if cancel_callback is not None and cancel_callback():
            raise SpiderPluginImportCancelled(result)

    def _fetch_json(self, url: str) -> object:
        response = self._get(url, timeout=15.0, follow_redirects=True)
        if response.status_code >= 300:
            raise httpx.HTTPStatusError(
                f"Error response {response.status_code} while requesting {url}",
                request=response.request,
                response=response,
            )
        return response.json()

    def _fetch_text(self, url: str) -> str:
        response = self._get(url, timeout=15.0, follow_redirects=True)
        if response.status_code >= 300:
            raise httpx.HTTPStatusError(
                f"Error response {response.status_code} while requesting {url}",
                request=response.request,
                response=response,
            )
        return response.text

    def _load_github_default_branch(self, owner: str, repo: str) -> str:
        payload = self._fetch_json(f"https://api.github.com/repos/{owner}/{repo}")
        if not isinstance(payload, dict):
            raise ValueError("无法解析仓库默认分支")
        branch = str(payload.get("default_branch") or "").strip()
        if not branch:
            raise ValueError("无法解析仓库默认分支")
        return branch

    def _get_plugin(self, plugin_id: int) -> SpiderPluginConfig:
        return self._repository.get_plugin(plugin_id)

    def _load_plugin(self, plugin_id: int, *, force_refresh: bool = False) -> tuple[SpiderPluginConfig, LoadedSpiderPlugin]:
        plugin = self._get_plugin(plugin_id)
        return plugin, self._loader.load(plugin, force_refresh=force_refresh)

    def _plugin_title(self, plugin: SpiderPluginConfig, loaded: LoadedSpiderPlugin) -> str:
        return plugin.display_name or loaded.plugin_name or _default_plugin_name(
            plugin.source_type, plugin.source_value
        )

    def _append_plugin_log(self, plugin_id: int, level: str, message: str) -> None:
        self._repository.append_log(plugin_id, level, message)

    def _build_plugin_definition(
        self,
        plugin,
        loaded,
        *,
        drive_detail_loader=None,
        offline_download_detail_loader=None,
    ) -> SpiderPluginDefinition:
        title = self._plugin_title(plugin, loaded)
        controller = SpiderPluginController(
            loaded.spider,
            plugin_name=title,
            search_enabled=loaded.search_enabled,
            drive_detail_loader=drive_detail_loader,
            offline_download_detail_loader=offline_download_detail_loader,
            playback_parser_service=self._playback_parser_service,
            preferred_parse_key_loader=self._preferred_parse_key_loader,
            base_url_loader=self._base_url_loader,
            danmaku_service=self._danmaku_service,
            danmaku_preference_store=self._danmaku_preference_store,
            playback_history_loader=None
            if self._playback_history_repository is None
            else lambda vod_id, plugin_id=plugin.id: self._playback_history_repository.get_history(
                "spider_plugin",
                vod_id,
                source_key=str(plugin_id),
            ),
            playback_history_saver=None
            if self._playback_history_repository is None
            else lambda vod_id, payload, source_name=title, plugin_id=plugin.id: self._playback_history_repository.save_history(
                "spider_plugin",
                vod_id,
                payload,
                source_key=str(plugin_id),
                source_name=source_name,
            ),
        )
        return SpiderPluginDefinition(
            id=plugin.id,
            title=title,
            controller=controller,
            search_enabled=loaded.search_enabled,
            sort_order=plugin.sort_order,
        )

    def _build_action_context(
        self,
        plugin: SpiderPluginConfig,
        loaded: LoadedSpiderPlugin,
        *,
        parent=None,
    ) -> SpiderPluginActionContext:
        return SpiderPluginActionContext(
            parent=parent,
            plugin_id=plugin.id,
            plugin_name=self._plugin_title(plugin, loaded),
            config_text=plugin.config_text,
            set_config_text=lambda text, plugin_id=plugin.id: self.set_plugin_config(plugin_id, text),
            refresh_plugin=lambda plugin_id=plugin.id: self.refresh_plugin(plugin_id),
            log=lambda level, message, plugin_id=plugin.id: self._append_plugin_log(plugin_id, level, message),
        )

    def list_plugin_actions(self, plugin_id: int) -> list[SpiderPluginAction]:
        plugin, loaded = self._load_plugin(plugin_id)
        get_actions = getattr(loaded.spider, "getManagerActions", None)
        if not callable(get_actions):
            return []
        actions: list[SpiderPluginAction] = []
        for payload in get_actions() or []:
            action = _coerce_plugin_action(payload)
            if action is None:
                self._repository.append_log(plugin.id, "error", f"插件动作声明无效: {payload!r}")
                continue
            if action.visible:
                actions.append(action)
        return actions

    def run_plugin_action(self, plugin_id: int, action_id: str, parent=None) -> None:
        actions = self.list_plugin_actions(plugin_id)
        action = next((item for item in actions if item.id == action_id), None)
        if action is None:
            raise ValueError(f"插件动作未注册: {action_id}")
        plugin, loaded = self._load_plugin(plugin_id)
        runner = getattr(loaded.spider, "runManagerAction", None)
        if not callable(runner):
            raise ValueError(f"插件不支持动作执行: {action_id}")
        context = self._build_action_context(plugin, loaded, parent=parent)
        try:
            runner(action_id, context)
        except Exception as exc:
            self._repository.append_log(plugin.id, "error", f"插件动作执行失败[{action_id}]: {exc}")
            raise

    def import_github_repository(
        self,
        repo_url: str,
        *,
        progress_callback: Callable[[SpiderPluginImportProgress], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> SpiderPluginImportResult:
        result = SpiderPluginImportResult()
        owner, repo = _parse_github_repo(repo_url)
        self._raise_if_import_cancelled(cancel_callback, result)
        self._emit_import_progress(progress_callback, stage="resolve_repo", message="正在解析仓库信息")
        self._raise_if_import_cancelled(cancel_callback, result)
        default_branch = self._load_github_default_branch(owner, repo)
        self._emit_import_progress(progress_callback, stage="fetch_manifest", message="正在读取 spiders_v2.json")
        self._raise_if_import_cancelled(cancel_callback, result)
        manifest = self._fetch_json(_raw_github_url(owner, repo, default_branch, "spiders_v2.json"))
        if not isinstance(manifest, list):
            raise ValueError("spiders_v2.json 格式无效")

        valid_entries = [entry for entry in manifest if isinstance(entry, dict) and str(entry.get("file") or "").strip()]
        total = len(valid_entries)
        for index, entry in enumerate(valid_entries, start=1):
            self._raise_if_import_cancelled(cancel_callback, result)
            file_path = str(entry.get("file") or "").strip()
            self._emit_import_progress(
                progress_callback,
                stage="import_plugin",
                current=index,
                total=total,
                message=f"正在导入 {file_path}",
            )
            path = PurePosixPath(file_path)
            if path.is_absolute() or ".." in path.parts:
                result.skipped_count += 1
                continue
            try:
                source_url = _raw_github_url(owner, repo, default_branch, file_path)
                self._raise_if_import_cancelled(cancel_callback, result)
                source_text = self._fetch_text(source_url)
                plugin_version = _parse_plugin_version(source_text)
                existing = self._repository.find_plugin_by_source_value(source_url)
                if existing is None:
                    plugin = self._repository.add_plugin(
                        "remote",
                        source_url,
                        _default_plugin_name("remote", source_url),
                        enabled=bool(entry.get("valid", True)),
                        plugin_version=plugin_version,
                    )
                    result.imported_count += 1
                    self._raise_if_import_cancelled(cancel_callback, result)
                    self.refresh_plugin(plugin.id)
                    continue
                if existing.plugin_version == plugin_version:
                    result.skipped_count += 1
                    continue
                self._repository.update_plugin(
                    existing.id,
                    display_name=existing.display_name,
                    enabled=existing.enabled,
                    cached_file_path=existing.cached_file_path,
                    last_loaded_at=existing.last_loaded_at,
                    last_error=existing.last_error,
                    config_text=existing.config_text,
                    plugin_version=plugin_version,
                )
                result.updated_count += 1
                self._raise_if_import_cancelled(cancel_callback, result)
                self.refresh_plugin(existing.id)
            except SpiderPluginImportCancelled:
                raise
            except Exception:
                result.skipped_count += 1
        return result

    def iter_enabled_plugins(
        self,
        drive_detail_loader=None,
        offline_download_detail_loader=None,
        *,
        prioritized_plugin_ids: tuple[str, ...] | list[str] = (),
    ):
        prioritized_order = {str(plugin_id): index for index, plugin_id in enumerate(prioritized_plugin_ids)}
        plugins = [plugin for plugin in self._repository.list_plugins() if plugin.enabled]
        plugins.sort(
            key=lambda plugin: (
                prioritized_order.get(str(plugin.id), len(prioritized_order)),
                plugin.sort_order,
                plugin.id,
            )
        )
        for plugin in plugins:
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
                    config_text=plugin.config_text,
                    plugin_version=plugin.plugin_version,
                )
                self._repository.append_log(plugin.id, "error", str(exc))
                continue
            yield self._build_plugin_definition(
                plugin,
                loaded,
                drive_detail_loader=drive_detail_loader,
                offline_download_detail_loader=offline_download_detail_loader,
            )

    def load_enabled_plugins(self, drive_detail_loader=None, offline_download_detail_loader=None) -> list[SpiderPluginDefinition]:
        definitions: list[SpiderPluginDefinition] = []
        for definition in self.iter_enabled_plugins(
            drive_detail_loader=drive_detail_loader,
            offline_download_detail_loader=offline_download_detail_loader,
        ):
            definitions.append(definition)
        return definitions

    def load_plugins(
        self,
        plugin_ids: tuple[str, ...] | list[str] | tuple[int, ...] | list[int],
        drive_detail_loader=None,
        offline_download_detail_loader=None,
    ) -> list[SpiderPluginDefinition]:
        requested_ids = {str(plugin_id) for plugin_id in plugin_ids if str(plugin_id)}
        if not requested_ids:
            return []
        definitions: list[SpiderPluginDefinition] = []
        plugins = [plugin for plugin in self._repository.list_plugins() if plugin.enabled and str(plugin.id) in requested_ids]
        plugins.sort(key=lambda plugin: plugin.sort_order)
        for plugin in plugins:
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
                    config_text=plugin.config_text,
                    plugin_version=plugin.plugin_version,
                )
                self._repository.append_log(plugin.id, "error", str(exc))
                continue
            definitions.append(
                self._build_plugin_definition(
                    plugin,
                    loaded,
                    drive_detail_loader=drive_detail_loader,
                    offline_download_detail_loader=offline_download_detail_loader,
                )
            )
        return definitions


__all__ = [
    "LoadedSpiderPlugin",
    "SpiderPluginLoader",
    "SpiderPluginDefinition",
    "SpiderPluginManager",
]
