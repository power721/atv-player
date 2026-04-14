import atv_player.app as app_module
import atv_player.ui.main_window as main_window_module
from atv_player.app import AppCoordinator, decide_start_view
from atv_player.models import AppConfig, OpenPlayerRequest, PlayItem, VodItem
from atv_player.ui.main_window import MainWindow


class FakeBrowseController:
    pass


class FakeHistoryController:
    pass


class FakePlayerController:
    def create_session(self, vod, playlist, clicked_index: int):
        return {"vod": vod, "playlist": playlist, "clicked_index": clicked_index}


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
    assert window.nav_tabs.count() == 2
    assert window.nav_tabs.tabText(0) == "浏览"
    assert window.nav_tabs.tabText(1) == "播放记录"


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


def test_main_window_open_player_hides_main_and_updates_last_active_state(qtbot, monkeypatch) -> None:
    created = {}

    class FakePlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            created["config"] = config
            self.opened_session = None
            self.shown = False

        def open_session(self, session) -> None:
            self.opened_session = session

        def show(self) -> None:
            self.shown = True

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    monkeypatch.setattr(main_window_module, "PlayerWindow", FakePlayerWindow)
    config = AppConfig()
    window = MainWindow(
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )

    qtbot.addWidget(window)
    window.show()
    request = OpenPlayerRequest(
        vod=VodItem(vod_id="vod-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
        clicked_index=0,
        source_mode="detail",
        source_vod_id="vod-1",
    )

    window.open_player(request)

    assert window.isHidden() is True
    assert config.last_active_window == "player"
    assert config.last_playback_mode == "detail"
    assert config.last_playback_vod_id == "vod-1"


def test_main_window_ctrl_p_shows_existing_player_window(qtbot) -> None:
    class ExistingPlayerWindow:
        def __init__(self) -> None:
            self.session = object()
            self.show_calls = 0
            self.raise_calls = 0
            self.activate_calls = 0

        def show(self) -> None:
            self.show_calls += 1

        def raise_(self) -> None:
            self.raise_calls += 1

        def activateWindow(self) -> None:
            self.activate_calls += 1

    config = AppConfig(last_active_window="main")
    window = MainWindow(
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.player_window = ExistingPlayerWindow()
    window.show()

    window.show_or_restore_player()

    assert window.isHidden() is True
    assert config.last_active_window == "player"
    assert window.player_window.show_calls == 1
    assert window.player_window.raise_calls == 1
    assert window.player_window.activate_calls == 1


def test_main_window_ctrl_p_restores_last_player_when_missing(qtbot, monkeypatch) -> None:
    restored = {"called": 0}
    config = AppConfig(last_active_window="main", last_playback_mode="detail", last_playback_vod_id="vod-1")
    window = MainWindow(
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    def fake_restore():
        restored["called"] += 1
        return object()

    monkeypatch.setattr(window, "restore_last_player", fake_restore)

    window.show_or_restore_player()

    assert restored["called"] == 1
