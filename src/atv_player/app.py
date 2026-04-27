from __future__ import annotations

import threading
import time
import logging
from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication, QWidget

from atv_player.api import ApiClient, ApiError, UnauthorizedError
from atv_player.custom_live_service import CustomLiveService
from atv_player.controllers.browse_controller import BrowseController
from atv_player.controllers.douban_controller import DoubanController
from atv_player.controllers.emby_controller import EmbyController
from atv_player.controllers.jellyfin_controller import JellyfinController
from atv_player.controllers.live_controller import LiveController
from atv_player.controllers.history_controller import HistoryController
from atv_player.controllers.login_controller import LoginController
from atv_player.controllers.player_controller import PlayerController
from atv_player.controllers.telegram_search_controller import TelegramSearchController
from atv_player.live_epg_repository import LiveEpgRepository
from atv_player.live_epg_service import LiveEpgService
from atv_player.local_playback_history import LocalPlaybackHistoryRepository
from atv_player.models import AppConfig, LiveEpgConfig
from atv_player.paths import app_cache_dir, app_data_dir
from atv_player.live_source_repository import LiveSourceRepository
from atv_player.plugins import SpiderPluginLoader, SpiderPluginManager
from atv_player.plugins.repository import SpiderPluginRepository
from atv_player.playback_parsers import BuiltInPlaybackParserService
from atv_player.player.m3u8_ad_filter import M3U8AdFilter
from atv_player.storage import SettingsRepository
from atv_player.time_utils import is_refresh_stale
from atv_player.ui.login_window import LoginWindow
from atv_player.ui.main_window import MainWindow
from atv_player.ui.icon_cache import load_icon

POSTER_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
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


def decide_start_view(config: AppConfig) -> str:
    return "main" if config.token else "login"


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


def build_application() -> tuple[QApplication, SettingsRepository]:
    app = QApplication([])
    app.setApplicationName("atv-player")
    app.setWindowIcon(load_icon(_app_icon_path()))
    data_dir = app_data_dir()
    repo = SettingsRepository(data_dir / "app.db")
    purge_stale_poster_cache()
    logger.info("Application initialized data_dir=%s", data_dir)
    return app, repo


class AppCoordinator(QObject):
    def __init__(self, repo: SettingsRepository) -> None:
        super().__init__()
        self.repo = repo
        self.login_window: LoginWindow | None = None
        self.main_window: MainWindow | None = None
        self._api_client: ApiClient | None = None
        self._m3u8_ad_filter = M3U8AdFilter()
        self._playback_parser_service = BuiltInPlaybackParserService()
        if hasattr(repo, "database_path"):
            self._live_source_repository = LiveSourceRepository(repo.database_path)
            self._live_epg_repository = LiveEpgRepository(repo.database_path)
            self._plugin_repository = SpiderPluginRepository(repo.database_path)
            self._playback_history_repository = LocalPlaybackHistoryRepository(repo.database_path)
            cache_dir = repo.database_path.parent / "plugins" / "cache"
            self._plugin_loader = SpiderPluginLoader(cache_dir)
            self._plugin_manager = SpiderPluginManager(
                self._plugin_repository,
                self._plugin_loader,
                self._playback_history_repository,
            )
            setattr(self._plugin_manager, "_playback_parser_service", self._playback_parser_service)
            setattr(
                self._plugin_manager,
                "_preferred_parse_key_loader",
                lambda: self.repo.load_config().preferred_parse_key,
            )
        else:
            self._live_source_repository = _NullLiveSourceRepository()
            self._live_epg_repository = None
            self._plugin_repository = None
            self._playback_history_repository = None
            self._plugin_loader = None
            self._plugin_manager = _NullPluginManager()

    def _close_api_client(self) -> None:
        if self._api_client is None:
            return
        close_client = getattr(self._api_client, "close", None)
        if callable(close_client):
            close_client()
        self._api_client = None

    def start(self) -> QWidget:
        config = self.repo.load_config()
        logger.info("App start view=%s", decide_start_view(config))
        if decide_start_view(config) == "main":
            self._api_client = ApiClient(
                config.base_url,
                token=config.token,
                vod_token=config.vod_token,
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

    def _show_login(self, error_message: str = "") -> LoginWindow:
        logger.info("Show login window has_error=%s", bool(error_message))
        self._close_api_client()
        login_controller = LoginController(
            self.repo,
            lambda base_url: ApiClient(base_url),
        )
        self.login_window = LoginWindow(login_controller)
        if error_message and hasattr(self.login_window, "set_error_message"):
            self.login_window.set_error_message(error_message)
        self.login_window.login_succeeded.connect(self._handle_login_succeeded)
        if self.main_window is not None:
            self.main_window.close()
            self.main_window = None
        return self.login_window

    def _show_main(self):
        self._close_api_client()
        self._api_client = self._build_api_client()
        config = self.repo.load_config()
        capabilities = self._load_capabilities(self._api_client)
        drive_detail_loader = getattr(self._api_client, "get_drive_share_detail", None)
        try:
            spider_plugins = self._plugin_manager.load_enabled_plugins(
                drive_detail_loader=drive_detail_loader,
            )
        except TypeError as exc:
            if "drive_detail_loader" not in str(exc):
                raise
            spider_plugins = self._plugin_manager.load_enabled_plugins()
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
        browse_controller = BrowseController(self._api_client)
        history_controller = HistoryController(self._api_client, self._playback_history_repository)
        player_controller = PlayerController(self._api_client)
        self._start_live_background_refresh(live_source_manager, live_epg_service)
        logger.info(
            "Show main window emby=%s jellyfin=%s spider_plugins=%s",
            bool(capabilities.get("emby")),
            bool(capabilities.get("jellyfin")),
            len(spider_plugins),
        )
        self.main_window = MainWindow(
            browse_controller=browse_controller,
            history_controller=history_controller,
            player_controller=player_controller,
            config=config,
            save_config=lambda: self.repo.save_config(config),
            douban_controller=douban_controller,
            telegram_controller=telegram_controller,
            live_controller=live_controller,
            live_source_manager=live_source_manager,
            emby_controller=emby_controller,
            jellyfin_controller=jellyfin_controller,
            spider_plugins=spider_plugins,
            plugin_manager=self._plugin_manager,
            drive_detail_loader=drive_detail_loader,
            show_emby_tab=bool(capabilities.get("emby")),
            show_jellyfin_tab=bool(capabilities.get("jellyfin")),
            m3u8_ad_filter=self._m3u8_ad_filter,
            playback_parser_service=self._playback_parser_service,
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
        default_capabilities = {"emby": True, "jellyfin": True}
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
        capabilities["emby"] = bool(response.get("emby", capabilities["emby"]))
        capabilities["jellyfin"] = bool(response.get("jellyfin", capabilities["jellyfin"]))
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
