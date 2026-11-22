from __future__ import annotations

import ast
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx

from atv_player.danmaku.preferences import DanmakuSeriesPreferenceStore
from atv_player.models import (
    DoubanCategory,
    SpiderPluginAction,
    SpiderPluginActionContext,
    SpiderPluginConfig,
    SpiderPluginImportCancelled,
    SpiderPluginImportProgress,
    SpiderPluginImportResult,
)
from atv_player.plugins.controller import (
    SpiderPluginController,
    coerce_spider_plugin_action,
)
from atv_player.plugins.loader import LoadedSpiderPlugin, SpiderPluginLoader
from atv_player.plugins.repository import SpiderPluginRepository


@dataclass(slots=True)
class SpiderPluginDefinition:
    id: int
    title: str
    controller: object
    search_enabled: bool
    sort_order: int = 0


def _source_file_identity(path: Path) -> tuple[int, int] | None:
    try:
        if not path.is_file():
            return None
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _default_plugin_name(source_type: str, source_value: str) -> str:
    if source_type == "remote":
        parsed = urlparse(source_value)
        path = unquote(parsed.path or "")
        name = Path(path).stem or Path(path).name.removesuffix(".py")
        if name:
            return name
    return Path(source_value).stem or Path(source_value).name.removesuffix(".py")


def _parse_github_repo(value: str) -> tuple[str, str]:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise ValueError("请输入 GitHub 仓库地址")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError("请输入 GitHub 仓库地址")
    return parts[0], parts[1]


def _is_github_repo_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    return parsed.scheme == "https" and parsed.netloc == "github.com" and len(parts) >= 2


def _parse_manifest_source_url(value: str) -> str:
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("请输入 GitHub 仓库地址或 spiders_v2.json URL")
    return url


def _parse_manifest_plugin_version(entry: object) -> int | None:
    if not isinstance(entry, dict):
        return None
    try:
        value = int(entry.get("version"))
    except (TypeError, ValueError):
        return None
    if value < 1:
        return None
    return value


def _parse_manifest_plugin_id(entry: object) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("id") or "").strip()


def _parse_plugin_source_metadata(source_text: str) -> tuple[str, int]:
    plugin_id = ""
    plugin_version = 1
    for raw_line in source_text.splitlines()[:16]:
        line = raw_line.strip()
        id_match = re.match(r"^//\s*@id\s*:\s*(.+?)\s*$", line)
        if id_match is not None:
            plugin_id = id_match.group(1).strip()
            continue
        version_match = re.match(r"^//\s*@version\s*:\s*(\d+)\s*$", line)
        if version_match is not None:
            plugin_version = max(1, int(version_match.group(1)))
    try:
        module = ast.parse(source_text)
        for node in module.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = next(
                (target.id for target in targets if isinstance(target, ast.Name)), ""
            )
            if name not in {"PLUGIN_ID", "PLUGIN_VERSION"}:
                continue
            value = ast.literal_eval(node.value)
            if name == "PLUGIN_ID" and isinstance(value, str) and value.strip():
                plugin_id = value.strip()
            elif name == "PLUGIN_VERSION" and isinstance(value, int):
                plugin_version = max(1, value)
    except (SyntaxError, ValueError):
        pass
    return plugin_id, plugin_version


def _raw_github_url(owner: str, repo: str, branch: str, relative_path: str) -> str:
    encoded_parts = [quote(part) for part in PurePosixPath(relative_path).parts]
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{'/'.join(encoded_parts)}"


def _extract_github_default_branch_from_html(html: str) -> str:
    for pattern in (
        r'<meta[^>]+name=["\']octolytics-dimension-repository_default_branch["\'][^>]+content=["\']([^"\']+)["\']',
        r'"defaultBranch"\s*:\s*"([^"]+)"',
        r'"default_branch"\s*:\s*"([^"]+)"',
    ):
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match is None:
            continue
        branch = str(match.group(1) or "").strip()
        if branch:
            return branch
    raise ValueError("无法解析仓库默认分支")


def _resolve_manifest_entry_source_url(manifest_url: str, file_path: str) -> str | None:
    parsed = urlparse(file_path)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return file_path
    path = PurePosixPath(file_path)
    if path.is_absolute() or ".." in path.parts:
        return None
    return urljoin(manifest_url, quote(file_path))


class SpiderPluginManager:
    def __init__(
        self,
        repository: SpiderPluginRepository,
        loader: SpiderPluginLoader,
        playback_history_repository=None,
        *,
        danmaku_preference_store: DanmakuSeriesPreferenceStore | None = None,
        get=httpx.get,
    ) -> None:
        self._repository = repository
        self._loader = loader
        self._playback_history_repository = playback_history_repository
        self._get = get
        self._playback_parser_service = None
        self._yt_dlp_service = None
        self._preferred_parse_key_loader = None
        self._base_url_loader = None
        self._danmaku_service = None
        self._danmaku_preference_store = (
            danmaku_preference_store or DanmakuSeriesPreferenceStore()
        )
        self._metadata_hydrator_factory = None
        self._metadata_scrape_service_factory = None
        self._episode_title_enhancer_factory = None
        # 刷新加载出的实例暂存:refresh_plugin 加载一次后,随后的 tab 重建
        # (load_plugins/iter_enabled_plugins) 校验源未变即复用,免去二次 exec+init。
        self._refreshed_loads: dict[int, tuple[LoadedSpiderPlugin, tuple[int, int]]] = (
            {}
        )

    def list_plugins(self) -> list[SpiderPluginConfig]:
        return self._repository.list_plugins()

    def backfill_source_metadata(self) -> int:
        """Backfill stable ids for plugins installed before PLUGIN_ID existed."""
        updated = 0
        for plugin in self._repository.list_plugins():
            if plugin.manifest_id:
                continue
            source_path = Path(
                plugin.source_value
                if plugin.source_type == "local"
                else plugin.cached_file_path
            )
            if source_path.suffix.lower() not in {".py", ".txt"} or not source_path.is_file():
                continue
            try:
                manifest_id, _ = _parse_plugin_source_metadata(
                    source_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError):
                continue
            if not manifest_id:
                continue
            existing = self._repository.find_plugin_by_manifest_id(manifest_id)
            if existing is not None and existing.id != plugin.id:
                continue
            self._repository.update_plugin(
                plugin.id,
                display_name=plugin.display_name,
                enabled=plugin.enabled,
                cached_file_path=plugin.cached_file_path,
                last_loaded_at=plugin.last_loaded_at,
                last_error=plugin.last_error,
                config_text=plugin.config_text,
                plugin_version=plugin.plugin_version,
                category_overrides_json=plugin.category_overrides_json,
                source_type=plugin.source_type,
                source_value=plugin.source_value,
                manifest_id=manifest_id,
            )
            updated += 1
        return updated

    def _upsert_single_plugin(
        self,
        source_type: str,
        source_value: str,
        display_name: str,
        *,
        manifest_id: str = "",
        plugin_version: int = 1,
    ) -> SpiderPluginConfig:
        existing = self._repository.find_plugin_by_manifest_id(manifest_id)
        if existing is None:
            return self._repository.add_plugin(
                source_type,
                source_value,
                display_name,
                plugin_version=plugin_version,
                manifest_id=manifest_id,
            )
        self._repository.update_plugin(
            existing.id,
            source_type=source_type,
            source_value=source_value,
            display_name=existing.display_name,
            enabled=existing.enabled,
            cached_file_path=existing.cached_file_path,
            last_loaded_at=existing.last_loaded_at,
            last_error=existing.last_error,
            config_text=existing.config_text,
            plugin_version=plugin_version,
            category_overrides_json=existing.category_overrides_json,
            manifest_id=manifest_id,
        )
        return self._repository.get_plugin(existing.id)

    def add_local_plugin(self, path: str) -> None:
        manifest_id = ""
        plugin_version = 1
        if Path(path).suffix.lower() in {".py", ".txt"}:
            manifest_id, plugin_version = _parse_plugin_source_metadata(
                Path(path).read_text(encoding="utf-8")
            )
        plugin = self._upsert_single_plugin(
            "local",
            path,
            Path(path).stem,
            manifest_id=manifest_id,
            plugin_version=plugin_version,
        )
        self.refresh_plugin(plugin.id)

    def add_remote_plugin(self, url: str) -> None:
        name = _default_plugin_name("remote", url)
        manifest_id = ""
        plugin_version = 1
        if Path(urlparse(url).path).suffix.lower() in {".py", ".txt"}:
            try:
                manifest_id, plugin_version = _parse_plugin_source_metadata(
                    self._fetch_text(url)
                )
            except Exception:
                manifest_id = ""
                plugin_version = 1
        plugin = self._upsert_single_plugin(
            "remote",
            url,
            name,
            manifest_id=manifest_id,
            plugin_version=plugin_version,
        )
        self.refresh_plugin(plugin.id)

    def rename_plugin(self, plugin_id: int, display_name: str) -> None:
        self._repository.rename_plugin(plugin_id, display_name)

    def set_plugin_enabled(self, plugin_id: int, enabled: bool) -> None:
        self._repository.set_plugin_enabled(plugin_id, enabled)

    def set_plugin_config(self, plugin_id: int, config_text: str) -> None:
        self._repository.set_plugin_config(plugin_id, config_text)

    def set_plugin_category_overrides(self, plugin_id: int, category_overrides_json: str) -> None:
        self._repository.set_plugin_category_overrides(plugin_id, category_overrides_json)

    def move_plugin(self, plugin_id: int, direction: int) -> None:
        self._repository.move_plugin(plugin_id, direction)

    def reorder_plugins(self, plugin_ids_in_order: list[int]) -> None:
        self._repository.reorder_plugins(plugin_ids_in_order)

    def refresh_plugin(self, plugin_id: int) -> None:
        plugin = self._repository.get_plugin(plugin_id)
        try:
            loaded = self._loader.load(plugin, force_refresh=True)
        except Exception as exc:
            self._refreshed_loads.pop(plugin_id, None)
            self._repository.update_plugin(
                plugin_id,
                display_name=plugin.display_name,
                enabled=plugin.enabled,
                cached_file_path=plugin.cached_file_path,
                last_loaded_at=plugin.last_loaded_at,
                last_error=str(exc),
                config_text=plugin.config_text,
                plugin_version=plugin.plugin_version,
                category_overrides_json=plugin.category_overrides_json,
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
            category_overrides_json=plugin.category_overrides_json,
            manifest_id=loaded.config.manifest_id,
        )
        identity = _source_file_identity(self._plugin_source_path(plugin, loaded))
        if identity is None:
            self._refreshed_loads.pop(plugin_id, None)
        else:
            self._refreshed_loads[plugin_id] = (loaded, identity)

    @staticmethod
    def _plugin_source_path(
        plugin: SpiderPluginConfig,
        loaded: LoadedSpiderPlugin,
    ) -> Path:
        if plugin.source_type == "local":
            return Path(plugin.source_value)
        return Path(loaded.config.cached_file_path or plugin.cached_file_path or "")

    def _load_plugin_for_definition(
        self,
        plugin: SpiderPluginConfig,
        *,
        initialize: bool,
    ) -> LoadedSpiderPlugin:
        stashed = self._refreshed_loads.pop(plugin.id, None)
        if stashed is not None:
            loaded, identity = stashed
            # 任一条件变化即作废:源文件被改(mtime/size)、来源或配置变了
            # (initialize_spider 闭包捕获的是旧 config_text,不能复用)。
            if (
                plugin.source_type == loaded.config.source_type
                and plugin.source_value == loaded.config.source_value
                and plugin.config_text == loaded.config.config_text
                and _source_file_identity(
                    self._plugin_source_path(plugin, loaded)
                ) == identity
            ):
                return loaded
        return self._loader.load(plugin, initialize=initialize)

    def delete_plugin(self, plugin_id: int) -> None:
        self._refreshed_loads.pop(plugin_id, None)
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
        api_error: Exception | None = None
        try:
            payload = self._fetch_json(f"https://api.github.com/repos/{owner}/{repo}")
            if isinstance(payload, dict):
                branch = str(payload.get("default_branch") or "").strip()
                if branch:
                    return branch
            api_error = ValueError("无法解析仓库默认分支")
        except Exception as exc:
            api_error = exc

        try:
            repo_html = self._fetch_text(f"https://github.com/{owner}/{repo}")
            return _extract_github_default_branch_from_html(repo_html)
        except Exception:
            if api_error is not None:
                raise api_error
            raise

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
            category_overrides_json=plugin.category_overrides_json,
            drive_detail_loader=drive_detail_loader,
            offline_download_detail_loader=offline_download_detail_loader,
            playback_parser_service=self._playback_parser_service,
            yt_dlp_service=self._yt_dlp_service,
            preferred_parse_key_loader=self._preferred_parse_key_loader,
            base_url_loader=self._base_url_loader,
            metadata_hydrator_factory=self._metadata_hydrator_factory,
            metadata_scrape_service_factory=self._metadata_scrape_service_factory,
            episode_title_enhancer_factory=self._episode_title_enhancer_factory,
            danmaku_service=self._danmaku_service,
            danmaku_preference_store=self._danmaku_preference_store,
            spider_initializer=loaded.initialize_spider,
            plugin_log_writer=lambda message, plugin_id=plugin.id: self._append_plugin_log(plugin_id, "info", message),
            playback_history_loader=None
            if self._playback_history_repository is None
            else lambda vod_id, vod_name="", plugin_id=plugin.id: self._playback_history_repository.get_history(
                "spider_plugin",
                vod_id,
                source_key=str(plugin_id),
                vod_name=vod_name,
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

    def load_plugin_categories(self, plugin_id: int) -> list[DoubanCategory]:
        plugin, loaded = self._load_plugin(plugin_id)
        controller = SpiderPluginController(
            loaded.spider,
            plugin_name=self._plugin_title(plugin, loaded),
            search_enabled=loaded.search_enabled,
            category_overrides_json=plugin.category_overrides_json,
            spider_initializer=loaded.initialize_spider,
        )
        return controller.load_raw_categories()

    def list_plugin_actions(self, plugin_id: int) -> list[SpiderPluginAction]:
        plugin, loaded = self._load_plugin(plugin_id)
        get_actions = getattr(loaded.spider, "getManagerActions", None)
        if not callable(get_actions):
            return []
        actions: list[SpiderPluginAction] = []
        for payload in get_actions() or []:
            action = coerce_spider_plugin_action(payload)
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
        return self.import_plugins(
            repo_url,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )

    def import_plugins(
        self,
        source_url: str,
        *,
        progress_callback: Callable[[SpiderPluginImportProgress], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> SpiderPluginImportResult:
        result = SpiderPluginImportResult()
        source_url = source_url.strip()
        if _is_github_repo_url(source_url):
            owner, repo = _parse_github_repo(source_url)
            self._raise_if_import_cancelled(cancel_callback, result)
            self._emit_import_progress(progress_callback, stage="resolve_repo", message="正在解析仓库信息")
            self._raise_if_import_cancelled(cancel_callback, result)
            default_branch = self._load_github_default_branch(owner, repo)
            manifest_url = _raw_github_url(owner, repo, default_branch, "spiders_v2.json")
            return self._import_manifest(
                manifest_url,
                result,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
            )
        manifest_url = _parse_manifest_source_url(source_url)
        return self._import_manifest(
            manifest_url,
            result,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )

    def _import_manifest(
        self,
        manifest_url: str,
        result: SpiderPluginImportResult,
        *,
        progress_callback: Callable[[SpiderPluginImportProgress], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> SpiderPluginImportResult:
        self._emit_import_progress(progress_callback, stage="fetch_manifest", message="正在读取 spiders_v2.json")
        self._raise_if_import_cancelled(cancel_callback, result)
        manifest = self._fetch_json(manifest_url)
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
            plugin_version = _parse_manifest_plugin_version(entry)
            if plugin_version is None:
                result.skipped_count += 1
                continue
            manifest_id = _parse_manifest_plugin_id(entry)
            try:
                source_url = _resolve_manifest_entry_source_url(manifest_url, file_path)
                if source_url is None:
                    result.skipped_count += 1
                    continue
                self._raise_if_import_cancelled(cancel_callback, result)
                existing = self._repository.find_plugin_by_manifest_id(manifest_id)
                matched_by_manifest_id = existing is not None
                if existing is None:
                    existing = self._repository.find_plugin_by_source_value(source_url)
                if existing is not None and existing.plugin_version == plugin_version:
                    if matched_by_manifest_id or (
                        existing.source_value == source_url
                        and existing.manifest_id == manifest_id
                    ):
                        result.skipped_count += 1
                        continue
                self._fetch_text(source_url)
                if existing is None:
                    plugin = self._repository.add_plugin(
                        "remote",
                        source_url,
                        _default_plugin_name("remote", source_url),
                        enabled=bool(entry.get("valid", True)),
                        plugin_version=plugin_version,
                        manifest_id=manifest_id,
                    )
                    result.imported_count += 1
                    self._raise_if_import_cancelled(cancel_callback, result)
                    self.refresh_plugin(plugin.id)
                    continue
                self._repository.update_plugin(
                    existing.id,
                    display_name=existing.display_name,
                    enabled=bool(entry.get("valid", True)) if matched_by_manifest_id else existing.enabled,
                    cached_file_path=existing.cached_file_path,
                    last_loaded_at=existing.last_loaded_at,
                    last_error=existing.last_error,
                    config_text=existing.config_text,
                    plugin_version=plugin_version,
                    category_overrides_json=existing.category_overrides_json,
                    source_type="remote",
                    source_value=source_url,
                    manifest_id=manifest_id,
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
        initialize_plugins: bool = True,
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
                loaded = self._load_plugin_for_definition(plugin, initialize=initialize_plugins)
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
                    category_overrides_json=plugin.category_overrides_json,
                )
                self._repository.append_log(plugin.id, "error", str(exc))
                continue
            self._persist_loaded_manifest_id(plugin, loaded)
            yield self._build_plugin_definition(
                plugin,
                    loaded,
                    drive_detail_loader=drive_detail_loader,
                    offline_download_detail_loader=offline_download_detail_loader,
                )

    def load_enabled_plugins(
        self,
        drive_detail_loader=None,
        offline_download_detail_loader=None,
        *,
        initialize_plugins: bool = True,
    ) -> list[SpiderPluginDefinition]:
        definitions: list[SpiderPluginDefinition] = []
        for definition in self.iter_enabled_plugins(
            drive_detail_loader=drive_detail_loader,
            offline_download_detail_loader=offline_download_detail_loader,
            initialize_plugins=initialize_plugins,
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
                loaded = self._load_plugin_for_definition(plugin, initialize=True)
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
                    category_overrides_json=plugin.category_overrides_json,
                )
                self._repository.append_log(plugin.id, "error", str(exc))
                continue
            self._persist_loaded_manifest_id(plugin, loaded)
            definitions.append(
                self._build_plugin_definition(
                    plugin,
                    loaded,
                    drive_detail_loader=drive_detail_loader,
                    offline_download_detail_loader=offline_download_detail_loader,
                )
            )
        return definitions

    def _persist_loaded_manifest_id(
        self, plugin: SpiderPluginConfig, loaded: LoadedSpiderPlugin
    ) -> None:
        manifest_id = loaded.config.manifest_id.strip()
        if not manifest_id or manifest_id == plugin.manifest_id:
            return
        self._repository.update_plugin(
            plugin.id,
            display_name=plugin.display_name,
            enabled=plugin.enabled,
            cached_file_path=loaded.config.cached_file_path,
            last_loaded_at=plugin.last_loaded_at,
            last_error=plugin.last_error,
            config_text=plugin.config_text,
            plugin_version=plugin.plugin_version,
            category_overrides_json=plugin.category_overrides_json,
            source_type=plugin.source_type,
            source_value=plugin.source_value,
            manifest_id=manifest_id,
        )


__all__ = [
    "LoadedSpiderPlugin",
    "SpiderPluginLoader",
    "SpiderPluginDefinition",
    "SpiderPluginManager",
]
