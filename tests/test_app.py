import httpx

import atv_player.app as app_module
import atv_player.ui.main_window as main_window_module
from atv_player.api import ApiClient
from atv_player.app import AppCoordinator, decide_start_view
from atv_player.models import AppConfig, OpenPlayerRequest, PlayItem, VodItem
from atv_player.ui.main_window import MainWindow


class RaisingTransport(httpx.BaseTransport):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise self.exc


class FakeBrowseController:
    pass


class FakeHistoryController:
    pass


class FakeDoubanController:
    def load_categories(self):
        return []

    def load_items(self, category_id: str, page: int):
        return [], 0


class FakePlayerController:
    def create_session(
        self,
        vod,
        playlist,
        clicked_index: int,
        detail_resolver=None,
        resolved_vod_by_id=None,
    ):
        return {
            "vod": vod,
            "playlist": playlist,
            "clicked_index": clicked_index,
            "detail_resolver": detail_resolver,
            "resolved_vod_by_id": resolved_vod_by_id or {},
        }


def test_main_window_starts_on_douban_tab(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.currentIndex() == 0
    assert window.nav_tabs.count() == 3
    assert window.nav_tabs.tabText(0) == "豆瓣电影"
    assert window.nav_tabs.tabText(1) == "文件浏览"
    assert window.nav_tabs.tabText(2) == "播放记录"


def test_main_window_logout_button_emits_logout_requested(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )

    qtbot.addWidget(window)

    assert window.logout_button.text() == "退出登录"
    with qtbot.waitSignal(window.logout_requested, timeout=1000):
        window.logout_button.click()


def test_main_window_passes_config_and_save_callback_to_browse_page(qtbot) -> None:
    saved = {"count": 0}
    config = AppConfig()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )

    qtbot.addWidget(window)

    assert window.browse_page.config is config
    assert callable(window.browse_page._save_config)


def test_main_window_switches_to_browse_and_searches_from_douban_signal(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    searched = []
    monkeypatch.setattr(window.browse_page, "search_keyword", lambda keyword: searched.append(keyword))

    window.douban_page.search_requested.emit("霸王别姬")

    assert window.nav_tabs.currentWidget() is window.browse_page
    assert searched == ["霸王别姬"]


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


def test_app_coordinator_falls_back_to_main_when_player_restore_fails(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.config = AppConfig(
                base_url="http://127.0.0.1:4567",
                username="alice",
                token="auth-123",
                vod_token="vod-123",
                last_active_window="player",
                last_playback_mode="detail",
                last_playback_vod_id="vod-1",
            )

        def load_config(self) -> AppConfig:
            return self.config

        def save_config(self, config: AppConfig) -> None:
            self.config = config

        def clear_token(self) -> None:
            self.config.token = ""
            self.config.vod_token = ""

    class FakeApiClient:
        def __init__(self, base_url: str, token: str = "", vod_token: str = "") -> None:
            self.base_url = base_url
            self.token = token
            self.vod_token = vod_token

        def set_vod_token(self, vod_token: str) -> None:
            self.vod_token = vod_token

    class FakeMainWindow:
        logout_requested = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def restore_last_player(self):
            raise RuntimeError("restore failed")

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)

    monkeypatch.setattr(app_module, "ApiClient", FakeApiClient)
    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(coordinator, "_build_api_client", lambda: FakeApiClient("http://127.0.0.1:4567", "auth-123", "vod-123"))

    widget = coordinator._show_main()

    assert isinstance(widget, FakeMainWindow)
    assert repo.config.last_active_window == "main"


def test_app_coordinator_show_main_keeps_window_open_when_initial_browse_times_out(
    qtbot,
    monkeypatch,
) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.config = AppConfig(
                base_url="http://127.0.0.1:4567",
                username="alice",
                token="auth-123",
                vod_token="vod-123",
                last_path="/电影",
            )

        def load_config(self) -> AppConfig:
            return self.config

        def save_config(self, config: AppConfig) -> None:
            self.config = config

        def clear_token(self) -> None:
            self.config.token = ""
            self.config.vod_token = ""

    class TimeoutApiClient(ApiClient):
        def __init__(self, base_url: str, token: str = "", vod_token: str = "") -> None:
            super().__init__(
                base_url,
                token=token,
                vod_token=vod_token,
                transport=RaisingTransport(httpx.ReadTimeout("timed out")),
            )

    coordinator = AppCoordinator(FakeRepo())
    monkeypatch.setattr(app_module, "ApiClient", TimeoutApiClient)

    window = coordinator._show_main()
    qtbot.addWidget(window)

    assert isinstance(window, MainWindow)
    status_widget = window.browse_page.breadcrumb_layout.itemAt(0).widget()
    assert status_widget.text() == "/电影 | 加载文件列表超时"


def test_app_coordinator_logout_clears_tokens_and_shows_login(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.config = AppConfig(
                base_url="http://127.0.0.1:4567",
                username="alice",
                token="auth-123",
                vod_token="vod-123",
            )
            self.clear_token_calls = 0

        def load_config(self) -> AppConfig:
            return self.config

        def save_config(self, config: AppConfig) -> None:
            self.config = config

        def clear_token(self) -> None:
            self.clear_token_calls += 1
            self.config.token = ""
            self.config.vod_token = ""

    class SignalStub:
        def __init__(self) -> None:
            self._callbacks = []

        def connect(self, callback) -> None:
            self._callbacks.append(callback)

        def emit(self) -> None:
            for callback in list(self._callbacks):
                callback()

    class FakeMainWindow:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.logout_requested = SignalStub()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeLoginWindow:
        login_succeeded = SignalStub()

        def __init__(self, controller) -> None:
            self.controller = controller
            self.shown = False

        def show(self) -> None:
            self.shown = True

    class FakeApiClient:
        def __init__(self, base_url: str, token: str = "", vod_token: str = "") -> None:
            self.base_url = base_url
            self.token = token
            self.vod_token = vod_token

        def set_vod_token(self, vod_token: str) -> None:
            self.vod_token = vod_token

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)

    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(app_module, "LoginWindow", FakeLoginWindow)
    monkeypatch.setattr(
        coordinator,
        "_build_api_client",
        lambda: FakeApiClient(repo.config.base_url, repo.config.token, repo.config.vod_token),
    )

    main_window = coordinator._show_main()
    main_window.logout_requested.emit()

    assert repo.clear_token_calls == 1
    assert repo.config.token == ""
    assert repo.config.vod_token == ""
    assert isinstance(coordinator.login_window, FakeLoginWindow)
    assert coordinator.login_window.shown is True
    assert coordinator.main_window is None
    assert main_window.closed is True


def test_main_window_open_player_hides_main_and_updates_last_active_state(qtbot, monkeypatch) -> None:
    created = {}

    class FakePlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            created["config"] = config
            self.opened_session = None
            self.start_paused = None
            self.shown = False

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened_session = session
            self.start_paused = start_paused

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
    assert config.last_player_paused is False
    assert window.player_window.start_paused is False


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


def test_main_window_escape_shows_existing_player_window(qtbot) -> None:
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


def test_main_window_restore_last_player_opens_paused_from_config(qtbot, monkeypatch) -> None:
    class RestoreBrowseController:
        def build_request_from_detail(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="Movie"),
                playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
                clicked_index=0,
                source_mode="detail",
                source_vod_id=vod_id,
            )

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.opened: list[tuple[object, bool]] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    assert window.player_window.opened[0][1] is True


def test_main_window_restore_last_player_rebuilds_folder_request_with_detail_resolver(qtbot) -> None:
    class RestoreBrowseController:
        def __init__(self) -> None:
            self.load_calls: list[str] = []
            self.request_calls: list[str] = []

        def load_folder(self, path: str, page: int = 1, size: int = 50):
            self.load_calls.append(path)
            return [VodItem(vod_id="1$91483$1", vod_name="Episode 1", path="/TV/Ep1.mkv", type=2)], 1

        def build_request_from_folder_item(self, clicked, items):
            self.request_calls.append(clicked.vod_id)
            return OpenPlayerRequest(
                vod=VodItem(vod_id=clicked.vod_id, vod_name="Episode 1"),
                playlist=[PlayItem(title="Episode 1", url="", vod_id=clicked.vod_id)],
                clicked_index=0,
                source_mode="folder",
                source_path="/TV",
                source_vod_id=clicked.vod_id,
                source_clicked_vod_id=clicked.vod_id,
                detail_resolver=lambda item: VodItem(vod_id=item.vod_id, vod_name="Resolved Episode"),
                resolved_vod_by_id={},
            )

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.opened: list[tuple[object, bool]] = []
            self.opened_session = None

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))
            self.opened_session = session

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    config = AppConfig(
        last_active_window="player",
        last_playback_mode="folder",
        last_playback_path="/TV",
        last_playback_clicked_vod_id="1$91483$1",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    import atv_player.ui.main_window as main_window_module_local

    original = main_window_module_local.PlayerWindow
    main_window_module_local.PlayerWindow = RecordingPlayerWindow
    try:
        restored = window.restore_last_player()
    finally:
        main_window_module_local.PlayerWindow = original

    assert restored is window.player_window
    assert window.player_window.opened_session["detail_resolver"] is not None
