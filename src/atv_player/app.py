from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import gc
import inspect
import threading
import time
import logging
import re
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QPushButton, QToolButton, QWidget

from atv_player.api import ApiClient, ApiError, UnauthorizedError
from atv_player.danmaku.cache import purge_stale_danmaku_cache
from atv_player.danmaku.direct_parse import load_direct_parse_danmaku
from atv_player.danmaku.service import create_default_danmaku_service
from atv_player.custom_live_service import CustomLiveService
from atv_player.controllers.browse_controller import BrowseController
from atv_player.controllers.douban_controller import DoubanController
from atv_player.controllers.bilibili_controller import BilibiliController
from atv_player.controllers.emby_controller import EmbyController
from atv_player.controllers.feiniu_controller import FeiniuController
from atv_player.controllers.jellyfin_controller import JellyfinController
from atv_player.controllers.live_controller import LiveController
from atv_player.controllers.history_controller import HistoryController
from atv_player.controllers.login_controller import LoginController
from atv_player.controllers.player_controller import PlayerController
from atv_player.controllers.pansou_controller import PansouController
from atv_player.controllers.telegram_search_controller import TelegramSearchController
from atv_player.danmaku.utils import infer_playlist_episode_number
from atv_player.diagnostics import resolve_app_version
from atv_player.episode_titles import (
    apply_episode_title_index_map,
    extract_season_number,
    normalize_episode_title_text,
    playlist_has_title_variants,
    seed_original_titles,
)
from atv_player.live_epg_repository import LiveEpgRepository
from atv_player.live_epg_service import LiveEpgService
from atv_player.local_playback_history import LocalPlaybackHistoryRepository
from atv_player.metadata import (
    METADATA_EPISODE_TITLE_SOURCE_PRIORITY,
    MetadataBindingRepository,
    MetadataCache,
    MetadataContext,
    MetadataHydrator,
    build_provider_episode_playlist,
)
from atv_player.metadata.providers.bangumi import BangumiMetadataProvider
from atv_player.metadata.providers.bangumi_client import BangumiClient
from atv_player.metadata.providers.bilibili import BilibiliMetadataProvider
from atv_player.metadata.providers.iqiyi import IqiyiMetadataProvider
from atv_player.metadata.providers.local_douban import OfficialDoubanProvider
from atv_player.metadata.providers.local_douban_client import LocalDoubanClient
from atv_player.metadata.providers.plugin import CustomPluginProvider
from atv_player.metadata.providers.remote_douban import LocalDoubanProvider
from atv_player.metadata.scrape import MetadataScrapeService
from atv_player.metadata.providers.tencent import TencentMetadataProvider
from atv_player.metadata.providers.tmdb import TMDBProvider, infer_tmdb_media_type
from atv_player.metadata.providers.tmdb_client import TMDBClient
from atv_player.models import AppConfig, LiveEpgConfig, PlayItem, VodItem
from atv_player.network_client import NetworkClient
from atv_player.network_proxy import ProxyConfig, ProxyDecider
from atv_player.paths import app_cache_dir, app_data_dir
from atv_player.live_source_repository import LiveSourceRepository
from atv_player.plugins import SpiderPluginLoader, SpiderPluginManager
from atv_player.plugins.compat.base.spider import set_proxy_decider_loader as set_spider_proxy_decider_loader
from atv_player.plugins.compat.base.spider import set_session_loader as set_spider_session_loader
from atv_player.plugins.repository import SpiderPluginRepository
from atv_player.playback_parsers import BuiltInPlaybackParserService
from atv_player.player.m3u8_ad_filter import M3U8AdFilter
from atv_player.proxy.server import LocalHlsProxyServer
from atv_player.yt_dlp_service import YtdlpPlaybackService
from atv_player.storage import SettingsRepository
from atv_player.time_utils import is_refresh_stale
from atv_player.ui.poster_loader import set_http_get_loader, set_proxy_decider_loader
from atv_player.ui.login_window import LoginWindow
from atv_player.ui.main_window import (
    MainWindow,
    load_direct_parse_detail,
    set_main_window_http_get_loader,
    set_main_window_http_post_loader,
)
from atv_player.ui.player_window import set_player_window_http_get_loader
from atv_player.ui.icon_cache import load_icon

POSTER_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_MAIN_THREAD_GC_INTERVAL_MS = 30_000
_METADATA_SEARCH_CACHE_TTL_SECONDS = 7 * 24 * 3600
_METADATA_EMPTY_SEARCH_CACHE_TTL_SECONDS = 3600
_METADATA_DETAIL_CACHE_TTL_SECONDS = 7 * 24 * 3600
_EPISODE_SORT_SENTINEL = 10**9
_QUALITY_VARIANT_EPISODE_RE = re.compile(r"^\s*0*(\d{1,3})\s*[-_. ]\s*(?:4k|2160p|1080p|720p|480p|360p)\b", re.IGNORECASE)
logger = logging.getLogger(__name__)


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


def decide_start_view(config: AppConfig) -> str:
    return "main" if config.token else "login"


def _proxy_signature(config: AppConfig) -> tuple[str, str, tuple[str, ...]]:
    return (
        config.network_proxy_mode,
        config.network_proxy_url,
        tuple(config.network_proxy_bypass_rules),
    )


def _make_proxy_invalidation_wrapper(save_fn, config: AppConfig, network: NetworkClient):
    last = [_proxy_signature(config)]

    def wrapped() -> None:
        save_fn()
        current = _proxy_signature(config)
        if current != last[0]:
            last[0] = current
            network.invalidate_proxy()

    return wrapped


def _app_icon_path() -> Path:
    return Path(__file__).resolve().parent / "icons" / "app.svg"


def purge_stale_poster_cache(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - POSTER_CACHE_MAX_AGE_SECONDS
    cache_dir = app_cache_dir() / "posters"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for entry in cache_dir.iterdir():
        try:
            if not entry.is_file():
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


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
        match = _QUALITY_VARIANT_EPISODE_RE.match(candidate)
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
        r"[.\s_-]*0*\d{1,4}\s*[-_. ]\s*(?:4k|2160p|1080p|720p|480p|360p)\b.*$",
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


def build_application() -> tuple[QApplication, SettingsRepository]:
    app = QApplication([])
    _install_button_pointing_hand_cursor(app)
    _install_main_thread_gc_workaround(app)
    app.setApplicationName("atv-player")
    if hasattr(app, "setApplicationVersion"):
        app.setApplicationVersion(resolve_app_version())
    app.setWindowIcon(load_icon(_app_icon_path()))
    data_dir = app_data_dir()
    repo = SettingsRepository(data_dir / "app.db")
    purge_stale_poster_cache()
    threading.Thread(target=purge_stale_danmaku_cache, daemon=True).start()
    logger.info("Application initialized data_dir=%s", data_dir)
    return app, repo


class AppCoordinator(QObject):
    def __init__(self, repo: SettingsRepository) -> None:
        super().__init__()
        self.repo = repo
        self.login_window: LoginWindow | None = None
        self.main_window: MainWindow | None = None
        self._api_client: ApiClient | None = None
        self._network = NetworkClient(self._build_proxy_decider)
        set_proxy_decider_loader(lambda: self._network.proxy_decider)
        set_http_get_loader(lambda: self._network.get)
        set_main_window_http_get_loader(lambda: self._network.get)
        set_main_window_http_post_loader(lambda: self._network.post)
        set_player_window_http_get_loader(lambda: self._network.get)
        set_spider_proxy_decider_loader(lambda: self._network.proxy_decider)
        set_spider_session_loader(lambda: self._network.requests_session())
        self._m3u8_ad_filter = M3U8AdFilter(
            proxy_server=LocalHlsProxyServer(
                get=self._network.get,
                stream=self._network.stream,
            ),
            get=self._network.get,
        )
        self._playback_parser_service = BuiltInPlaybackParserService(
            get=self._network.get,
            post=self._network.post,
        )
        self._yt_dlp_service = YtdlpPlaybackService(
            proxy_decider=self._build_proxy_decider(),
            config_loader=self.repo.load_config,
        )
        self._danmaku_service = create_default_danmaku_service(
            get=self._network.get,
            post=self._network.post,
        )
        if hasattr(repo, "database_path"):
            self._live_source_repository = LiveSourceRepository(repo.database_path)
            self._live_epg_repository = LiveEpgRepository(repo.database_path)
            self._plugin_repository = SpiderPluginRepository(repo.database_path)
            self._playback_history_repository = LocalPlaybackHistoryRepository(repo.database_path)
            cache_dir = app_cache_dir() / "plugins"
            self._plugin_loader = SpiderPluginLoader(cache_dir, get=self._network.get)
            self._plugin_manager = SpiderPluginManager(
                self._plugin_repository,
                self._plugin_loader,
                self._playback_history_repository,
            )
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
            self._plugin_loader = None
            self._plugin_manager = _NullPluginManager()
        self._metadata_binding_repository = (
            MetadataBindingRepository(repo.database_path)
            if hasattr(repo, "database_path")
            else None
        )

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
            )
        )

    def _proxy_http_get(self):
        return self._network.get

    def _proxy_http_post(self):
        return self._network.post

    def _proxy_http_stream(self):
        return self._network.stream

    def start(self) -> QWidget:
        config = self.repo.load_config()
        logger.info("App start view=%s", decide_start_view(config))
        if decide_start_view(config) == "main":
            self._api_client = ApiClient(
                config.base_url,
                token=config.token,
                vod_token=config.vod_token,
                proxy_decider=self._build_proxy_decider(),
            )
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
        api_client = ApiClient(
            config.base_url,
            token=config.token,
            vod_token=config.vod_token,
            proxy_decider=self._build_proxy_decider(),
        )
        self._ensure_vod_token(api_client)
        return api_client

    def _ensure_vod_token(self, api_client: ApiClient) -> str:
        config = self.repo.load_config()
        if config.vod_token:
            api_client.set_vod_token(config.vod_token)
            return config.vod_token
        vod_token = api_client.fetch_vod_token()
        config.vod_token = vod_token
        self.repo.save_config(config)
        logger.info("Fetched and stored vod token")
        return vod_token

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
        proxy_decider = self._build_proxy_decider()
        if source_kind == "plugin":
            plugin_payload = self._build_plugin_metadata_payload(raw_detail)
            if plugin_payload is not None:
                providers.append(CustomPluginProvider(plugin_payload))
        providers.append(
            BangumiMetadataProvider(
                BangumiClient(
                    access_token=config.metadata_bangumi_access_token,
                    proxy_decider=proxy_decider,
                )
            )
        )
        providers.append(BilibiliMetadataProvider())
        providers.append(IqiyiMetadataProvider())
        providers.append(TencentMetadataProvider())
        if str(config.metadata_douban_cookie or "").strip():
            local_douban_client = LocalDoubanClient(
                cookie=config.metadata_douban_cookie,
                proxy_decider=proxy_decider,
            )
            providers.append(OfficialDoubanProvider(local_douban_client))
        if str(config.metadata_tmdb_api_key or "").strip():
            providers.append(
                TMDBProvider(
                    TMDBClient(
                        api_key=config.metadata_tmdb_api_key,
                        proxy_decider=proxy_decider,
                    )
                )
            )
        providers.append(LocalDoubanProvider(api_client))
        return providers

    def _build_metadata_hydrator_factory(self, api_client: ApiClient):
        cache = MetadataCache(app_cache_dir() / "metadata")
        supported_sources = {"browse", "plugin", "emby", "jellyfin", "feiniu", "bilibili"}

        def factory(*, request=None, source_kind: str = "", source_key: str = "", vod=None, raw_detail=None):
            del request
            if vod is None or source_kind not in supported_sources:
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
        supported_sources = {"browse", "plugin", "emby", "jellyfin", "feiniu", "bilibili"}

        def factory(*, request=None, source_kind: str = "", source_key: str = "", vod=None, raw_detail=None):
            del request, source_key, vod
            if source_kind not in supported_sources:
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
            return MetadataScrapeService(cache=cache, providers=providers)

        return factory

    def _build_episode_title_enhancer_factory(self, api_client: ApiClient):
        del api_client
        cache = MetadataCache(app_cache_dir() / "metadata")

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
                return [dict(item) for item in payload if isinstance(item, Mapping)]
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
            category_name = str(getattr(vod, "category_name", "") or "").strip().lower()
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
        ) -> list[PlayItem] | None:
            if not playlist_has_title_variants(playlist):
                return None
            resolved_pairs = [pair for pair in season_episode_pairs if pair is not None]
            has_multi_version_pairs = len(resolved_pairs) != len(set(resolved_pairs))
            if len(playlist) > 1:
                indexed_playlist = list(enumerate(playlist))
                if has_multi_version_pairs:
                    occurrence_by_pair: dict[tuple[int, int], int] = {}
                    version_slot_by_index: dict[int, int] = {}
                    for index, pair in enumerate(season_episode_pairs):
                        if pair is None:
                            version_slot_by_index[index] = _EPISODE_SORT_SENTINEL
                            continue
                        version_slot_by_index[index] = occurrence_by_pair.get(pair, 0)
                        occurrence_by_pair[pair] = version_slot_by_index[index] + 1
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

        def _search_metadata_candidates(session_vod: VodItem, provider_source_kind: str) -> list[object]:
            query = MetadataContext(vod=session_vod, source_kind=provider_source_kind).to_query()
            candidates: list[object] = []
            for provider in (TencentMetadataProvider(), IqiyiMetadataProvider()):
                try:
                    matches = provider.search(query)
                except Exception:
                    continue
                if matches:
                    candidates.append(matches[0])
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

        def factory(*, request=None, source_kind: str = "", source_key: str = "", vod=None, raw_detail=None):
            del request, source_key, raw_detail
            if source_kind not in {"plugin", "browse"} or vod is None:
                return None
            config = self.repo.load_config()
            if not config.metadata_enhancement_enabled:
                return None
            if not config.episode_title_enhancement_enabled:
                return None
            if not config.metadata_tmdb_api_key.strip():
                return None
            query = MetadataContext(vod=vod, source_kind=source_kind).to_query()
            if infer_tmdb_media_type(query) == "movie":
                return None
            tmdb_client = TMDBClient(
                api_key=config.metadata_tmdb_api_key,
                proxy_decider=self._build_proxy_decider(),
            )

            def enhance(session) -> list | None:
                session_vod = getattr(session, "vod", None) or vod
                current_playlist = list(getattr(session, "playlist", []) or [])
                if not current_playlist:
                    return None
                playlist = seed_original_titles([replace(item) for item in current_playlist])
                default_season = _guess_season_number(session_vod)
                season_episode_pairs = _season_episode_pairs(playlist, default_season)
                year = str(getattr(session_vod, "vod_year", "") or "").strip()
                search_title = _strip_search_season_suffix(session_vod.vod_name)
                playlist_search_title = _infer_series_title_from_playlist(playlist)
                playlist_search_year = _infer_series_year_from_playlist(playlist)
                has_season_marker = _title_has_season_marker(session_vod.vod_name)
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
                requested_seasons: set[int] = set()
                for pair in season_episode_pairs:
                    if pair is None:
                        continue
                    requested_seasons.add(pair[0])
                if not requested_seasons:
                    requested_seasons.add(default_season)
                if search_results:
                    preferred_title = playlist_search_title or search_title
                    matched = _select_tmdb_search_match(
                        search_results,
                        preferred_title,
                        session_vod,
                        requested_seasons,
                        tmdb_client,
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
                            finalized = _finalize_episode_playlist(playlist, season_episode_pairs)
                            if finalized is not None:
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
                for candidate_vod in candidate_vods:
                    for candidate in _search_metadata_candidates(candidate_vod, source_kind):
                        updated_playlist = build_provider_episode_playlist(
                            candidate_vod,
                            playlist,
                            candidate,
                            source_priority=METADATA_EPISODE_TITLE_SOURCE_PRIORITY,
                        )
                        if updated_playlist is None:
                            continue
                        updated_pairs = _season_episode_pairs(updated_playlist, default_season)
                        finalized = _finalize_episode_playlist(updated_playlist, updated_pairs)
                        if finalized is None:
                            continue
                        if _count_mapped_episode_titles(finalized) > _count_mapped_episode_titles(playlist):
                            playlist = finalized
                            season_episode_pairs = updated_pairs
                finalized_playlist = _finalize_episode_playlist(playlist, season_episode_pairs)
                if finalized_playlist is not None:
                    return finalized_playlist
                return playlist if playlist_has_title_variants(playlist) else None
            return enhance

        return factory

    def _show_login(self, error_message: str = "") -> LoginWindow:
        logger.info("Show login window has_error=%s", bool(error_message))
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
        *,
        drive_detail_loader,
        offline_download_detail_loader,
        prioritized_plugin_ids: tuple[str, ...] = (),
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
        return loader(**kwargs)

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
            )
        return self._call_plugin_loader(
            self._plugin_manager.load_enabled_plugins,
            drive_detail_loader=drive_detail_loader,
            offline_download_detail_loader=offline_download_detail_loader,
        )

    def _show_main(self):
        self._close_api_client()
        self._api_client = self._build_api_client()
        metadata_hydrator_factory = self._build_metadata_hydrator_factory(self._api_client)
        metadata_scrape_service_factory = self._build_metadata_scrape_service_factory(self._api_client)
        episode_title_enhancer_factory = self._build_episode_title_enhancer_factory(self._api_client)
        setattr(self._plugin_manager, "_metadata_hydrator_factory", metadata_hydrator_factory)
        setattr(self._plugin_manager, "_metadata_scrape_service_factory", metadata_scrape_service_factory)
        setattr(self._plugin_manager, "_episode_title_enhancer_factory", episode_title_enhancer_factory)
        config = self.repo.load_config()
        capabilities = self._load_capabilities(self._api_client)
        drive_detail_loader = getattr(self._api_client, "get_drive_share_detail", None)
        offline_download_detail_loader = getattr(self._api_client, "get_offline_download_detail", None)
        prioritized_plugin_ids = self._startup_prioritized_plugin_ids(config)
        def plugin_loader_task():
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
        telegram_controller = TelegramSearchController(self._api_client)
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
        browse_controller = BrowseController(self._api_client)
        pansou_controller = PansouController(browse_controller) if bool(capabilities.get("pansou")) else None
        history_controller = HistoryController(self._api_client, self._playback_history_repository)
        player_controller = PlayerController(self._api_client)
        self._start_live_background_refresh(live_source_manager, live_epg_service)
        logger.info(
            "Show main window bilibili=%s emby=%s jellyfin=%s feiniu=%s spider_plugins=%s",
            bool(capabilities.get("bilibili")),
            bool(capabilities.get("emby")),
            bool(capabilities.get("jellyfin")),
            bool(capabilities.get("feiniu")),
            0,
        )
        self.main_window = MainWindow(
            browse_controller=browse_controller,
            history_controller=history_controller,
            player_controller=player_controller,
            config=config,
            save_config=_make_proxy_invalidation_wrapper(
                lambda: self.repo.save_config(config), config, self._network
            ),
            douban_controller=douban_controller,
            telegram_controller=telegram_controller,
            bilibili_controller=bilibili_controller,
            live_controller=live_controller,
            live_source_manager=live_source_manager,
            emby_controller=emby_controller,
            jellyfin_controller=jellyfin_controller,
            feiniu_controller=feiniu_controller,
            pansou_controller=pansou_controller,
            spider_plugins=[],
            plugin_loader_task=plugin_loader_task,
            plugin_manager=self._plugin_manager,
            drive_detail_loader=drive_detail_loader,
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
            show_bilibili_tab=bool(capabilities.get("bilibili")),
            show_emby_tab=bool(capabilities.get("emby")),
            show_jellyfin_tab=bool(capabilities.get("jellyfin")),
            show_feiniu_tab=bool(capabilities.get("feiniu")),
            m3u8_ad_filter=self._m3u8_ad_filter,
            playback_parser_service=self._playback_parser_service,
            yt_dlp_service=self._yt_dlp_service,
            metadata_hydrator_factory=metadata_hydrator_factory,
            metadata_scrape_service_factory=metadata_scrape_service_factory,
            episode_title_enhancer_factory=episode_title_enhancer_factory,
            metadata_binding_repository=self._metadata_binding_repository,
        )
        self.main_window.logout_requested.connect(self._handle_logout_requested)
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
                self.repo.save_config(config)
            else:
                if restored is not None:
                    return restored
        return self.main_window

    def _start_live_background_refresh(self, live_source_manager, live_epg_service) -> None:
        def refresh_epg() -> None:
            try:
                config = live_epg_service.load_config()
                if config.epg_url.strip() and is_refresh_stale(getattr(config, "last_refreshed_at", 0)):
                    live_epg_service.refresh()
                    logger.info("Background refresh finished target=epg")
            except Exception:
                logger.exception("Background refresh failed target=epg")
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
                    logger.info("Background refresh finished target=live-source source_id=%s", source.id)
                except Exception:
                    logger.exception("Background refresh failed target=live-source source_id=%s", source.id)
                    continue

        threading.Thread(target=refresh_epg, daemon=True).start()
        threading.Thread(target=refresh_sources, daemon=True).start()

    def _load_capabilities(self, api_client: ApiClient) -> dict[str, bool]:
        default_capabilities = {"bilibili": False, "emby": True, "jellyfin": True, "feiniu": True, "pansou": False}
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
        return capabilities

    def _handle_login_succeeded(self) -> None:
        logger.info("Login succeeded")
        try:
            widget = self._show_main()
        except (ApiError, UnauthorizedError) as exc:
            logger.exception("Failed to initialize after login error=%s", exc)
            widget = self._show_login(error_message=str(exc))
        widget.show()

    def _handle_logout_requested(self) -> None:
        logger.info("Logout requested")
        self.repo.clear_token()
        widget = self._show_login()
        widget.show()

    def close(self) -> None:
        close_filter = getattr(self._m3u8_ad_filter, "close", None)
        if callable(close_filter):
            close_filter()
        self._close_api_client()
        self._network.close()
