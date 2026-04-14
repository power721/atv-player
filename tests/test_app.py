import atv_player.app as app_module
from atv_player.app import AppCoordinator, decide_start_view
from atv_player.models import AppConfig
from atv_player.ui.main_window import MainWindow


class FakeBrowseController:
    pass


class FakeHistoryController:
    pass


class FakePlayerController:
    pass


def test_main_window_starts_on_browse_tab(qtbot) -> None:
    window = MainWindow(
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.currentIndex() == 0


def test_decide_start_view_prefers_login_without_token() -> None:
    assert decide_start_view(AppConfig(token="")) == "login"


def test_decide_start_view_uses_main_window_with_token() -> None:
    assert decide_start_view(AppConfig(token="token-123")) == "main"


def test_app_coordinator_start_does_not_require_vod_root_probe(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.config = AppConfig(
                base_url="http://127.0.0.1:4567",
                username="alice",
                token="auth-123",
                vod_token="",
                last_path="/",
            )

        def load_config(self) -> AppConfig:
            return self.config

        def save_config(self, config: AppConfig) -> None:
            self.config = config

        def clear_token(self) -> None:
            self.config.token = ""
            self.config.vod_token = ""

    class FakeApiClient:
        list_vod_calls = 0

        def __init__(self, base_url: str, token: str = "", vod_token: str = "") -> None:
            self.base_url = base_url
            self.token = token
            self.vod_token = vod_token

        def set_vod_token(self, vod_token: str) -> None:
            self.vod_token = vod_token

        def fetch_vod_token(self) -> str:
            self.vod_token = "vod-123"
            return self.vod_token

        def list_vod(self, path_id: str, page: int, size: int) -> dict:
            type(self).list_vod_calls += 1
            raise AssertionError("start() should not probe /vod root to validate login")

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)

    monkeypatch.setattr(app_module, "ApiClient", FakeApiClient)
    monkeypatch.setattr(coordinator, "_show_main", lambda: "main-widget")
    monkeypatch.setattr(coordinator, "_show_login", lambda: "login-widget")

    widget = coordinator.start()

    assert widget == "main-widget"
    assert repo.config.vod_token == "vod-123"
    assert FakeApiClient.list_vod_calls == 0
