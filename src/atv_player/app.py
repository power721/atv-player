from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QWidget

from atv_player.api import ApiClient, ApiError, UnauthorizedError
from atv_player.controllers.browse_controller import BrowseController
from atv_player.controllers.douban_controller import DoubanController
from atv_player.controllers.emby_controller import EmbyController
from atv_player.controllers.jellyfin_controller import JellyfinController
from atv_player.controllers.live_controller import LiveController
from atv_player.controllers.history_controller import HistoryController
from atv_player.controllers.login_controller import LoginController
from atv_player.controllers.player_controller import PlayerController
from atv_player.controllers.telegram_search_controller import TelegramSearchController
from atv_player.models import AppConfig
from atv_player.paths import app_cache_dir, app_data_dir
from atv_player.plugins import SpiderPluginLoader, SpiderPluginManager
from atv_player.plugins.repository import SpiderPluginRepository
from atv_player.storage import SettingsRepository
from atv_player.ui.login_window import LoginWindow
from atv_player.ui.main_window import MainWindow

POSTER_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


class _NullPluginManager:
    def load_enabled_plugins(self) -> list:
        return []


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
    app.setWindowIcon(QIcon(str(_app_icon_path())))
    data_dir = app_data_dir()
    repo = SettingsRepository(data_dir / "app.db")
    purge_stale_poster_cache()
    return app, repo


class AppCoordinator(QObject):
    def __init__(self, repo: SettingsRepository) -> None:
        super().__init__()
        self.repo = repo
        self.login_window: LoginWindow | None = None
        self.main_window: MainWindow | None = None
        self._api_client: ApiClient | None = None
        if hasattr(repo, "database_path"):
            self._plugin_repository = SpiderPluginRepository(repo.database_path)
            cache_dir = repo.database_path.parent / "plugins" / "cache"
            self._plugin_loader = SpiderPluginLoader(cache_dir)
            self._plugin_manager = SpiderPluginManager(self._plugin_repository, self._plugin_loader)
        else:
            self._plugin_repository = None
            self._plugin_loader = None
            self._plugin_manager = _NullPluginManager()

    def start(self) -> QWidget:
        config = self.repo.load_config()
        if decide_start_view(config) == "main":
            self._api_client = ApiClient(
                config.base_url,
                token=config.token,
                vod_token=config.vod_token,
            )
            try:
                self._ensure_vod_token(self._api_client)
            except UnauthorizedError:
                self.repo.clear_token()
                return self._show_login()
            except ApiError as exc:
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
        return vod_token

    def _show_login(self, error_message: str = "") -> LoginWindow:
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
        self._api_client = self._build_api_client()
        config = self.repo.load_config()
        capabilities = self._load_capabilities(self._api_client)
        spider_plugins = self._plugin_manager.load_enabled_plugins()
        douban_controller = DoubanController(self._api_client)
        telegram_controller = TelegramSearchController(self._api_client)
        live_controller = LiveController(self._api_client)
        emby_controller = EmbyController(self._api_client)
        jellyfin_controller = JellyfinController(self._api_client)
        browse_controller = BrowseController(self._api_client)
        history_controller = HistoryController(self._api_client)
        player_controller = PlayerController(self._api_client)
        self.main_window = MainWindow(
            browse_controller=browse_controller,
            history_controller=history_controller,
            player_controller=player_controller,
            config=config,
            save_config=lambda: self.repo.save_config(config),
            douban_controller=douban_controller,
            telegram_controller=telegram_controller,
            live_controller=live_controller,
            emby_controller=emby_controller,
            jellyfin_controller=jellyfin_controller,
            spider_plugins=spider_plugins,
            plugin_manager=self._plugin_manager,
            show_emby_tab=bool(capabilities.get("emby")),
            show_jellyfin_tab=bool(capabilities.get("jellyfin")),
        )
        self.main_window.logout_requested.connect(self._handle_logout_requested)
        if self.login_window is not None:
            self.login_window.close()
            self.login_window = None
        if config.last_active_window == "player":
            try:
                restored = self.main_window.restore_last_player()
            except Exception:
                config.last_active_window = "main"
                self.repo.save_config(config)
            else:
                if restored is not None:
                    return restored
        return self.main_window

    def _load_capabilities(self, api_client: ApiClient) -> dict[str, bool]:
        default_capabilities = {"emby": True, "jellyfin": True}
        get_capabilities = getattr(api_client, "get_capabilities", None)
        if not callable(get_capabilities):
            return default_capabilities
        try:
            response = get_capabilities()
        except (ApiError, UnauthorizedError):
            return default_capabilities
        if not isinstance(response, dict):
            return default_capabilities
        capabilities = dict(default_capabilities)
        capabilities["emby"] = bool(response.get("emby", capabilities["emby"]))
        capabilities["jellyfin"] = bool(response.get("jellyfin", capabilities["jellyfin"]))
        return capabilities

    def _handle_login_succeeded(self) -> None:
        widget = self._show_main()
        widget.show()

    def _handle_logout_requested(self) -> None:
        self.repo.clear_token()
        widget = self._show_login()
        widget.show()
