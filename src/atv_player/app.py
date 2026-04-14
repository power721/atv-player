from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication, QWidget

from atv_player.api import ApiClient, UnauthorizedError
from atv_player.controllers.browse_controller import BrowseController, encode_vod_path
from atv_player.controllers.history_controller import HistoryController
from atv_player.controllers.login_controller import LoginController
from atv_player.controllers.player_controller import PlayerController
from atv_player.models import AppConfig
from atv_player.storage import SettingsRepository
from atv_player.ui.login_window import LoginWindow
from atv_player.ui.main_window import MainWindow


def decide_start_view(config: AppConfig) -> str:
    return "main" if config.token else "login"


def build_application() -> tuple[QApplication, SettingsRepository]:
    app = QApplication([])
    app.setApplicationName("atv-player")
    data_dir = Path.home() / ".local" / "share" / "atv-player"
    repo = SettingsRepository(data_dir / "app.db")
    return app, repo


class AppCoordinator(QObject):
    def __init__(self, repo: SettingsRepository) -> None:
        super().__init__()
        self.repo = repo
        self.login_window: LoginWindow | None = None
        self.main_window: MainWindow | None = None
        self._api_client: ApiClient | None = None

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
                self._api_client.list_vod(encode_vod_path("/"), page=1, size=1)
            except UnauthorizedError:
                self.repo.clear_token()
                return self._show_login()
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

    def _show_login(self) -> LoginWindow:
        login_controller = LoginController(
            self.repo,
            lambda base_url: ApiClient(base_url),
        )
        self.login_window = LoginWindow(login_controller)
        self.login_window.login_succeeded.connect(self._handle_login_succeeded)
        if self.main_window is not None:
            self.main_window.close()
            self.main_window = None
        return self.login_window

    def _show_main(self) -> MainWindow:
        self._api_client = self._build_api_client()
        config = self.repo.load_config()
        browse_controller = BrowseController(self._api_client)
        history_controller = HistoryController(self._api_client)
        player_controller = PlayerController(self._api_client)
        self.main_window = MainWindow(
            browse_controller=browse_controller,
            history_controller=history_controller,
            player_controller=player_controller,
            config=config,
        )
        self.main_window.logout_requested.connect(self._handle_logout_requested)
        if self.login_window is not None:
            self.login_window.close()
            self.login_window = None
        return self.main_window

    def _handle_login_succeeded(self) -> None:
        widget = self._show_main()
        widget.show()

    def _handle_logout_requested(self) -> None:
        self.repo.clear_token()
        widget = self._show_login()
        widget.show()
