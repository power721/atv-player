from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import gc
import httpx
import inspect
import os
import threading
import time
import logging
import re
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QInputDialog, QPushButton, QToolButton, QWidget

from atv_player.api import ApiClient, ApiError, UnauthorizedError
from atv_player.ai import (
    AIEnrichmentService,
    AIProviderConfig,
    OpenAICompatibleClient,
    SmartSearchIntentParser,
)
from atv_player.danmaku.cache import purge_stale_danmaku_cache
from atv_player.danmaku.direct_parse import load_direct_parse_danmaku
from atv_player.danmaku.generic import GenericDanmakuController
from atv_player.danmaku.preferences import DanmakuSeriesPreferenceStore
from atv_player.danmaku.service import create_default_danmaku_service
from atv_player.subtitles.service import create_default_subtitle_service
from atv_player.custom_live_service import CustomLiveService
from atv_player.controllers.browse_controller import BrowseController
from atv_player.controllers.favorites_controller import FavoritesController
from atv_player.controllers.following_controller import FollowingController
from atv_player.controllers.douban_controller import DoubanController
from atv_player.controllers.global_catalog_controller import GlobalCatalogController
from atv_player.controllers.media_detail_controller import MediaDetailController
from atv_player.controllers.bilibili_controller import BilibiliController
from atv_player.controllers.emby_controller import EmbyController
from atv_player.controllers.feiniu_controller import FeiniuController
from atv_player.controllers.jellyfin_controller import JellyfinController
from atv_player.controllers.live_controller import LiveController
from atv_player.controllers.history_controller import HistoryController
from atv_player.controllers.login_controller import LoginController
from atv_player.controllers.player_controller import PlayerController
from atv_player.controllers.pansou_controller import PansouController
from atv_player.controllers.msub_controller import MsubController
from atv_player.controllers.telegram_search_controller import TelegramSearchController
from atv_player.controllers.telegram_channel_controller import TelegramChannelController
from atv_player.controllers.youtube_category_config import load_youtube_category_config
from atv_player.controllers.youtube_controller import YouTubeController, default_youtube_categories
from atv_player.crash_diagnostics import install_crash_diagnostics
from atv_player.danmaku.utils import (
    infer_playlist_episode_number,
    is_likely_variety_title,
    is_variety_collection,
)
from atv_player.diagnostics import resolve_app_version
from atv_player.episode_titles import (
    episode_version_slots_by_index,
    extract_season_number,
    normalize_episode_title_text,
    playlist_has_title_variants,
    seed_original_titles,
)
from atv_player.live_epg_repository import LiveEpgRepository
from atv_player.live_epg_service import LiveEpgService
from atv_player.local_playback_history import LocalPlaybackHistoryRepository
from atv_player.favorite_tmdb_bindings import FavoriteTMDBBindingRepository
from atv_player.favorites_repository import FavoritesRepository
from atv_player.following_backend import FollowingBackendSyncService
from atv_player.following_metadata import FollowingMetadataGateway
from atv_player.following_repository import FollowingRepository
from atv_player.following_update_service import FollowingUpdateService
from atv_player.playback_sync_service import PlaybackHistorySyncService
from atv_player.heat import HeatController, HeatService
from atv_player.metadata import (
    METADATA_EPISODE_TITLE_SOURCE_PRIORITY,
    EpisodeTitleOverrideRepository,
    MetadataBindingRepository,
    MetadataCache,
    MetadataContext,
    MetadataHydrator,
    apply_episode_title_overrides,
    build_provider_episode_playlist,
    resolve_episode_title_source_priority,
)
from atv_player.metadata.discovery import TMDBDiscoveryService
from atv_player.metadata.bindings import is_query_redirect_binding
from atv_player.metadata.matching import is_confident_match, score_match
from atv_player.metadata.models import MetadataMatch
from atv_player.metadata.providers.bangumi import BangumiMetadataProvider
from atv_player.metadata.providers.bangumi_client import BangumiClient
from atv_player.metadata.providers.bilibili import BilibiliMetadataProvider
from atv_player.metadata.providers.iqiyi import IqiyiMetadataProvider
from atv_player.metadata.providers.official_douban import OfficialDoubanProvider
from atv_player.metadata.providers.official_douban_client import LocalDoubanClient
from atv_player.metadata.providers.plugin import CustomPluginProvider
from atv_player.metadata.providers.local_douban import LocalDoubanProvider
from atv_player.metadata.providers.sohu import SohuMetadataProvider
from atv_player.metadata.scrape import MetadataScrapeService, _match_media_kind, _query_media_kind
from atv_player.metadata.providers.tencent import TencentMetadataProvider
from atv_player.metadata.providers.tmdb import TMDBProvider, infer_tmdb_media_type
from atv_player.metadata.providers.tmdb_client import TMDBClient
from atv_player.metadata.providers.youku import YoukuMetadataProvider
from atv_player.metadata.query import infer_metadata_category_name_from_title, is_short_drama_collection
from atv_player.models import AppConfig, LiveEpgConfig, PlayItem, VodItem
from atv_player.network_proxy import ProxyConfig, ProxyDecider, build_httpx_kwargs_for_url
from atv_player.paths import app_cache_dir, app_data_dir
from atv_player.live_source_repository import LiveSourceRepository
from atv_player.log_store import AppLogService, StructuredJsonlHandler
from atv_player.logging_utils import configure_logging
from atv_player.plugins import SpiderPluginLoader, SpiderPluginManager
from atv_player.plugins.compat.base.spider import set_proxy_decider_loader as set_spider_proxy_decider_loader
from atv_player.plugins.repository import SpiderPluginRepository
from atv_player.playback_parsers import BuiltInPlaybackParserService
from atv_player.player.m3u8_ad_filter import M3U8AdFilter
from atv_player.proxy.server import LocalHlsProxyServer
from atv_player.search import SmartSearchController
from atv_player.yt_dlp_service import YtdlpPlaybackService
from atv_player.storage import SettingsRepository
from atv_player.time_utils import is_refresh_stale
from atv_player.ui.poster_loader import set_proxy_decider_loader
from atv_player.ui.login_window import LoginWindow
from atv_player.ui.main_window import MainWindow, load_direct_parse_detail
from atv_player.ui.icon_cache import load_icon
from atv_player.ui.theme import ThemeManager, install_theme

POSTER_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
POSTER_CACHE_PURGE_INTERVAL_SECONDS = 24 * 60 * 60
_POSTER_CACHE_PURGE_MARKER_NAME = ".last_poster_cache_purge"
_MAIN_THREAD_GC_INTERVAL_MS = 30_000
_METADATA_SEARCH_CACHE_TTL_SECONDS = 7 * 24 * 3600
_METADATA_EMPTY_SEARCH_CACHE_TTL_SECONDS = 3600
_METADATA_DETAIL_CACHE_TTL_SECONDS = 7 * 24 * 3600
_EPISODE_SORT_SENTINEL = 10**9
_EPISODE_TITLE_PLAYLIST_CACHE_VERSION = "v4"
_QUALITY_VARIANT_EPISODE_RE = re.compile(
    r"(?:^|[\s\-_.])0*(\d{1,3})\s*[-_. ~～〜]\s*(?:4k|2160p|1080p|720p|480p|360p)\b",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)


def _build_tmdb_client(*, api_key: str, proxy_base_url: str = "", proxy_decider=None):
    kwargs = {
        "api_key": api_key,
        "proxy_decider": proxy_decider,
    }
    normalized_proxy_base_url = str(proxy_base_url or "").strip()
    if normalized_proxy_base_url:
        kwargs["proxy_base_url"] = normalized_proxy_base_url
    return TMDBClient(**kwargs)


def _has_tmdb_access(config: AppConfig) -> bool:
    return bool(
        str(config.metadata_tmdb_api_key or "").strip()
        or str(config.metadata_tmdb_proxy_base_url or "").strip()
    )


class _NullPluginManager:
    def load_enabled_plugins(self, drive_detail_loader=None) -> list:
        del drive_detail_loader
        return []


class _NullLiveSourceRepository:
    def list_sources(self) -> list:
        return []


class _NullLiveEpgService:
    def load_config(self) -> LiveEpgConfig:
        return LiveEpgConfig()

    def save_url(self, epg_url: str) -> None:
        del epg_url

    def refresh(self) -> None:
        return None

    def get_schedule(self, channel_name: str):
        del channel_name
        return None


class _HttpTextClient:
    def __init__(self, client: ApiClient) -> None:
        self._client = client

    def get_text(self, url: str) -> str:
        return self._client.get_text(url)

    def get_bytes(self, url: str) -> bytes:
        return self._client.get_bytes(url)


class _ButtonCursorEventFilter(QObject):
    def eventFilter(self, watched, event) -> bool:
        del event
        if isinstance(watched, (QPushButton, QToolButton)) and watched.cursor().shape() != Qt.CursorShape.PointingHandCursor:
            watched.setCursor(Qt.CursorShape.PointingHandCursor)
        return False


class _ActivationPullFilter(QObject):
    """主窗口重新激活时请求一次播放记录 PULL + 服务端追剧信号同步(均各自限频)。"""

    def __init__(self, callback, parent=None) -> None:
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.ActivationChange:
            window = watched
            if callable(getattr(window, "isActiveWindow", None)) and window.isActiveWindow():
                try:
                    self._callback()
                except Exception:  # noqa: BLE001 - 同步触发失败不应影响窗口事件
                    logger.exception("playback sync pull_soon failed")
        return False


def decide_start_view(config: AppConfig) -> str:
    return "main" if config.token else "login"


def _app_icon_path() -> Path:
    return Path(__file__).resolve().parent / "icons" / "app.svg"


def purge_stale_poster_cache(now: float | None = None) -> None:
    cache_dir = app_cache_dir() / "posters"
    cache_dir.mkdir(parents=True, exist_ok=True)
    current_time = now if now is not None else time.time()
    marker_path = cache_dir / _POSTER_CACHE_PURGE_MARKER_NAME
    try:
        if marker_path.exists() and current_time - marker_path.stat().st_mtime < POSTER_CACHE_PURGE_INTERVAL_SECONDS:
            return
    except OSError:
        pass

    cutoff = current_time - POSTER_CACHE_MAX_AGE_SECONDS
    for entry in cache_dir.iterdir():
        try:
            if entry.name == _POSTER_CACHE_PURGE_MARKER_NAME:
                continue
            if not entry.is_file():
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue
    try:
        marker_path.touch()
        os.utime(marker_path, (current_time, current_time))
    except OSError:
        pass


def _install_button_pointing_hand_cursor(app: QApplication) -> None:
    if not hasattr(app, "installEventFilter"):
        return
    filter_obj = _ButtonCursorEventFilter(app)
    app.installEventFilter(filter_obj)
    setattr(app, "_button_cursor_event_filter", filter_obj)


def _install_main_thread_gc_workaround(app: QApplication) -> None:
    if tuple(sys.version_info[:2]) < (3, 14) or not gc.isenabled():
        return
    gc.disable()
    timer = QTimer(app if isinstance(app, QObject) else None)
    timer.setInterval(_MAIN_THREAD_GC_INTERVAL_MS)
    timer.timeout.connect(gc.collect)
    timer.start()
    setattr(app, "_main_thread_gc_timer", timer)
    logger.warning(
        "Enabled Python 3.14 GC workaround: automatic GC disabled, using main-thread periodic collection",
    )


def _path_basename(value: str) -> str:
    text = str(value or "").strip().rstrip("/\\")
    if not text:
        return ""
    return re.split(r"[\\/]", text)[-1]


def _extract_quality_variant_episode_number(item: PlayItem) -> int | None:
    seen: set[str] = set()
    for value in (item.original_title, item.title, item.path):
        candidate = _path_basename(str(value or ""))
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        match = _QUALITY_VARIANT_EPISODE_RE.search(candidate)
        if match is None:
            continue
        try:
            episode_number = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if episode_number > 0:
            return episode_number
    return None


def _extract_series_title_candidate(value: object) -> str:
    text = _path_basename(str(value or "").strip())
    if not text:
        return ""
    text = re.sub(r"\.(mkv|mp4|avi|mov|m4v|ts|flv)\b.*$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"[\[(（【].*$", "", text).strip()
    for pattern in (
        r"[.\s_-]*S\d+\s*E\d+.*$",
        r"[.\s_-]*第\s*[0-9零一二两三四五六七八九十百千]+\s*[集话期部回].*$",
        r"[.\s_-]*0*\d{1,4}\s*[-_. ~～〜]\s*(?:4k|2160p|1080p|720p|480p|360p)\b.*$",
    ):
        stripped = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
        if stripped != text:
            text = stripped
            break
    text = re.sub(r"\s*[\(（\[【]\s*(?:19|20)\d{2}\s*[\)）\]】]\s*$", "", text).strip() or text
    text = re.sub(r"[丨｜|]", "", text)
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_")
    if not text or text.isdigit():
        return ""
    return text


def _infer_series_title_from_playlist(playlist: list[PlayItem]) -> str:
    candidate_scores: dict[str, tuple[int, int, str]] = {}
    for item in playlist:
        values = [
            _path_basename(item.path),
            _path_basename(str(item.path or "").strip().rsplit("/", 1)[0]),
            item.original_title,
            item.title,
        ]
        for value in values:
            candidate = _extract_series_title_candidate(value)
            if not candidate:
                continue
            normalized = re.sub(r"\s+", "", candidate).lower()
            if not normalized:
                continue
            count, length, stored = candidate_scores.get(normalized, (0, 0, candidate))
            preferred = candidate if len(candidate) >= len(stored) else stored
            candidate_scores[normalized] = (count + 1, max(length, len(candidate)), preferred)
    if not candidate_scores:
        return ""
    _normalized, (_count, _length, candidate) = max(
        candidate_scores.items(),
        key=lambda entry: (entry[1][0], entry[1][1], entry[0]),
    )
    return candidate


def _extract_series_year_candidates(value: object) -> list[str]:
    text = _path_basename(str(value or "").strip())
    if not text:
        return []
    return re.findall(r"[\(（\[【]\s*((?:19|20)\d{2})\s*[\)）\]】]", text)


def _infer_series_year_from_playlist(playlist: list[PlayItem]) -> str:
    year_scores: dict[str, int] = {}
    for item in playlist:
        values = [
            (_path_basename(str(item.path or "").strip().rsplit("/", 1)[0]), 3),
            (_path_basename(item.path), 1),
            (item.original_title, 1),
            (item.title, 1),
        ]
        for value, weight in values:
            for year in _extract_series_year_candidates(value):
                year_scores[year] = year_scores.get(year, 0) + weight
    if not year_scores:
        return ""
    return max(year_scores.items(), key=lambda entry: (entry[1], entry[0]))[0]


def _is_bilibili_metadata_enhancement_id(vod_id: object) -> bool:
    text = str(vod_id or "").strip().lower()
    return text.startswith(("ss", "ep", "season$"))


def _has_bilibili_season_id_detail_field(vod: object) -> bool:
    for field in list(getattr(vod, "detail_fields", []) or []):
        if str(getattr(field, "label", "") or "").strip().lower() != "season id":
            continue
        for part in list(getattr(field, "value_parts", []) or []):
            action = getattr(part, "action", None)
            action_value = str(getattr(action, "value", "") or "").strip().lower()
            if action_value.startswith(("ss", "season$")):
                return True
            if str(getattr(part, "label", "") or "").strip().isdigit():
                return True
        value = str(getattr(field, "value", "") or "").strip().lower()
        if value.startswith(("ss", "season$")) or value.isdigit():
            return True
    return False


def _supports_bilibili_metadata_enhancement(vod: object | None) -> bool:
    if vod is None:
        return False
    return _is_bilibili_metadata_enhancement_id(getattr(vod, "vod_id", "")) or _has_bilibili_season_id_detail_field(vod)


def build_application() -> tuple[QApplication, SettingsRepository, AppLogService]:
    app_instance_getter = getattr(QApplication, "instance", None)
    app = app_instance_getter() if callable(app_instance_getter) else None
    if app is None:
        app = QApplication(sys.argv)
    _install_button_pointing_hand_cursor(app)
    _install_main_thread_gc_workaround(app)
    app.setApplicationName("atv-player")
    if hasattr(app, "setApplicationVersion"):
        app.setApplicationVersion(resolve_app_version())
    app.setWindowIcon(load_icon(_app_icon_path()))
    data_dir = app_data_dir()
    repo = SettingsRepository(data_dir / "app.db")
    config = repo.load_config()
    repo.ensure_app_identity()
    app_log_service = AppLogService(
        data_dir / "logs",
        enabled_getter=lambda: repo.load_config().logging_enabled,
        max_bytes=10 * 1024 * 1024,
        max_archives=5,
    )
    if not sys.platform.startswith("win"):
        install_crash_diagnostics(app_log_service.active_path.parent)
    configure_logging("INFO", StructuredJsonlHandler(app_log_service))
    install_theme(app, ThemeManager(), config.theme_mode)
    purge_stale_poster_cache()
    threading.Thread(target=purge_stale_danmaku_cache, daemon=True).start()
    logger.info(
        "Application initialized data_dir=%s",
        data_dir,
        extra={"log_category": "app", "log_source": "app"},
    )
    return app, repo, app_log_service


def apply_saved_theme(app: QApplication | None, repo: SettingsRepository) -> str:
    target_app = app or QApplication.instance()
    if target_app is None:
        return "light"
    manager = getattr(target_app, "_theme_manager", None)
    if manager is None:
        manager = ThemeManager()
        setattr(target_app, "_theme_manager", manager)
    resolved = install_theme(target_app, manager, repo.load_config().theme_mode)
    refresh_widgets = getattr(target_app, "topLevelWidgets", None)
    if callable(refresh_widgets):
        for widget in refresh_widgets():
            refresh_window_chrome = getattr(widget, "refresh_window_chrome", None)
            if callable(refresh_window_chrome):
                refresh_window_chrome()
            apply_theme = getattr(widget, "_apply_theme", None)
            if callable(apply_theme):
                apply_theme()
    return resolved


class AppCoordinator(QObject):
    def __init__(
        self,
        repo: SettingsRepository,
        *,
        app_log_service: AppLogService | None = None,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._app_log_service = app_log_service
        self.login_window: LoginWindow | None = None
        self.main_window: MainWindow | None = None
        self._playback_sync_service: PlaybackHistorySyncService | None = None
        self._following_backend_sync_service: FollowingBackendSyncService | None = None
        self._activation_pull_filter: _ActivationPullFilter | None = None
        self._api_client: ApiClient | None = None
        self._vod_token_options: list[str] = []
        self._vod_token_is_admin = False
        initial_config = self.repo.load_config()
        set_proxy_decider_loader(self._build_proxy_decider)
        set_spider_proxy_decider_loader(self._build_proxy_decider)
        self._m3u8_ad_filter = M3U8AdFilter(
            proxy_server=LocalHlsProxyServer(
                get=self._proxy_http_get(),
                stream=self._proxy_http_stream(),
                segment_prefetch_size=initial_config.m3u_proxy_segment_prefetch_size,
                ad_filter_mode=initial_config.m3u8_ad_filter_mode,
            ),
            get=self._proxy_http_get(),
        )
        self._playback_parser_service = BuiltInPlaybackParserService(
            get=self._proxy_http_get(),
            post=self._proxy_http_post(),
        )
        self._yt_dlp_service = YtdlpPlaybackService(
            proxy_decider=self._build_proxy_decider(),
            config_loader=self.repo.load_config,
        )
        self._danmaku_service = create_default_danmaku_service(
            get=self._proxy_http_get(),
            post=self._proxy_http_post(),
            disabled_provider_ids_loader=lambda: self.repo.load_config().disabled_danmaku_provider_ids,
            config_loader=self.repo.load_config,
        )
        self._danmaku_preference_store = DanmakuSeriesPreferenceStore()
        self._subtitle_search_service = create_default_subtitle_service(
            get=self._proxy_http_get(),
            post=self._proxy_http_post(),
            config_loader=self.repo.load_config,
            disabled_provider_ids_loader=lambda: self.repo.load_config().disabled_subtitle_provider_ids,
        )
        if hasattr(repo, "database_path"):
            self._live_source_repository = LiveSourceRepository(repo.database_path)
            self._live_epg_repository = LiveEpgRepository(repo.database_path)
            self._plugin_repository = SpiderPluginRepository(repo.database_path)
            self._playback_history_repository = LocalPlaybackHistoryRepository(repo.database_path)
            self._favorites_repository = FavoritesRepository(repo.database_path)
            self._following_repository = FollowingRepository(repo.database_path)
            self._favorite_tmdb_binding_repository = FavoriteTMDBBindingRepository(repo.database_path)
            cache_dir = app_cache_dir() / "plugins"
            self._plugin_loader = self._build_spider_plugin_loader(cache_dir)
            self._plugin_manager = SpiderPluginManager(
                self._plugin_repository,
                self._plugin_loader,
                self._playback_history_repository,
                danmaku_preference_store=self._danmaku_preference_store,
            )
            self._plugin_manager.backfill_source_metadata()
            setattr(self._plugin_manager, "_playback_parser_service", self._playback_parser_service)
            setattr(self._plugin_manager, "_yt_dlp_service", self._yt_dlp_service)
            setattr(self._plugin_manager, "_danmaku_service", self._danmaku_service)
            setattr(
                self._plugin_manager,
                "_preferred_parse_key_loader",
                lambda: self.repo.load_config().preferred_parse_key,
            )
            setattr(
                self._plugin_manager,
                "_base_url_loader",
                lambda: self.repo.load_config().base_url,
            )
        else:
            self._live_source_repository = _NullLiveSourceRepository()
            self._live_epg_repository = None
            self._plugin_repository = None
            self._playback_history_repository = None
            self._favorites_repository = None
            self._following_repository = None
            self._favorite_tmdb_binding_repository = None
            self._plugin_loader = None
            self._plugin_manager = _NullPluginManager()
        self._metadata_binding_repository = (
            MetadataBindingRepository(repo.database_path)
            if hasattr(repo, "database_path")
            else None
        )
        self._episode_title_override_repository = (
            EpisodeTitleOverrideRepository(repo.database_path)
            if hasattr(repo, "database_path")
            else None
        )

    def _apply_runtime_config(self, config: AppConfig) -> None:
        proxy_server = getattr(self._m3u8_ad_filter, "_proxy_server", None)
        update_segment_prefetch_size = getattr(proxy_server, "set_segment_prefetch_size", None)
        if callable(update_segment_prefetch_size):
            update_segment_prefetch_size(config.m3u_proxy_segment_prefetch_size)
        update_ad_filter_mode = getattr(proxy_server, "set_ad_filter_mode", None)
        if callable(update_ad_filter_mode):
            update_ad_filter_mode(config.m3u8_ad_filter_mode)

    def _save_shared_config(self, config: AppConfig) -> None:
        self._apply_runtime_config(config)
        self.repo.save_config(config)

    def _build_spider_plugin_loader(self, cache_dir: Path):
        try:
            parameters = inspect.signature(SpiderPluginLoader).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs = {}
        if accepts_kwargs or "get" in parameters:
            kwargs["get"] = self._proxy_http_get()
        return SpiderPluginLoader(cache_dir, **kwargs)

    def _close_api_client(self) -> None:
        if self._api_client is None:
            return
        close_client = getattr(self._api_client, "close", None)
        if callable(close_client):
            close_client()
        self._api_client = None

    def _build_proxy_decider(self) -> ProxyDecider:
        config = self.repo.load_config()
        return ProxyDecider(
            ProxyConfig(
                mode=config.network_proxy_mode,
                proxy_url=config.network_proxy_url,
                bypass_rules=list(config.network_proxy_bypass_rules),
                proxy_rules=list(config.network_proxy_rules),
            )
        )

    def _build_smart_search_controller(
        self,
        config: AppConfig,
        *,
        favorites_controller=None,
        following_controller=None,
        history_controller=None,
    ):
        if not config.ai_enabled:
            return None
        provider_config = AIProviderConfig(
            base_url=config.ai_base_url,
            api_key=config.ai_api_key,
            chat_model=config.ai_chat_model,
            timeout_seconds=config.ai_request_timeout_seconds,
        )
        if not provider_config.is_complete:
            return None
        client = OpenAICompatibleClient(provider_config)
        parser = SmartSearchIntentParser(client)
        return SmartSearchController(
            intent_parser=parser,
            favorites_controller=favorites_controller,
            following_controller=following_controller,
            history_controller=history_controller,
        )

    def _build_ai_enrichment_service(self, config: AppConfig, *, workflow: str = ""):
        if not config.ai_enabled:
            return None
        if workflow == "metadata" and not config.ai_metadata_enrichment_enabled:
            return None
        if workflow == "danmaku" and not config.ai_danmaku_enrichment_enabled:
            return None
        if workflow == "episode_titles" and not config.ai_episode_title_rewrite_enabled:
            return None
        if workflow == "following" and not config.ai_following_summary_enabled:
            return None
        provider_config = AIProviderConfig(
            base_url=config.ai_base_url,
            api_key=config.ai_api_key,
            chat_model=config.ai_chat_model,
            timeout_seconds=config.ai_request_timeout_seconds,
        )
        if not provider_config.is_complete:
            return None
        return AIEnrichmentService(OpenAICompatibleClient(provider_config))

    def _build_metadata_ai_enrichment_service(self, config: AppConfig):
        workflows = [
            workflow
            for workflow, enabled in (
                ("metadata", config.ai_metadata_enrichment_enabled),
                ("episode_titles", config.ai_episode_title_rewrite_enabled),
            )
            if enabled
        ]
        if not workflows:
            return None
        if len(workflows) == 1:
            return self._build_ai_enrichment_service(config, workflow=workflows[0])
        return self._build_ai_enrichment_service(config)

    def _refresh_danmaku_ai_enrichment(self, config: AppConfig):
        ai_enrichment_service = self._build_ai_enrichment_service(config, workflow="danmaku")
        if self._danmaku_service is not None:
            setattr(self._danmaku_service, "_ai_enrichment_service", ai_enrichment_service)
        return ai_enrichment_service

    def _proxy_http_get(self):
        def run(url: str, **kwargs):
            request_kwargs = dict(kwargs)
            request_kwargs.update(build_httpx_kwargs_for_url(self._build_proxy_decider(), url))
            return httpx.get(url, **request_kwargs)

        return run

    def _proxy_http_post(self):
        def run(url: str, **kwargs):
            request_kwargs = dict(kwargs)
            request_kwargs.update(build_httpx_kwargs_for_url(self._build_proxy_decider(), url))
            return httpx.post(url, **request_kwargs)

        return run

    def _proxy_http_stream(self):
        def run(method: str, url: str, **kwargs):
            request_kwargs = dict(kwargs)
            request_kwargs.update(build_httpx_kwargs_for_url(self._build_proxy_decider(), url))
            return httpx.stream(method, url, **request_kwargs)

        return run

    def _create_api_client(self, config: AppConfig) -> ApiClient:
        try:
            return ApiClient(
                config.base_url,
                token=config.token,
                vod_token=config.vod_token,
                proxy_decider=self._build_proxy_decider(),
                username=config.username,
            )
        except TypeError as exc:
            if "proxy_decider" not in str(exc):
                raise
            return ApiClient(
                config.base_url,
                token=config.token,
                vod_token=config.vod_token,
            )

    def start(self) -> QWidget:
        config = self.repo.load_config()
        if self._app_log_service is not None:
            configure_logging("INFO", StructuredJsonlHandler(self._app_log_service))
        logger.info(
            "App start view=%s",
            decide_start_view(config),
            extra={"log_category": "app", "log_source": "app"},
        )
        if decide_start_view(config) == "main":
            self._api_client = self._create_api_client(config)
            try:
                self._ensure_vod_token(self._api_client)
            except UnauthorizedError:
                logger.warning("Stored login expired, redirect to login")
                self.repo.clear_token()
                return self._show_login()
            except ApiError as exc:
                logger.warning("App startup failed, redirect to login error=%s", exc)
                return self._show_login(error_message=str(exc))
            return self._show_main()
        return self._show_login()

    def _build_api_client(self) -> ApiClient:
        config = self.repo.load_config()
        api_client = self._create_api_client(config)
        self._ensure_vod_token(api_client)
        return api_client

    def _ensure_vod_token(self, api_client: ApiClient) -> str:
        config = self.repo.load_config()
        options, is_admin = self._load_vod_token_options(api_client)
        self._vod_token_options = options
        self._vod_token_is_admin = is_admin
        if not is_admin and config.username:
            vod_token = self._user_vod_token(config.username)
        elif config.vod_token and (not options or config.vod_token in options):
            vod_token = config.vod_token
        else:
            # Keeps existing installations working until an administrator makes
            # an explicit choice on the next login.
            vod_token = options[0] if options else "-"
        api_client.set_vod_token(vod_token)
        if config.vod_token == vod_token:
            return vod_token
        config.vod_token = vod_token
        self.repo.save_config(config)
        logger.info("Stored vod token role=%s", "admin" if is_admin else "user")
        return vod_token

    @staticmethod
    def _load_vod_token_options(api_client) -> tuple[list[str], bool]:
        get_info = getattr(api_client, "get_vod_token_info", None)
        if callable(get_info):
            info = get_info()
            raw_tokens = info.get("tokens", []) if isinstance(info, Mapping) else []
            tokens = list(
                dict.fromkeys(
                    str(token).strip() for token in raw_tokens if str(token).strip()
                )
            )
            role = (
                str(info.get("role") or "").strip().upper()
                if isinstance(info, Mapping)
                else ""
            )
            return tokens, role == "ADMIN"

        # Compatibility for integrations and older tests that expose only the
        # original API method. Such servers had no role information and used
        # the first token for the administrator account.
        fetch_token = getattr(api_client, "fetch_vod_token")
        token = str(fetch_token() or "-").strip() or "-"
        return [token], True

    @staticmethod
    def _user_vod_token(username: str) -> str:
        return f"u-{username.strip()}"

    def _select_vod_token_after_login(self) -> bool:
        config = self.repo.load_config()
        api_client = self._create_api_client(config)
        try:
            options, is_admin = self._load_vod_token_options(api_client)
        finally:
            close = getattr(api_client, "close", None)
            if callable(close):
                close()
        self._vod_token_options = options
        self._vod_token_is_admin = is_admin
        if not is_admin:
            config.vod_token = self._user_vod_token(config.username)
            self.repo.save_config(config)
            return True
        if not options:
            raise ApiError("服务器未返回可用的 VOD token")
        current_index = (
            options.index(config.vod_token) if config.vod_token in options else 0
        )
        token, accepted = QInputDialog.getItem(
            self.login_window,
            "选择 VOD Token",
            "请选择要使用的 VOD Token：",
            options,
            current_index,
            False,
        )
        if not accepted:
            return False
        config.vod_token = str(token)
        self.repo.save_config(config)
        return True

    def _apply_vod_token_change(self, vod_token: str) -> None:
        if self._api_client is not None:
            self._api_client.set_vod_token(vod_token)

    @staticmethod
    def _metadata_has_value(value: object) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, dict, set)):
            return bool(value)
        return value is not None

    def _build_plugin_metadata_payload(self, raw_detail: Mapping[str, object] | None) -> dict[str, object] | None:
        if not isinstance(raw_detail, Mapping):
            return None
        payload: dict[str, object] = {}
        for key in ("metadata", "meta"):
            candidate = raw_detail.get(key)
            if isinstance(candidate, Mapping):
                payload.update({str(item_key): item_value for item_key, item_value in candidate.items()})
                break
        derived = {
            "id": raw_detail.get("vod_id") or raw_detail.get("id"),
            "title": raw_detail.get("vod_name") or raw_detail.get("title") or raw_detail.get("name"),
            "overview": raw_detail.get("vod_content") or raw_detail.get("description") or raw_detail.get("intro"),
            "rating": raw_detail.get("vod_remarks") or raw_detail.get("rating"),
            "year": raw_detail.get("vod_year") or raw_detail.get("year"),
            "poster": raw_detail.get("vod_pic") or raw_detail.get("poster") or raw_detail.get("cover"),
            "actors": raw_detail.get("vod_actor") or raw_detail.get("actors"),
            "country": raw_detail.get("vod_area") or raw_detail.get("country"),
            "language": raw_detail.get("vod_lang") or raw_detail.get("language"),
            "directors": raw_detail.get("vod_director") or raw_detail.get("directors") or raw_detail.get("director"),
            "genre": raw_detail.get("type_name") or raw_detail.get("genre"),
            "detail_fields": raw_detail.get("detail_fields") or raw_detail.get("ext"),
            "imdb_id": raw_detail.get("imdb_id"),
            "tmdb_id": raw_detail.get("tmdb_id"),
        }
        for key, value in derived.items():
            if not self._metadata_has_value(payload.get(key)) and self._metadata_has_value(value):
                payload[key] = value
        if "detail_fields" in payload and not isinstance(payload["detail_fields"], list):
            payload.pop("detail_fields", None)
        if not any(self._metadata_has_value(value) for value in payload.values()):
            return None
        return payload

    def _build_metadata_providers(
        self,
        *,
        api_client: ApiClient,
        config: AppConfig,
        source_kind: str,
        raw_detail=None,
    ) -> list[object]:
        providers: list[object] = []
        disabled_provider_ids = {
            str(item or "").strip()
            for item in config.disabled_metadata_provider_ids
        }

        def enabled(provider_id: str) -> bool:
            return provider_id not in disabled_provider_ids

        proxy_decider = self._build_proxy_decider()
        if source_kind == "plugin" and enabled("plugin"):
            plugin_payload = self._build_plugin_metadata_payload(raw_detail)
            if plugin_payload is not None:
                providers.append(CustomPluginProvider(plugin_payload))
        if enabled("bangumi"):
            providers.append(
                BangumiMetadataProvider(
                    BangumiClient(
                        access_token=config.metadata_bangumi_access_token,
                        proxy_decider=proxy_decider,
                    )
                )
            )
        if enabled("bilibili"):
            providers.append(BilibiliMetadataProvider())
        if enabled("iqiyi"):
            providers.append(IqiyiMetadataProvider())
        if enabled("tencent"):
            providers.append(TencentMetadataProvider())
        if enabled("youku"):
            providers.append(YoukuMetadataProvider())
        if enabled("official_douban") and str(config.metadata_douban_cookie or "").strip():
            local_douban_client = LocalDoubanClient(
                cookie=config.metadata_douban_cookie,
                proxy_decider=proxy_decider,
            )
            providers.append(OfficialDoubanProvider(local_douban_client))
        if enabled("tmdb") and _has_tmdb_access(config):
            providers.append(
                TMDBProvider(
                    _build_tmdb_client(
                        api_key=config.metadata_tmdb_api_key,
                        proxy_base_url=config.metadata_tmdb_proxy_base_url,
                        proxy_decider=proxy_decider,
                    )
                )
            )
        if enabled("sohu"):
            providers.append(SohuMetadataProvider())
        if enabled("local_douban") and enabled("remote_douban"):
            providers.append(LocalDoubanProvider(api_client))
        return providers

    def _build_metadata_hydrator_factory(self, api_client: ApiClient):
        cache = MetadataCache(app_cache_dir() / "metadata")
        supported_sources = {"browse", "telegram", "telegram_channel", "plugin", "emby", "jellyfin", "feiniu", "bilibili"}

        def factory(*, request=None, source_kind: str = "", source_key: str = "", vod=None, raw_detail=None):
            del request
            if vod is None or source_kind not in supported_sources:
                return None
            if is_short_drama_collection(vod.vod_name, vod.category_name, vod.type_name):
                return None
            if source_kind == "bilibili" and not _supports_bilibili_metadata_enhancement(vod):
                return None
            config = self.repo.load_config()
            if not config.metadata_enhancement_enabled:
                return None
            providers = self._build_metadata_providers(
                api_client=api_client,
                config=config,
                source_kind=source_kind,
                raw_detail=raw_detail,
            )
            hydrator = MetadataHydrator(
                cache=cache,
                providers=providers,
                binding_repository=self._metadata_binding_repository,
            )

            def hydrate(session) -> object:
                session_vod = getattr(session, "vod", None) or vod
                playlist = list(getattr(session, "playlist", []) or [])
                start_index = int(getattr(session, "start_index", 0) or 0)
                current_item = playlist[start_index] if 0 <= start_index < len(playlist) else None
                return hydrator.hydrate(
                    MetadataContext(
                        vod=session_vod,
                        source_kind=source_kind,
                        source_key=source_key,
                        current_item=current_item,
                        raw_detail=raw_detail,
                    )
                )

            return hydrate

        return factory

    def _build_metadata_scrape_service_factory(self, api_client: ApiClient):
        cache = MetadataCache(app_cache_dir() / "metadata")
        supported_sources = {"browse", "telegram", "telegram_channel", "plugin", "emby", "jellyfin", "feiniu", "bilibili"}

        def factory(*, request=None, source_kind: str = "", source_key: str = "", vod=None, raw_detail=None):
            del request, source_key
            if source_kind not in supported_sources:
                return None
            if vod is not None and is_short_drama_collection(vod.vod_name, vod.category_name, vod.type_name):
                return None
            if source_kind == "bilibili" and not _supports_bilibili_metadata_enhancement(vod):
                return None
            config = self.repo.load_config()
            if not config.metadata_enhancement_enabled:
                return None
            providers = self._build_metadata_providers(
                api_client=api_client,
                config=config,
                source_kind=source_kind,
                raw_detail=raw_detail,
            )
            ai_enrichment_service = self._build_metadata_ai_enrichment_service(config)
            return MetadataScrapeService(
                cache=cache,
                providers=providers,
                ai_enrichment_service=ai_enrichment_service,
                ai_query_refinement_enabled=config.ai_metadata_enrichment_enabled,
                ai_episode_title_rewrite_enabled=config.ai_episode_title_rewrite_enabled,
            )

        return factory

    def _build_following_metadata_search_service(self, api_client: ApiClient) -> MetadataScrapeService:
        config = self.repo.load_config()
        providers = self._build_metadata_providers(
            api_client=api_client,
            config=config,
            source_kind="browse",
            raw_detail=None,
        )
        ai_enrichment_service = self._build_metadata_ai_enrichment_service(config)
        return MetadataScrapeService(
            cache=MetadataCache(app_cache_dir() / "metadata"),
            providers=providers,
            ai_enrichment_service=ai_enrichment_service,
            ai_query_refinement_enabled=config.ai_metadata_enrichment_enabled,
            ai_episode_title_rewrite_enabled=config.ai_episode_title_rewrite_enabled,
        )

    def _build_following_tmdb_discovery_service(self) -> TMDBDiscoveryService | None:
        config = self.repo.load_config()
        disabled_provider_ids = {str(item or "").strip() for item in config.disabled_metadata_provider_ids}
        if "tmdb" in disabled_provider_ids:
            return None
        api_key = str(config.metadata_tmdb_api_key or "").strip()
        if not _has_tmdb_access(config):
            return None
        proxy_decider = self._build_proxy_decider()
        douban_client = None
        if "official_douban" not in disabled_provider_ids and str(config.metadata_douban_cookie or "").strip():
            douban_client = LocalDoubanClient(
                cookie=config.metadata_douban_cookie,
                proxy_decider=proxy_decider,
            )
        return TMDBDiscoveryService(
            client=_build_tmdb_client(
                api_key=api_key,
                proxy_base_url=config.metadata_tmdb_proxy_base_url,
                proxy_decider=proxy_decider,
            ),
            cache=MetadataCache(app_cache_dir() / "metadata"),
            douban_client=douban_client,
        )

    def _build_danmaku_controller_factory(self):
        def factory(*, request=None, source_kind: str = "", source_key: str = "", vod=None, raw_detail=None):
            del request, source_key, raw_detail
            if source_kind not in {"browse", "telegram", "telegram_channel", "emby", "jellyfin", "feiniu", "msub"}:
                return None
            if vod is not None and is_short_drama_collection(vod.vod_name, vod.category_name, vod.type_name):
                return None
            if self._danmaku_service is None:
                return None
            return GenericDanmakuController(
                self._danmaku_service,
                danmaku_preference_store=self._danmaku_preference_store,
            )

        return factory

    def _build_episode_title_enhancer_factory(self, api_client: ApiClient):
        del api_client
        cache = MetadataCache(app_cache_dir() / "metadata")
        metadata_title_providers: list[object] = []

        def _normalize_title(value: object) -> str:
            return re.sub(r"\s+", "", str(value or "").strip().lower())

        def _strip_search_season_suffix(value: object) -> str:
            text = str(value or "").strip()
            if not text:
                return ""
            stripped = re.sub(
                r"(?:\s*[-:：]\s*)?(?:第\s*[0-9零一二两三四五六七八九十百]+\s*季|season\s*\d+|s\d+)\s*$",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip()
            return stripped or text

        def _title_has_season_marker(value: object) -> bool:
            return re.search(
                r"(?:第\s*[0-9零一二两三四五六七八九十百]+\s*季|season\s*\d+|\bS\d+\b)",
                str(value or "").strip(),
                re.IGNORECASE,
            ) is not None

        def _guess_season_number(vod: VodItem) -> int:
            for value in (vod.vod_name, vod.vod_remarks, vod.category_name):
                season_number = extract_season_number(value)
                if season_number is not None:
                    return season_number
            return 1

        def _search_tv_cached(client: TMDBClient, title: str, year: str = "") -> list[dict[str, object]]:
            cache_key = f"{title}\x1f{year}"
            payload = cache.load_payload(
                "tmdb_episode_search",
                cache_key,
                ttl_seconds=_METADATA_SEARCH_CACHE_TTL_SECONDS,
                empty_ttl_seconds=_METADATA_EMPTY_SEARCH_CACHE_TTL_SECONDS,
            )
            if isinstance(payload, list):
                results = [dict(item) for item in payload if isinstance(item, Mapping)]
                logger.info(
                    "Episode title enhancer TMDB search cache hit title=%s year=%s results=%s",
                    title,
                    year,
                    len(results),
                )
                return results
            logger.info(
                "Episode title enhancer TMDB search cache miss title=%s year=%s",
                title,
                year,
            )
            results = list(client.search_tv(title, year=year))
            cache.save_payload("tmdb_episode_search", cache_key, results)
            return results

        def _get_tv_season_detail_cached(
            client: TMDBClient,
            tmdb_id: str,
            season_number: int,
        ) -> dict[str, object]:
            cache_key = f"{tmdb_id}:{season_number}"
            payload = cache.load_payload(
                "tmdb_episode_season_detail",
                cache_key,
                ttl_seconds=_METADATA_DETAIL_CACHE_TTL_SECONDS,
            )
            if isinstance(payload, Mapping):
                return dict(payload)
            detail = dict(client.get_tv_season_detail(tmdb_id, season_number))
            cache.save_payload("tmdb_episode_season_detail", cache_key, detail)
            return detail

        def _select_tmdb_search_match(
            search_results: list[dict[str, object]],
            preferred_title: str,
            vod: VodItem,
            requested_seasons: set[int],
            tmdb_client: TMDBClient,
        ) -> dict[str, object] | None:
            if not search_results:
                return None
            normalized_title = _normalize_title(preferred_title)
            preferred_base = _normalize_title(_strip_search_season_suffix(preferred_title))
            preferred_season = extract_season_number(preferred_title)
            category_name = " ".join(
                value
                for value in (
                    str(getattr(vod, "category_name", "") or "").strip(),
                    infer_metadata_category_name_from_title(getattr(vod, "vod_name", "")),
                    str(getattr(vod, "vod_name", "") or "").strip(),
                )
                if value
            ).lower()
            prefer_animation = any(token in category_name for token in ("动漫", "动画", "anime", "国创"))
            prefer_live_action = any(token in category_name for token in ("电视剧", "剧集", "连续剧", "真人"))
            prefer_short_drama = any(token in category_name for token in ("短剧", "短片"))
            expected_year = str(getattr(vod, "vod_year", "") or "").strip()

            def _extract_candidate_year(candidate: dict[str, object]) -> int | None:
                raw = str(candidate.get("first_air_date") or candidate.get("release_date") or candidate.get("year") or "").strip()
                return int(raw[:4]) if len(raw) >= 4 and raw[:4].isdigit() else None

            def _year_closeness_score(candidate: dict[str, object]) -> int:
                if not expected_year.isdigit():
                    return 0
                candidate_year = _extract_candidate_year(candidate)
                if candidate_year is None:
                    return 0
                return -abs(candidate_year - int(expected_year))

            def _season_coverage_score(candidate: dict[str, object]) -> int:
                tmdb_id = str(candidate.get("id") or "").strip()
                if not tmdb_id or not requested_seasons:
                    return 0
                covered = 0
                for season_number in requested_seasons:
                    try:
                        detail = _get_tv_season_detail_cached(tmdb_client, tmdb_id, season_number)
                    except Exception:
                        continue
                    episodes = detail.get("episodes")
                    if isinstance(episodes, list) and episodes:
                        covered += 1
                return covered

            def _score(candidate: dict[str, object]) -> tuple[int, int, int, int, int, int, int]:
                candidate_title = str(candidate.get("name") or candidate.get("title") or "").strip()
                candidate_normalized = _normalize_title(candidate_title)
                candidate_base = _normalize_title(_strip_search_season_suffix(candidate_title))
                candidate_season = extract_season_number(candidate_title)
                try:
                    genre_ids = {int(value) for value in candidate.get("genre_ids") or []}
                except (TypeError, ValueError):
                    genre_ids = set()
                exact_match = 1 if candidate_normalized == normalized_title else 0
                season_base_match = 1 if preferred_base and candidate_base == preferred_base and candidate_season == preferred_season else 0
                live_action_match = 1 if prefer_live_action and 16 not in genre_ids else 0
                short_drama_match = 1 if prefer_short_drama and 10766 in genre_ids else 0
                animation_match = 1 if prefer_animation and 16 in genre_ids else 0
                base_match = 1 if preferred_base and candidate_base == preferred_base else 0
                season_coverage = _season_coverage_score(candidate)
                year_closeness = _year_closeness_score(candidate)
                return (
                    exact_match,
                    season_base_match,
                    base_match,
                    animation_match,
                    live_action_match + short_drama_match,
                    season_coverage,
                    year_closeness,
                )

            return max(search_results, key=_score)

        def _load_tmdb_titles_by_season_episode(
            tmdb_client: TMDBClient,
            session_vod: VodItem,
            *,
            tmdb_id: str,
            season_map: Mapping[int, int],
            include_season_prefix: bool,
        ) -> dict[tuple[int, int], str]:
            titles_by_season_episode: dict[tuple[int, int], str] = {}
            for target_season_number, actual_season_number in sorted(season_map.items()):
                try:
                    season_detail = _get_tv_season_detail_cached(tmdb_client, tmdb_id, actual_season_number)
                except Exception:
                    logger.debug(
                        "Skip TMDB season episode title enhancement fallback to provider metadata tmdb_id=%s season=%s title=%s",
                        tmdb_id,
                        actual_season_number,
                        str(getattr(session_vod, "vod_name", "") or "").strip(),
                        exc_info=True,
                    )
                    continue
                episodes = season_detail.get("episodes")
                if not isinstance(episodes, list):
                    continue
                for episode in episodes:
                    if not isinstance(episode, dict):
                        continue
                    try:
                        episode_number = int(episode.get("episode_number") or 0)
                    except (TypeError, ValueError):
                        continue
                    episode_title = str(episode.get("name") or "").strip()
                    if episode_number <= 0 or not episode_title:
                        continue
                    prefix = f"第{target_season_number}季 " if include_season_prefix else ""
                    titles_by_season_episode[(target_season_number, episode_number)] = (
                        f"{prefix}第{episode_number}集 {episode_title}"
                    )
            return titles_by_season_episode

        def _season_episode_pairs(playlist: list[PlayItem], default_season: int) -> list[tuple[int, int] | None]:
            pairs: list[tuple[int, int] | None] = []
            for item in playlist:
                season_number = None
                for value in (item.original_title, item.title, item.path):
                    season_number = extract_season_number(value)
                    if season_number is not None:
                        break
                episode_number = infer_playlist_episode_number(item, playlist)
                if episode_number is None:
                    episode_number = _extract_quality_variant_episode_number(item)
                if episode_number is None:
                    pairs.append(None)
                    continue
                pairs.append((season_number or default_season, episode_number))
            return pairs

        def _finalize_episode_playlist(
            playlist: list[PlayItem],
            season_episode_pairs: list[tuple[int, int] | None],
            *,
            original_playlist: list[PlayItem] | None = None,
            preserve_order: bool = False,
        ) -> list[PlayItem] | None:
            if not playlist_has_title_variants(playlist):
                return None
            if preserve_order and original_playlist is not None:
                playlist = _restore_original_playlist_order(original_playlist, playlist)
            elif len(playlist) > 1:
                resolved_pairs = [pair for pair in season_episode_pairs if pair is not None]
                has_multi_version_pairs = len(resolved_pairs) != len(set(resolved_pairs))
                indexed_playlist = list(enumerate(playlist))
                if has_multi_version_pairs:
                    version_slot_by_index = episode_version_slots_by_index(
                        playlist,
                        season_episode_pairs,
                        sentinel=_EPISODE_SORT_SENTINEL,
                    )
                    indexed_playlist.sort(
                        key=lambda entry: (
                            version_slot_by_index[entry[0]],
                            season_episode_pairs[entry[0]] or (_EPISODE_SORT_SENTINEL, _EPISODE_SORT_SENTINEL),
                            entry[0],
                        )
                    )
                else:
                    indexed_playlist.sort(
                        key=lambda entry: (
                            season_episode_pairs[entry[0]] or (_EPISODE_SORT_SENTINEL, _EPISODE_SORT_SENTINEL),
                            entry[0],
                        )
                    )
                playlist = [item for _original_index, item in indexed_playlist]
            for index, item in enumerate(playlist):
                item.index = index
            return playlist

        def _episode_title_cache_item_identity(item: PlayItem) -> tuple[str, str, str, str]:
            return (
                str(item.original_title or item.title or "").strip(),
                str(item.path or "").strip(),
                str(item.title or "").strip(),
                str(item.play_source or "").strip(),
            )

        def _is_variety_playlist(session_vod: VodItem, playlist: list[PlayItem]) -> bool:
            if is_variety_collection(
                session_vod.type_name,
                session_vod.category_name,
                session_vod.vod_tag,
                session_vod.vod_content,
            ):
                return True
            variety_items = sum(
                is_likely_variety_title(item.original_title or item.title or item.path)
                for item in playlist
            )
            return variety_items >= 2 and variety_items * 2 >= len(playlist)

        def _restore_original_playlist_order(
            original_playlist: list[PlayItem],
            updated_playlist: list[PlayItem],
        ) -> list[PlayItem]:
            items_by_identity: dict[tuple[str, str, str, str], list[PlayItem]] = {}
            for item in updated_playlist:
                items_by_identity.setdefault(_episode_title_cache_item_identity(item), []).append(item)
            restored: list[PlayItem] = []
            for original_item in original_playlist:
                identity = _episode_title_cache_item_identity(original_item)
                matching_items = items_by_identity.get(identity)
                if matching_items:
                    restored.append(matching_items.pop(0))
            restored_ids = {id(item) for item in restored}
            restored.extend(item for item in updated_playlist if id(item) not in restored_ids)
            return restored

        def _episode_title_cache_key(
            provider_source_kind: str,
            session_vod: VodItem,
            playlist: list[PlayItem],
        ) -> str:
            seeded = seed_original_titles([replace(item) for item in playlist])
            return repr(
                (
                    _EPISODE_TITLE_PLAYLIST_CACHE_VERSION,
                    provider_source_kind,
                    str(getattr(session_vod, "vod_name", "") or "").strip(),
                    str(getattr(session_vod, "vod_year", "") or "").strip(),
                    str(getattr(session_vod, "category_name", "") or "").strip(),
                    tuple(_episode_title_cache_item_identity(item) for item in seeded),
                )
            )

        def _restore_cached_episode_title_playlist(
            provider_source_kind: str,
            session_vod: VodItem,
            playlist: list[PlayItem],
        ) -> list[PlayItem] | None:
            cache_key = _episode_title_cache_key(provider_source_kind, session_vod, playlist)
            payload = cache.load_payload(
                "episode_title_playlist",
                cache_key,
                ttl_seconds=_METADATA_DETAIL_CACHE_TTL_SECONDS,
            )
            if not isinstance(payload, Mapping):
                logger.info(
                    "Episode title enhancer final cache miss title=%s year=%s category=%s items=%s",
                    str(getattr(session_vod, "vod_name", "") or "").strip(),
                    str(getattr(session_vod, "vod_year", "") or "").strip(),
                    str(getattr(session_vod, "category_name", "") or "").strip(),
                    len(playlist),
                )
                return None
            titles_payload = payload.get("titles")
            order_payload = payload.get("order")
            if not isinstance(titles_payload, list) or not isinstance(order_payload, list):
                return None
            seeded = seed_original_titles([replace(item) for item in playlist])
            if len(titles_payload) != len(seeded) or len(order_payload) != len(seeded):
                return None
            try:
                order = [int(value) for value in order_payload]
            except (TypeError, ValueError):
                return None
            if sorted(order) != list(range(len(seeded))):
                return None
            for index, item_payload in enumerate(titles_payload):
                if not isinstance(item_payload, Mapping):
                    return None
                seeded[index].episode_display_title = str(item_payload.get("display") or "").strip()
                seeded[index].episode_title_source = str(item_payload.get("source") or "").strip()
            restored = [seeded[index] for index in order]
            for index, item in enumerate(restored):
                item.index = index
            logger.info(
                "Episode title enhancer final cache hit title=%s year=%s category=%s items=%s",
                str(getattr(session_vod, "vod_name", "") or "").strip(),
                str(getattr(session_vod, "vod_year", "") or "").strip(),
                str(getattr(session_vod, "category_name", "") or "").strip(),
                len(restored),
            )
            return restored if playlist_has_title_variants(restored) else None

        def _save_episode_title_playlist_cache(
            provider_source_kind: str,
            session_vod: VodItem,
            original_playlist: list[PlayItem],
            updated_playlist: list[PlayItem],
        ) -> None:
            seeded_original = seed_original_titles([replace(item) for item in original_playlist])
            seeded_updated = seed_original_titles([replace(item) for item in updated_playlist])
            indexes_by_identity: dict[tuple[str, str, str, str, str], list[int]] = {}
            for index, item in enumerate(seeded_original):
                indexes_by_identity.setdefault(_episode_title_cache_item_identity(item), []).append(index)
            titles_payload = [{"display": "", "source": ""} for _ in seeded_original]
            order: list[int] = []
            for item in seeded_updated:
                identity = _episode_title_cache_item_identity(item)
                source_indexes = indexes_by_identity.get(identity)
                if not source_indexes:
                    return
                source_index = source_indexes.pop(0)
                order.append(source_index)
                titles_payload[source_index] = {
                    "display": str(item.episode_display_title or "").strip(),
                    "source": str(item.episode_title_source or "").strip(),
                }
            if len(order) != len(seeded_original):
                return
            cache.save_payload(
                "episode_title_playlist",
                _episode_title_cache_key(provider_source_kind, session_vod, original_playlist),
                {"order": order, "titles": titles_payload},
            )

        def _search_metadata_candidates(session_vod: VodItem, provider_source_kind: str) -> list[object]:
            query = MetadataContext(vod=session_vod, source_kind=provider_source_kind).to_query()
            candidates: list[object] = []
            for provider in metadata_title_providers:
                try:
                    matches = provider.search(query)
                except Exception:
                    continue
                if matches:
                    candidate = matches[0]
                    if getattr(provider, "name", "") == "bangumi":
                        subject_id = ""
                        provider_id = str(getattr(candidate, "provider_id", "") or "").strip()
                        if provider_id.startswith("subject:"):
                            subject_id = provider_id.split(":", 1)[1].strip()
                        client = getattr(provider, "_client", None)
                        if subject_id and client is not None and hasattr(client, "get_episodes"):
                            try:
                                episodes = client.get_episodes(subject_id) or []
                            except Exception:
                                episodes = []
                            if isinstance(episodes, list) and episodes:
                                candidate = replace(
                                    candidate,
                                    raw={**dict(getattr(candidate, "raw", {}) or {}), "episodes": episodes},
                                )
                    hydrate = getattr(provider, "_hydrate_episode_candidate", None)
                    if callable(hydrate):
                        try:
                            candidate = hydrate(candidate)
                        except Exception:
                            pass
                    query_kind = _query_media_kind(query)
                    match_kind = _match_media_kind(candidate)
                    if query_kind and match_kind and query_kind != match_kind:
                        continue
                    if not is_confident_match(score_match(query, candidate)):
                        continue
                    query_year = str(getattr(session_vod, "vod_year", "") or "").strip()
                    candidate_year = str(getattr(candidate, "year", "") or "").strip()
                    if query_year and candidate_year and query_year != candidate_year:
                        continue
                    requested_season = _guess_season_number(session_vod)
                    candidate_season = extract_season_number(getattr(candidate, "title", ""))
                    if requested_season > 1 and candidate_season != requested_season:
                        continue
                    candidates.append(candidate)
            return candidates

        def _count_mapped_episode_titles(playlist: list[PlayItem]) -> int:
            return sum(
                1
                for item in playlist
                if str(item.episode_display_title or "").strip()
                and str(item.original_title or "").strip()
                and normalize_episode_title_text(str(item.original_title or ""))
                != normalize_episode_title_text(str(item.episode_display_title or ""))
            )

        def _episode_title_snapshot(playlist: list[PlayItem]) -> list[tuple[str, str]]:
            return [
                (
                    str(item.episode_display_title or "").strip(),
                    str(item.episode_title_source or "").strip(),
                )
                for item in playlist
            ]

        def _episode_title_source_rank(playlist: list[PlayItem], source_priority: list[str]) -> int:
            ranks = [
                source_priority.index(source)
                for _, source in _episode_title_snapshot(playlist)
                if source in source_priority
            ]
            return min(ranks) if ranks else len(source_priority) + 100

        def factory(*, request=None, source_kind: str = "", source_key: str = "", vod=None, raw_detail=None):
            del request, source_key, raw_detail
            if source_kind not in {"plugin", "browse", "telegram", "telegram_channel", "emby", "jellyfin", "feiniu"} or vod is None:
                return None
            if is_short_drama_collection(vod.vod_name, vod.category_name, vod.type_name):
                return None
            config = self.repo.load_config()
            if not config.metadata_enhancement_enabled:
                return None
            if not config.episode_title_enhancement_enabled:
                return None
            disabled_metadata_provider_ids = {
                str(item or "").strip()
                for item in config.disabled_metadata_provider_ids
            }
            if "tmdb" in disabled_metadata_provider_ids:
                return None
            if not _has_tmdb_access(config):
                return None
            query = MetadataContext(vod=vod, source_kind=source_kind).to_query()
            if infer_tmdb_media_type(query) == "movie":
                return None
            proxy_decider = self._build_proxy_decider()
            tmdb_client = _build_tmdb_client(
                api_key=config.metadata_tmdb_api_key,
                proxy_base_url=config.metadata_tmdb_proxy_base_url,
                proxy_decider=proxy_decider,
            )
            bangumi_provider = BangumiMetadataProvider(
                BangumiClient(
                    access_token=config.metadata_bangumi_access_token,
                    proxy_decider=proxy_decider,
                )
            )
            bilibili_provider = BilibiliMetadataProvider()
            tencent_provider = TencentMetadataProvider()
            iqiyi_provider = IqiyiMetadataProvider()
            metadata_title_providers[:] = [
                provider
                for provider in (
                    bangumi_provider,
                    bilibili_provider,
                    tencent_provider,
                    iqiyi_provider,
                )
                if provider.name not in disabled_metadata_provider_ids
            ]
            if not metadata_title_providers:
                return None

            def _bound_episode_candidate(session_vod: VodItem, current_item: PlayItem | None = None):
                bindings = self._metadata_binding_repository
                if bindings is None:
                    logger.info(
                        "Episode title enhancer binding lookup skipped title=%s year=%s reason=no_binding_repository",
                        str(session_vod.vod_name or "").strip(),
                        str(session_vod.vod_year or "").strip(),
                    )
                    return None
                binding_queries = []
                if current_item is not None:
                    binding_queries.append(
                        MetadataContext(
                            vod=session_vod,
                            source_kind=source_kind,
                            current_item=current_item,
                        ).to_query()
                    )
                binding_queries.append(MetadataContext(vod=session_vod, source_kind=source_kind).to_query())
                binding = None
                binding_query = None
                for candidate_query in binding_queries:
                    logger.info(
                        "Episode title enhancer binding lookup try query_title=%s query_year=%s current_media_title=%s vod_title=%s",
                        candidate_query.title,
                        candidate_query.year,
                        str(getattr(current_item, "media_title", "") or "").strip() if current_item is not None else "",
                        str(session_vod.vod_name or "").strip(),
                    )
                    binding = bindings.load(candidate_query.title, candidate_query.year)
                    if binding is not None:
                        binding_query = candidate_query
                        break
                if binding is None or binding_query is None:
                    logger.info(
                        "Episode title enhancer binding lookup miss current_media_title=%s vod_title=%s vod_year=%s",
                        str(getattr(current_item, "media_title", "") or "").strip() if current_item is not None else "",
                        str(session_vod.vod_name or "").strip(),
                        str(session_vod.vod_year or "").strip(),
                    )
                    return None
                provider_name = str(binding.provider or "").strip()
                provider_id = str(binding.provider_id or "").strip()
                if not provider_name or not provider_id:
                    logger.info(
                        "Episode title enhancer binding lookup invalid query_title=%s query_year=%s provider=%s provider_id=%s",
                        binding_query.title,
                        binding_query.year,
                        provider_name,
                        provider_id,
                    )
                    return None
                logger.info(
                    "Episode title enhancer binding lookup hit query_title=%s query_year=%s provider=%s provider_id=%s matched_title=%s matched_year=%s",
                    binding_query.title,
                    binding_query.year,
                    provider_name,
                    provider_id,
                    str(binding.matched_title or "").strip(),
                    str(binding.matched_year or "").strip(),
                )
                if is_query_redirect_binding(binding):
                    # 查询重定向行不是元数据绑定，交给 _session_query_redirect 处理
                    return None
                candidate = MetadataMatch(
                    provider=provider_name,
                    provider_id=provider_id,
                    title=str(binding.matched_title or binding_query.title or "").strip(),
                    year=str(binding.matched_year or binding_query.year or "").strip(),
                    raw={"provider_id": provider_id} if provider_name == "bilibili" else {},
                )
                if provider_name == "tmdb":
                    segments = provider_id.split(":")
                    if len(segments) < 2 or segments[0] != "tv":
                        return candidate
                    tmdb_id = str(segments[1] or "").strip()
                    season_number = None
                    if len(segments) >= 4 and segments[2] == "season":
                        try:
                            season_number = int(segments[3])
                        except (TypeError, ValueError):
                            season_number = None
                    if season_number is None:
                        season_number = extract_season_number(candidate.title) or _guess_season_number(session_vod)
                    if not tmdb_id or season_number is None:
                        return candidate
                    try:
                        season_detail = _get_tv_season_detail_cached(tmdb_client, tmdb_id, season_number)
                    except Exception:
                        return candidate
                    episodes = season_detail.get("episodes")
                    if isinstance(episodes, list) and episodes:
                        candidate.raw["season_number"] = season_number
                        candidate.raw["episodes"] = episodes
                    return candidate
                if provider_name == "bangumi":
                    subject_id = provider_id.split(":", 1)[1].strip() if provider_id.startswith("subject:") else ""
                    client = getattr(bangumi_provider, "_client", None)
                    if subject_id and client is not None and hasattr(client, "get_episodes"):
                        episodes = client.get_episodes(subject_id) or []
                        if isinstance(episodes, list) and episodes:
                            candidate.raw["episodes"] = episodes
                    return candidate
                if provider_name == "bilibili":
                    hydrate = getattr(bilibili_provider, "_hydrate_episode_candidate", None)
                    if callable(hydrate):
                        try:
                            return hydrate(candidate)
                        except Exception:
                            return candidate
                return candidate

            def _session_query_redirect(session_vod, current_item=None):
                """Resolves a saved 查询重定向 (manual query correction) for this title.

                与 _bound_episode_candidate 用同一组 binding 查询 key；重定向行只提供
                修正后的搜索标题/年份（如网盘混淆目录名 → 真实剧名），不提供 provider。
                """
                bindings = self._metadata_binding_repository
                if bindings is None:
                    return None
                binding_queries = []
                if current_item is not None:
                    binding_queries.append(
                        MetadataContext(
                            vod=session_vod,
                            source_kind=source_kind,
                            current_item=current_item,
                        ).to_query()
                    )
                binding_queries.append(MetadataContext(vod=session_vod, source_kind=source_kind).to_query())
                for candidate_query in binding_queries:
                    try:
                        binding = bindings.load(candidate_query.title, candidate_query.year)
                    except Exception:
                        continue
                    if not is_query_redirect_binding(binding):
                        continue
                    redirect_title = str(binding.provider_id or "").strip()
                    if redirect_title and redirect_title != candidate_query.title:
                        return redirect_title, str(binding.matched_year or "").strip()
                return None

            def enhance(session) -> list | None:
                session_vod = getattr(session, "vod", None) or vod
                current_playlist = list(getattr(session, "playlist", []) or [])
                if not current_playlist:
                    return None
                preserve_playlist_order = _is_variety_playlist(session_vod, current_playlist)
                playlist = seed_original_titles([replace(item) for item in current_playlist])
                force_refresh = bool(getattr(session, "episode_titles_force_refresh", False))
                if force_refresh:
                    # 手动"重写剧集标题"：一次性消费，忽略上次保存的标题缓存
                    session.episode_titles_force_refresh = False
                cached_playlist = None
                if not force_refresh:
                    cached_playlist = _restore_cached_episode_title_playlist(source_kind, session_vod, current_playlist)
                if cached_playlist is not None:
                    logger.info(
                        "Episode title enhancer restored cached titles mapped_count=%s",
                        _count_mapped_episode_titles(cached_playlist),
                    )
                    return cached_playlist
                default_season = _guess_season_number(session_vod)
                season_episode_pairs = _season_episode_pairs(playlist, default_season)
                current_item = None
                start_index = int(getattr(session, "start_index", 0) or 0)
                if 0 <= start_index < len(current_playlist):
                    current_item = current_playlist[start_index]
                bound_candidate = _bound_episode_candidate(session_vod, current_item)
                if bound_candidate is not None:
                    logger.info(
                        "Episode title enhancer trying bound candidate provider=%s provider_id=%s title=%s year=%s",
                        str(bound_candidate.provider or "").strip(),
                        str(bound_candidate.provider_id or "").strip(),
                        str(bound_candidate.title or "").strip(),
                        str(bound_candidate.year or "").strip(),
                    )
                    updated_playlist = build_provider_episode_playlist(
                        session_vod,
                        playlist,
                        bound_candidate,
                        source_priority=METADATA_EPISODE_TITLE_SOURCE_PRIORITY,
                    )
                    if updated_playlist is not None:
                        updated_pairs = _season_episode_pairs(updated_playlist, default_season)
                        finalized = _finalize_episode_playlist(
                            updated_playlist,
                            updated_pairs,
                            original_playlist=current_playlist,
                            preserve_order=preserve_playlist_order,
                        )
                        if finalized is not None:
                            logger.info(
                                "Episode title enhancer applied bound candidate provider=%s mapped_count=%s",
                                str(bound_candidate.provider or "").strip(),
                                _count_mapped_episode_titles(finalized),
                            )
                            _save_episode_title_playlist_cache(source_kind, session_vod, current_playlist, finalized)
                            return finalized
                    logger.info(
                        "Episode title enhancer skipped bound candidate provider=%s reason=no_rewrite_result",
                        str(bound_candidate.provider or "").strip(),
                    )
                year = str(getattr(session_vod, "vod_year", "") or "").strip()
                search_title = _strip_search_season_suffix(session_vod.vod_name)
                playlist_search_title = _infer_series_title_from_playlist(playlist)
                playlist_search_year = _infer_series_year_from_playlist(playlist)
                # 季标记要看完整标题（search_title 已剥离季后缀），重定向时以修正标题为准
                season_marker_source = session_vod.vod_name
                query_redirect = _session_query_redirect(session_vod, current_item)
                if query_redirect is not None:
                    # 手动修正过的查询优先：混淆目录名（如"X 心动的XH9"）搜不到任何 provider
                    redirect_title, redirect_year = query_redirect
                    search_title = _strip_search_season_suffix(redirect_title)
                    season_marker_source = redirect_title
                    if redirect_year:
                        year = redirect_year
                has_season_marker = _title_has_season_marker(season_marker_source)
                search_year = "" if has_season_marker else year
                search_results = _search_tv_cached(tmdb_client, search_title, year=search_year)
                if not search_results and search_title != session_vod.vod_name and not has_season_marker:
                    search_results = _search_tv_cached(tmdb_client, session_vod.vod_name, year=search_year)
                if (
                    not search_results
                    and playlist_search_title
                    and _normalize_title(playlist_search_title) not in {_normalize_title(search_title), _normalize_title(session_vod.vod_name)}
                ):
                    fallback_year = search_year or playlist_search_year
                    search_results = _search_tv_cached(tmdb_client, playlist_search_title, year=fallback_year)
                effective_vod = session_vod
                if (
                    playlist_search_title
                    and (
                        _normalize_title(playlist_search_title) != _normalize_title(session_vod.vod_name)
                        or (playlist_search_year and not year)
                    )
                ):
                    effective_vod = replace(
                        session_vod,
                        vod_name=playlist_search_title,
                        vod_year=year or playlist_search_year,
                    )
                candidate_vods: list[VodItem] = []
                if query_redirect is not None:
                    # 修正标题的候选排在最前，让官方平台（腾讯/爱奇艺等）按真实剧名
                    # 匹配分集标题
                    redirect_title, redirect_year = query_redirect
                    candidate_vods.append(
                        replace(
                            session_vod,
                            vod_name=redirect_title,
                            vod_year=year or redirect_year,
                        )
                    )
                requested_seasons: set[int] = set()
                for pair in season_episode_pairs:
                    if pair is None:
                        continue
                    requested_seasons.add(pair[0])
                if not requested_seasons:
                    requested_seasons.add(default_season)
                # TMDB's flat season/episode model cannot represent variety shows
                # (期上/中/下 + 加更 + 陪看 + 纯享), and its direct title assignment would
                # shadow the official-source match below. Skip it for variety playlists.
                if search_results and not preserve_playlist_order:
                    preferred_title = playlist_search_title or search_title
                    matched = _select_tmdb_search_match(
                        search_results,
                        preferred_title,
                        session_vod,
                        requested_seasons,
                        tmdb_client,
                    )
                    logger.info(
                        "Episode title enhancer TMDB search selected preferred_title=%s search_title=%s search_year=%s results=%s matched_id=%s matched_title=%s requested_seasons=%s",
                        preferred_title,
                        search_title,
                        search_year,
                        len(search_results),
                        str((matched or {}).get("id") or "").strip(),
                        str((matched or {}).get("name") or (matched or {}).get("title") or "").strip(),
                        sorted(requested_seasons),
                    )
                    tmdb_id = str(matched.get("id") or "").strip()
                    if tmdb_id:
                        include_season_prefix = len(requested_seasons) > 1
                        titles_by_season_episode = _load_tmdb_titles_by_season_episode(
                            tmdb_client,
                            session_vod,
                            tmdb_id=tmdb_id,
                            season_map={season_number: season_number for season_number in requested_seasons},
                            include_season_prefix=include_season_prefix,
                        )
                        unresolved_pairs = {
                            pair
                            for pair in season_episode_pairs
                            if pair is not None and pair not in titles_by_season_episode
                        }
                        if (
                            unresolved_pairs
                            and has_season_marker
                            and search_title != session_vod.vod_name
                        ):
                            raw_search_results = _search_tv_cached(tmdb_client, session_vod.vod_name, year="")
                            raw_matched = _select_tmdb_search_match(
                                raw_search_results,
                                session_vod.vod_name,
                                session_vod,
                                {1},
                                tmdb_client,
                            )
                            raw_tmdb_id = str((raw_matched or {}).get("id") or "").strip()
                            if raw_tmdb_id:
                                season_map = {
                                    pair[0]: 1
                                    for pair in unresolved_pairs
                                    if pair[0] == default_season
                                }
                                if season_map:
                                    titles_by_season_episode.update(
                                        _load_tmdb_titles_by_season_episode(
                                            tmdb_client,
                                            session_vod,
                                            tmdb_id=raw_tmdb_id,
                                            season_map=season_map,
                                            include_season_prefix=include_season_prefix,
                                        )
                                    )
                        for index, pair in enumerate(season_episode_pairs):
                            if pair is None:
                                continue
                            candidate = titles_by_season_episode.get(pair)
                            if candidate:
                                playlist[index].episode_display_title = candidate
                                playlist[index].episode_title_source = "tmdb"
                        titles_by_index = {
                            index: str(item.episode_display_title or "").strip()
                            for index, item in enumerate(playlist)
                            if str(item.episode_display_title or "").strip()
                        }
                        if titles_by_index:
                            finalized = _finalize_episode_playlist(
                                playlist,
                                season_episode_pairs,
                                original_playlist=current_playlist,
                                preserve_order=preserve_playlist_order,
                            )
                            if finalized is not None:
                                logger.info(
                                    "Episode title enhancer applied TMDB titles tmdb_id=%s mapped_count=%s unresolved_pairs=%s",
                                    tmdb_id,
                                    _count_mapped_episode_titles(finalized),
                                    len(unresolved_pairs),
                                )
                                playlist = finalized
                                season_episode_pairs = _season_episode_pairs(playlist, default_season)
                candidate_vods: list[VodItem] = []
                for candidate_vod in (effective_vod, session_vod):
                    identity = (
                        str(getattr(candidate_vod, "vod_name", "") or "").strip(),
                        str(getattr(candidate_vod, "vod_year", "") or "").strip(),
                    )
                    if not identity[0]:
                        continue
                    if identity in {
                        (
                            str(getattr(existing, "vod_name", "") or "").strip(),
                            str(getattr(existing, "vod_year", "") or "").strip(),
                        )
                        for existing in candidate_vods
                    }:
                        continue
                    candidate_vods.append(candidate_vod)
                candidate_results: list[tuple[VodItem, object]] = []
                for candidate_vod in candidate_vods:
                    for candidate in _search_metadata_candidates(candidate_vod, source_kind):
                        candidate_results.append((candidate_vod, candidate))
                dynamic_source_priority = resolve_episode_title_source_priority(
                    effective_vod,
                    playlist,
                    [candidate for _, candidate in candidate_results],
                    preferred_provider=str(getattr(bound_candidate, "provider", "") or "").strip(),
                )
                for candidate_vod, candidate in candidate_results:
                    logger.info(
                        "Episode title enhancer trying provider candidate provider=%s provider_id=%s candidate_title=%s candidate_year=%s query_title=%s query_year=%s",
                        str(getattr(candidate, "provider", "") or "").strip(),
                        str(getattr(candidate, "provider_id", "") or "").strip(),
                        str(getattr(candidate, "title", "") or "").strip(),
                        str(getattr(candidate, "year", "") or "").strip(),
                        str(candidate_vod.vod_name or "").strip(),
                        str(candidate_vod.vod_year or "").strip(),
                    )
                    updated_playlist = build_provider_episode_playlist(
                        candidate_vod,
                        playlist,
                        candidate,
                        source_priority=dynamic_source_priority,
                    )
                    if updated_playlist is None:
                        continue
                    updated_pairs = _season_episode_pairs(updated_playlist, default_season)
                    finalized = _finalize_episode_playlist(
                        updated_playlist,
                        updated_pairs,
                        original_playlist=current_playlist,
                        preserve_order=preserve_playlist_order,
                    )
                    if finalized is None:
                        continue
                    mapped_count = _count_mapped_episode_titles(finalized)
                    previous_mapped_count = _count_mapped_episode_titles(playlist)
                    if (
                        mapped_count > previous_mapped_count
                        or (
                            mapped_count == previous_mapped_count
                            and _episode_title_snapshot(finalized) != _episode_title_snapshot(playlist)
                            and _episode_title_source_rank(finalized, dynamic_source_priority)
                            < _episode_title_source_rank(playlist, dynamic_source_priority)
                        )
                    ):
                        logger.info(
                            "Episode title enhancer accepted provider candidate provider=%s mapped_count=%s previous_mapped_count=%s",
                            str(getattr(candidate, "provider", "") or "").strip(),
                            mapped_count,
                            previous_mapped_count,
                        )
                        playlist = finalized
                        season_episode_pairs = updated_pairs
                finalized_playlist = _finalize_episode_playlist(
                    playlist,
                    season_episode_pairs,
                    original_playlist=current_playlist,
                    preserve_order=preserve_playlist_order,
                )
                if finalized_playlist is not None:
                    logger.info(
                        "Episode title enhancer finalized mapped_count=%s sources=%s",
                        _count_mapped_episode_titles(finalized_playlist),
                        sorted({str(item.episode_title_source or "").strip() for item in finalized_playlist if str(item.episode_title_source or "").strip()}),
                    )
                    _save_episode_title_playlist_cache(source_kind, session_vod, current_playlist, finalized_playlist)
                    return finalized_playlist
                logger.info(
                    "Episode title enhancer finished without rewrite title=%s year=%s",
                    str(session_vod.vod_name or "").strip(),
                    str(session_vod.vod_year or "").strip(),
                )
                return playlist if playlist_has_title_variants(playlist) else None

            override_repo = self._episode_title_override_repository

            def enhance_with_overrides(session) -> list | None:
                # Manual per-episode overrides win over every auto-derived source
                # (manual is rank 0 in the source priority). Applied to the returned
                # playlist so the player-window merge carries the manual titles through.
                updated = enhance(session)
                if updated is None or override_repo is None:
                    return updated
                overrides = override_repo.load_for_session(
                    source_kind=source_kind,
                    source_key=str(getattr(session, "source_key", "") or ""),
                    vod_id=str(getattr(getattr(session, "vod", None), "vod_id", "") or ""),
                )
                if overrides:
                    apply_episode_title_overrides(updated, overrides)
                return updated

            return enhance_with_overrides

        return factory

    def _show_login(self, error_message: str = "") -> LoginWindow:
        logger.info("Show login window has_error=%s", bool(error_message))
        self._stop_playback_sync_service()
        self._stop_following_backend_sync_service()
        self._close_api_client()
        login_controller = LoginController(
            self.repo,
            lambda base_url: ApiClient(base_url, proxy_decider=self._build_proxy_decider()),
        )
        self.login_window = LoginWindow(login_controller)
        if error_message and hasattr(self.login_window, "set_error_message"):
            self.login_window.set_error_message(error_message)
        self.login_window.login_succeeded.connect(self._handle_login_succeeded)
        if self.main_window is not None:
            self.main_window.close()
            self.main_window = None
        return self.login_window

    def _call_plugin_loader(
        self,
        loader,
        *args,
        drive_detail_loader,
        offline_download_detail_loader,
        prioritized_plugin_ids: tuple[str, ...] = (),
        initialize_plugins: bool = True,
    ):
        try:
            parameters = inspect.signature(loader).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs = {}
        if accepts_kwargs or "drive_detail_loader" in parameters:
            kwargs["drive_detail_loader"] = drive_detail_loader
        if accepts_kwargs or "offline_download_detail_loader" in parameters:
            kwargs["offline_download_detail_loader"] = offline_download_detail_loader
        if accepts_kwargs or "prioritized_plugin_ids" in parameters:
            kwargs["prioritized_plugin_ids"] = prioritized_plugin_ids
        if accepts_kwargs or "initialize_plugins" in parameters:
            kwargs["initialize_plugins"] = initialize_plugins
        return loader(*args, **kwargs)

    def _startup_prioritized_plugin_ids(self, config: AppConfig) -> tuple[str, ...]:
        prioritized: list[str] = []
        if config.last_playback_source == "plugin" and config.last_playback_source_key:
            prioritized.append(config.last_playback_source_key)
        if config.last_selected_tab.startswith("plugin:"):
            prioritized.append(config.last_selected_tab.removeprefix("plugin:"))
        deduplicated: list[str] = []
        for plugin_id in prioritized:
            if plugin_id and plugin_id not in deduplicated:
                deduplicated.append(plugin_id)
        return tuple(deduplicated)

    def _load_startup_spider_plugins(
        self,
        drive_detail_loader,
        offline_download_detail_loader,
        prioritized_plugin_ids: tuple[str, ...] = (),
    ):
        iter_enabled_plugins = getattr(self._plugin_manager, "iter_enabled_plugins", None)
        if callable(iter_enabled_plugins):
            return self._call_plugin_loader(
                iter_enabled_plugins,
                drive_detail_loader=drive_detail_loader,
                offline_download_detail_loader=offline_download_detail_loader,
                prioritized_plugin_ids=prioritized_plugin_ids,
                initialize_plugins=not sys.platform.startswith("win"),
            )
        return self._call_plugin_loader(
            self._plugin_manager.load_enabled_plugins,
            drive_detail_loader=drive_detail_loader,
            offline_download_detail_loader=offline_download_detail_loader,
            initialize_plugins=not sys.platform.startswith("win"),
        )

    def _show_main(self):
        self._close_api_client()
        self._api_client = self._build_api_client()
        identity = str(
            getattr(self._api_client, "playback_sync_identity", "") or ""
        ).strip()
        if self._playback_history_repository is not None and identity:
            self._playback_history_repository.set_active_account(identity)
        metadata_hydrator_factory = self._build_metadata_hydrator_factory(self._api_client)
        metadata_scrape_service_factory = self._build_metadata_scrape_service_factory(self._api_client)
        danmaku_controller_factory = self._build_danmaku_controller_factory()
        episode_title_enhancer_factory = self._build_episode_title_enhancer_factory(self._api_client)
        setattr(self._plugin_manager, "_metadata_hydrator_factory", metadata_hydrator_factory)
        setattr(self._plugin_manager, "_metadata_scrape_service_factory", metadata_scrape_service_factory)
        setattr(self._plugin_manager, "_episode_title_enhancer_factory", episode_title_enhancer_factory)
        config = self.repo.load_config()
        self._refresh_danmaku_ai_enrichment(config)
        following_ai_enrichment_service = self._build_ai_enrichment_service(config, workflow="following")
        capabilities = self._load_capabilities(self._api_client)
        drive_detail_loader = getattr(self._api_client, "get_drive_share_detail", None)
        drive_resolver = getattr(self._api_client, "resolve_drive", None)
        drive_files_loader = getattr(self._api_client, "list_drive_files", None)
        offline_download_detail_loader = getattr(self._api_client, "get_offline_download_detail", None)
        prioritized_plugin_ids = self._startup_prioritized_plugin_ids(config)
        def plugin_loader_task():
            if getattr(config, "home_mode", "browse") == "classic":
                selected_plugin_id = ""
                if config.last_selected_tab.startswith("plugin:"):
                    selected_plugin_id = config.last_selected_tab.removeprefix("plugin:")
                if not selected_plugin_id:
                    return []
                load_plugins = getattr(self._plugin_manager, "load_plugins", None)
                if callable(load_plugins):
                    return self._call_plugin_loader(
                        load_plugins,
                        [selected_plugin_id],
                        drive_detail_loader=drive_detail_loader,
                        offline_download_detail_loader=offline_download_detail_loader,
                    )
                return []
            return self._load_startup_spider_plugins(
                drive_detail_loader,
                offline_download_detail_loader,
                prioritized_plugin_ids,
            )
        live_epg_service = _NullLiveEpgService()
        if self._live_epg_repository is not None:
            live_epg_service = LiveEpgService(
                self._live_epg_repository,
                http_client=_HttpTextClient(self._api_client),
            )
        live_source_manager = CustomLiveService(
            self._live_source_repository,
            http_client=_HttpTextClient(self._api_client),
            epg_service=live_epg_service,
        )
        douban_controller = DoubanController(self._api_client)
        global_catalog_controller = GlobalCatalogController.from_config_tmdb_key(
            config.metadata_tmdb_api_key,
            tmdb_proxy_base_url=config.metadata_tmdb_proxy_base_url,
        )
        media_detail_controller = None
        if _has_tmdb_access(config):
            media_detail_controller = MediaDetailController(
                client=_build_tmdb_client(
                    api_key=config.metadata_tmdb_api_key,
                    proxy_base_url=config.metadata_tmdb_proxy_base_url,
                    proxy_decider=self._build_proxy_decider(),
                ),
                metadata_search_service=self._build_following_metadata_search_service(self._api_client)
                if config.metadata_enhancement_enabled
                else None,
            )
        telegram_controller = TelegramSearchController(
            self._api_client,
            playback_history_loader=None
            if self._playback_history_repository is None
            else lambda vod_id: self._playback_history_repository.get_history("telegram", vod_id),
            playback_history_saver=None
            if self._playback_history_repository is None
            else lambda vod_id, payload: self._playback_history_repository.save_history(
                "telegram",
                vod_id,
                payload,
                source_name="电报影视",
            ),
            drive_resolver=drive_resolver,
            drive_files_loader=drive_files_loader,
        )
        telegram_channel_controller = TelegramChannelController(
            self._api_client,
            playback_history_loader=None
            if self._playback_history_repository is None
            else lambda vod_id: self._playback_history_repository.get_history("telegram_channel", vod_id),
            playback_history_saver=None
            if self._playback_history_repository is None
            else lambda vod_id, payload: self._playback_history_repository.save_history(
                "telegram_channel",
                vod_id,
                payload,
                source_name="电报频道",
            ),
            drive_resolver=drive_resolver,
            drive_files_loader=drive_files_loader,
        )
        live_controller = LiveController(self._api_client, custom_live_service=live_source_manager)
        bilibili_controller = BilibiliController(
            self._api_client,
            playback_history_loader=None
            if self._playback_history_repository is None
            else lambda vod_id: self._playback_history_repository.get_history("bilibili", vod_id),
            playback_history_saver=None
            if self._playback_history_repository is None
            else lambda vod_id, payload: self._playback_history_repository.save_history(
                "bilibili",
                vod_id,
                payload,
                source_name="B站",
            ),
        )
        show_youtube_tab = bool(self._yt_dlp_service is not None and self._yt_dlp_service.is_available())
        youtube_controller = None
        if show_youtube_tab:
            def youtube_category_config_loader(config=config):
                loaded = load_youtube_category_config(
                    config,
                    text_loader=self._api_client.get_text if self._api_client is not None else None,
                    save_config=lambda: self.repo.save_config(config),
                    builtin_categories=default_youtube_categories(),
                )
                return loaded.categories

            youtube_controller = YouTubeController(
                config,
                yt_dlp_service=self._yt_dlp_service,
                category_config_loader=youtube_category_config_loader,
                playback_history_loader=None
                if self._playback_history_repository is None
                else lambda vod_id: self._playback_history_repository.get_history("youtube", vod_id),
                playback_history_saver=None
                if self._playback_history_repository is None
                else lambda vod_id, payload: self._playback_history_repository.save_history(
                    "youtube",
                    vod_id,
                    payload,
                    source_name="YouTube",
                ),
            )
        emby_controller = EmbyController(
            self._api_client,
            playback_history_loader=None
            if self._playback_history_repository is None
            else lambda vod_id: self._playback_history_repository.get_history("emby", vod_id),
            playback_history_saver=None
            if self._playback_history_repository is None
            else lambda vod_id, payload: self._playback_history_repository.save_history(
                "emby",
                vod_id,
                payload,
                source_name="Emby",
            ),
        )
        jellyfin_controller = JellyfinController(
            self._api_client,
            playback_history_loader=None
            if self._playback_history_repository is None
            else lambda vod_id: self._playback_history_repository.get_history("jellyfin", vod_id),
            playback_history_saver=None
            if self._playback_history_repository is None
            else lambda vod_id, payload: self._playback_history_repository.save_history(
                "jellyfin",
                vod_id,
                payload,
                source_name="Jellyfin",
            ),
        )
        feiniu_controller = FeiniuController(
            self._api_client,
            playback_history_loader=None
            if self._playback_history_repository is None
            else lambda vod_id: self._playback_history_repository.get_history("feiniu", vod_id),
            playback_history_saver=None
            if self._playback_history_repository is None
            else lambda vod_id, payload: self._playback_history_repository.save_history(
                "feiniu",
                vod_id,
                payload,
                source_name="飞牛影视",
            ),
        )
        browse_controller = BrowseController(
            self._api_client,
            playback_history_loader=None
            if self._playback_history_repository is None
            else lambda source_key, vod_id: self._playback_history_repository.get_history(
                "browse", vod_id, source_key
            ),
            playback_history_saver=None
            if self._playback_history_repository is None
            else lambda source_key, vod_id, payload: self._playback_history_repository.save_history(
                "browse",
                vod_id,
                payload,
                source_key=source_key,
                source_name="AList",
            ),
        )
        pansou_controller = PansouController(browse_controller) if bool(capabilities.get("pansou")) else None
        history_controller = HistoryController(self._api_client, self._playback_history_repository)
        favorites_controller = FavoritesController(
            self._favorites_repository,
            detail_loader_by_source={
                "browse": lambda record, controller=browse_controller: controller.build_request_from_detail(record.vod_id).vod,
                "telegram": lambda record, controller=telegram_controller: controller.build_request(record.vod_id).vod,
                "telegram_channel": lambda record, controller=telegram_channel_controller: controller.build_request(record.vod_id).vod,
                "bilibili": lambda record, controller=bilibili_controller: controller.build_request(record.vod_id).vod,
                "youtube": lambda record, controller=youtube_controller: None if controller is None else controller.build_request(record.vod_id).vod,
                "live": lambda record, controller=live_controller: controller.build_request(record.vod_id).vod,
                "emby": lambda record, controller=emby_controller: controller.build_request(record.vod_id).vod,
                "jellyfin": lambda record, controller=jellyfin_controller: controller.build_request(record.vod_id).vod,
                "feiniu": lambda record, controller=feiniu_controller: controller.build_request(record.vod_id).vod,
            },
            tmdb_binding_repository=self._favorite_tmdb_binding_repository,
        )
        following_controller = None
        following_update_service = None
        following_backend_sync_service = None
        if self._following_repository is not None:
            following_search_service = self._build_following_metadata_search_service(self._api_client)
            following_discovery_service = self._build_following_tmdb_discovery_service()
            following_update_service = FollowingUpdateService(
                self._following_repository,
                metadata_gateway=FollowingMetadataGateway(following_search_service),
            )
            following_backend_sync_service = FollowingBackendSyncService(
                self._api_client,
                self._following_repository,
                settings_provider=lambda: {
                    "enabled": config.following_backend_enabled,
                    "auto_subscribe": config.following_backend_auto_subscribe,
                },
                parent=self,
            )
            self._stop_following_backend_sync_service()
            self._following_backend_sync_service = following_backend_sync_service
            following_controller = FollowingController(
                self._following_repository,
                metadata_search_service=following_search_service,
                update_service=following_update_service,
                discovery_service=following_discovery_service,
                favorite_tmdb_binding_repository=self._favorite_tmdb_binding_repository,
                ai_enrichment_service=following_ai_enrichment_service,
                backend_sync_service=following_backend_sync_service,
            )
        msub_controller = MsubController(
            self._api_client,
            playback_history_loader=None
            if self._playback_history_repository is None
            else lambda vod_id: self._playback_history_repository.get_history("msub", vod_id),
            playback_history_saver=None
            if self._playback_history_repository is None
            else lambda vod_id, payload: self._playback_history_repository.save_history(
                "msub",
                vod_id,
                payload,
                source_name="追剧",
            ),
        )
        smart_search_controller = self._build_smart_search_controller(
            config,
            favorites_controller=favorites_controller,
            following_controller=following_controller,
            history_controller=history_controller,
        )
        app_identity = self.repo.ensure_app_identity()
        heat_controller = HeatController(
            HeatService(),
            installation_id=app_identity.installation_id,
        )
        self._stop_playback_sync_service()
        if self._playback_history_repository is not None:
            self._playback_sync_service = PlaybackHistorySyncService(
                self._api_client,
                self._playback_history_repository,
                installation_id=app_identity.installation_id,
                to_sync_source_key=self._to_playback_sync_source_key,
                to_local_source_key=self._to_local_playback_source_key,
                playback_source_keys_loader=self._playback_sync_source_keys,
                is_playing_provider=self._playback_sync_is_playing,
                parent=self,
            )
        player_controller = PlayerController(self._api_client)
        self._start_live_background_refresh(live_source_manager, live_epg_service)
        logger.info(
            "Show main window bilibili=%s emby=%s jellyfin=%s feiniu=%s telegram_channel=%s spider_plugins=%s",
            bool(capabilities.get("bilibili")),
            bool(capabilities.get("emby")),
            bool(capabilities.get("jellyfin")),
            bool(capabilities.get("feiniu")),
            bool(capabilities.get("telegram_channel")),
            0,
        )
        self.main_window = MainWindow(
            browse_controller=browse_controller,
            favorites_controller=favorites_controller,
            following_controller=following_controller,
            following_update_service=following_update_service,
            history_controller=history_controller,
            player_controller=player_controller,
            config=config,
            app_log_service=self._app_log_service,
            save_config=lambda: self._save_shared_config(config),
            apply_theme=lambda: apply_saved_theme(QApplication.instance(), self.repo),
            vod_token_options=self._vod_token_options,
            can_select_vod_token=self._vod_token_is_admin,
            on_vod_token_changed=self._apply_vod_token_change,
            douban_controller=douban_controller,
            global_catalog_controller=global_catalog_controller,
            media_detail_controller=media_detail_controller,
            telegram_controller=telegram_controller,
            telegram_channel_controller=telegram_channel_controller,
            bilibili_controller=bilibili_controller,
            youtube_controller=youtube_controller,
            live_controller=live_controller,
            live_source_manager=live_source_manager,
            emby_controller=emby_controller,
            jellyfin_controller=jellyfin_controller,
            feiniu_controller=feiniu_controller,
            pansou_controller=pansou_controller,
            msub_controller=msub_controller,
            spider_plugins=[],
            plugin_loader_task=plugin_loader_task,
            plugin_manager=self._plugin_manager,
            drive_detail_loader=drive_detail_loader,
            drive_resolver=drive_resolver,
            drive_files_loader=drive_files_loader,
            offline_download_detail_loader=offline_download_detail_loader,
            direct_parse_detail_loader=load_direct_parse_detail,
            direct_parse_danmaku_loader=lambda url: load_direct_parse_danmaku(
                url,
                get=self._proxy_http_get(),
                proxy_decider=self._build_proxy_decider(),
            ),
            direct_parse_playback_history_loader=None
            if self._playback_history_repository is None
            else lambda vod_id: self._playback_history_repository.get_history("direct_parse", vod_id),
            direct_parse_playback_history_saver=None
            if self._playback_history_repository is None
            else lambda vod_id, payload: self._playback_history_repository.save_history(
                "direct_parse",
                vod_id,
                payload,
                source_name="全局解析",
            ),
            default_video_cover_loader=getattr(self._api_client, "get_video_cover", None),
            show_telegram_channel_tab=bool(capabilities.get("telegram_channel")),
            show_bilibili_tab=bool(capabilities.get("bilibili")),
            show_youtube_tab=show_youtube_tab,
            show_emby_tab=bool(capabilities.get("emby")),
            show_jellyfin_tab=bool(capabilities.get("jellyfin")),
            show_feiniu_tab=bool(capabilities.get("feiniu")),
            m3u8_ad_filter=self._m3u8_ad_filter,
            playback_parser_service=self._playback_parser_service,
            yt_dlp_service=self._yt_dlp_service,
            smart_search_controller=smart_search_controller,
            heat_controller=heat_controller,
            youtube_category_text_loader=getattr(self._api_client, "get_text", None),
            metadata_hydrator_factory=metadata_hydrator_factory,
            metadata_scrape_service_factory=metadata_scrape_service_factory,
            subtitle_search_service=self._subtitle_search_service,
            danmaku_controller_factory=danmaku_controller_factory,
            episode_title_enhancer_factory=episode_title_enhancer_factory,
            metadata_binding_repository=self._metadata_binding_repository,
            episode_title_override_repository=self._episode_title_override_repository,
            danmaku_preference_store=self._danmaku_preference_store,
        )
        self.main_window.logout_requested.connect(self._handle_logout_requested)
        if following_update_service is not None:
            following_update_service.start()
        if following_backend_sync_service is not None:
            following_backend_sync_service.start()
            following_backend_sync_service.sync_finished.connect(self._handle_following_backend_sync)
        if self._playback_sync_service is not None:
            self._playback_sync_service.start()
        if self.main_window is not None:
            self._activation_pull_filter = _ActivationPullFilter(
                lambda: (
                    self._playback_sync_service.pull_soon()
                    if self._playback_sync_service is not None
                    else None,
                    self._following_backend_sync_service.sync_soon()
                    if self._following_backend_sync_service is not None
                    else None,
                ),
                self.main_window,
            )
            self.main_window.installEventFilter(self._activation_pull_filter)
        if self.login_window is not None:
            self.login_window.close()
            self.login_window = None
        if config.last_active_window == "player":
            start_restore_last_player = getattr(self.main_window, "_start_restore_last_player", None)
            if callable(start_restore_last_player):
                start_restore_last_player()
                return self.main_window
            try:
                restored = self.main_window.restore_last_player()
            except Exception:
                config.last_active_window = "main"
                self._save_shared_config(config)
            else:
                if restored is not None:
                    return restored
        return self.main_window

    def _stop_playback_sync_service(self) -> None:
        if self._activation_pull_filter is not None:
            if self.main_window is not None:
                self.main_window.removeEventFilter(self._activation_pull_filter)
            self._activation_pull_filter.deleteLater()
            self._activation_pull_filter = None
        if self._playback_sync_service is None:
            return
        # 关闭/登出/重建前 flush 最后一次 PUSH,避免 <30s tick 窗口内的进度丢失。
        self._playback_sync_service.flush()
        self._playback_sync_service.deleteLater()
        self._playback_sync_service = None

    def _handle_following_backend_sync(self, results: object) -> None:
        main_window = self.main_window
        if main_window is None:
            return
        show_prompts = getattr(main_window, "show_following_homepage_prompts", None)
        if callable(show_prompts):
            show_prompts()
        following_page = getattr(main_window, "following_page", None)
        load_page = getattr(following_page, "load_page", None)
        nav_tabs = getattr(main_window, "nav_tabs", None)
        page_is_visible = (
            following_page is not None
            and nav_tabs is not None
            and nav_tabs.currentWidget() is following_page
        )
        if callable(load_page) and page_is_visible:
            load_page()

    def _stop_following_backend_sync_service(self) -> None:
        service = getattr(self, "_following_backend_sync_service", None)
        if service is None:
            return
        service.stop()
        service.deleteLater()
        self._following_backend_sync_service = None

    def _to_playback_sync_source_key(self, source_kind: str, source_key: str) -> str | None:
        if source_kind != "spider_plugin":
            return source_key
        if self._plugin_repository is None:
            return None
        try:
            plugin = self._plugin_repository.get_plugin(int(source_key))
        except (AssertionError, TypeError, ValueError):
            return None
        return plugin.manifest_id.strip() or None

    def _to_local_playback_source_key(self, source_kind: str, source_key: str) -> str | None:
        if source_kind != "spider_plugin":
            return source_key
        if self._plugin_repository is None:
            return None
        plugin = self._plugin_repository.find_plugin_by_manifest_id(source_key)
        return str(plugin.id) if plugin is not None else None

    def _playback_sync_is_playing(self) -> bool:
        """同步 PUSH 限流用:播放器窗口处于播放中才算"正在播放"(暂停/无窗口均不算)。

        供 worker 线程调用,只读普通 Python 属性,不触碰 Qt 可见性等跨线程不安全接口。
        """
        window = self.main_window
        player = getattr(window, "player_window", None) if window is not None else None
        return bool(getattr(player, "is_playing", False))

    def _playback_sync_source_keys(self) -> list[str]:
        if self._plugin_repository is None:
            return []
        return [
            plugin.manifest_id.strip()
            for plugin in self._plugin_repository.list_plugins()
            if plugin.enabled and plugin.manifest_id.strip()
        ]

    def _start_live_background_refresh(self, live_source_manager, live_epg_service) -> None:
        def refresh_epg() -> None:
            try:
                config = live_epg_service.load_config()
                if config.epg_url.strip() and is_refresh_stale(getattr(config, "last_refreshed_at", 0)):
                    live_epg_service.refresh()
                    logger.info(
                        "Background refresh finished target=epg",
                        extra={"log_category": "live", "log_source": "app"},
                    )
            except Exception:
                logger.exception(
                    "Background refresh failed target=epg",
                    extra={"log_category": "live", "log_source": "app"},
                )
                return

        def refresh_sources() -> None:
            now = int(time.time())
            for source in live_source_manager.list_sources():
                if source.source_type == "manual":
                    continue
                if not is_refresh_stale(getattr(source, "last_refreshed_at", 0), now=now):
                    continue
                try:
                    live_source_manager.refresh_source(source.id)
                    logger.info(
                        "Background refresh finished target=live-source source_id=%s",
                        source.id,
                        extra={"log_category": "live", "log_source": "app"},
                    )
                except Exception:
                    logger.exception(
                        "Background refresh failed target=live-source source_id=%s",
                        source.id,
                        extra={"log_category": "live", "log_source": "app"},
                    )
                    continue

        threading.Thread(target=refresh_epg, daemon=True).start()
        threading.Thread(target=refresh_sources, daemon=True).start()

    def _load_capabilities(self, api_client: ApiClient) -> dict[str, bool]:
        default_capabilities = {"bilibili": False, "emby": True, "jellyfin": True, "feiniu": True, "pansou": False, "telegram_channel": False}
        get_capabilities = getattr(api_client, "get_capabilities", None)
        if not callable(get_capabilities):
            return default_capabilities
        try:
            response = get_capabilities()
        except (ApiError, UnauthorizedError):
            logger.warning("Load capabilities failed, fallback to defaults")
            return default_capabilities
        if not isinstance(response, dict):
            logger.warning("Load capabilities returned invalid payload, fallback to defaults")
            return default_capabilities
        capabilities = dict(default_capabilities)
        capabilities["bilibili"] = bool(response.get("bilibili", capabilities["bilibili"]))
        capabilities["emby"] = bool(response.get("emby", capabilities["emby"]))
        capabilities["jellyfin"] = bool(response.get("jellyfin", capabilities["jellyfin"]))
        capabilities["feiniu"] = bool(response.get("feiniu", capabilities["feiniu"]))
        capabilities["pansou"] = bool(response.get("pansou", capabilities["pansou"]))
        capabilities["telegram_channel"] = bool(response.get("telegram_channel", capabilities["telegram_channel"]))
        return capabilities

    def _handle_login_succeeded(self) -> None:
        logger.info("Login succeeded")
        try:
            if not self._select_vod_token_after_login():
                if self.login_window is not None:
                    self.login_window.set_error_message("请选择 VOD Token 后再继续登录")
                return
            widget = self._show_main()
        except (ApiError, UnauthorizedError) as exc:
            logger.exception("Failed to initialize after login error=%s", exc)
            widget = self._show_login(error_message=str(exc))
        widget.show()

    def _handle_logout_requested(self) -> None:
        logger.info("Logout requested")
        try:
            self._api_client.logout()
        except:
            pass
        self.repo.clear_token()
        widget = self._show_login()
        widget.show()

    def close(self) -> None:
        self._stop_playback_sync_service()
        self._stop_following_backend_sync_service()
        close_filter = getattr(self._m3u8_ad_filter, "close", None)
        if callable(close_filter):
            close_filter()
        self._close_api_client()
