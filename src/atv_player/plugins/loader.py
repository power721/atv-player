from __future__ import annotations

import importlib.util
import logging
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import httpx

from atv_player.models import SpiderPluginConfig
import atv_player.plugins.compat.base.spider as compat_spider_module
from atv_player.plugins.compat.base.spider import Spider as CompatSpider


logger = logging.getLogger(__name__)


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
        compat_spider_module.set_cache_root(self._cache_dir / "spider-cache")
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
            spider.init(config.config_text)
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
            config_text=config.config_text,
        )
        logger.info(
            "Loaded spider plugin id=%s name=%s source_type=%s search_enabled=%s",
            config.id,
            plugin_name or config.display_name or source_path.stem,
            config.source_type,
            search_enabled,
        )
        return LoadedSpiderPlugin(
            config=updated_config,
            spider=spider,
            plugin_name=plugin_name,
            search_enabled=search_enabled,
        )

    def _install_compat_modules(self) -> None:
        base_package = types.ModuleType("base")
        spider_module = sys.modules["atv_player.plugins.compat.base.spider"]
        setattr(base_package, "spider", spider_module)
        sys.modules["base"] = base_package
        sys.modules["base.spider"] = spider_module

    def _resolve_source_path(self, config: SpiderPluginConfig, force_refresh: bool) -> Path:
        if config.source_type == "local":
            return Path(config.source_value)
        cache_path = self._cache_dir / f"plugin_{config.id}.py"
        if not force_refresh and config.cached_file_path:
            cached = Path(config.cached_file_path)
            if cached.is_file() and cached.stat().st_size > 0:
                logger.info("Use cached spider plugin id=%s path=%s", config.id, cached)
                return cached
        try:
            logger.info(
                "Download spider plugin id=%s source=%s force_refresh=%s",
                config.id,
                config.source_value,
                force_refresh,
            )
            response = self._get(config.source_value, timeout=15.0, follow_redirects=True)
            if response.status_code >= 300:
                raise httpx.HTTPStatusError(
                    f"Error response {response.status_code} while requesting {config.source_value}",
                    request=response.request,
                    response=response,
                )
            cache_path.write_text(response.text, encoding="utf-8")
            return cache_path
        except Exception:
            if cache_path.is_file() and cache_path.stat().st_size > 0:
                logger.warning("Spider plugin refresh failed, fallback to cache id=%s path=%s", config.id, cache_path)
                return cache_path
            raise
