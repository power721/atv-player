import os
import httpx
import time
from PySide6.QtGui import QIcon

import atv_player.app as app_module
import atv_player.ui.main_window as main_window_module
from atv_player.api import ApiClient
from atv_player.app import AppCoordinator, decide_start_view
from atv_player.models import AppConfig, DoubanCategory, OpenPlayerRequest, PlayItem, VodItem
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


class FakeTelegramController(FakeDoubanController):
    def build_request(self, vod_id: str):
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="Telegram Movie"),
            playlist=[PlayItem(title="Episode 1", url="", vod_id="ep-1")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )


class FakeEmbyController(FakeDoubanController):
    def __init__(self) -> None:
        self.folder_calls: list[str] = []

    def build_request(self, vod_id: str):
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="Emby Movie"),
            playlist=[PlayItem(title="Episode 1", url="", vod_id="ep-emby-1")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )

    def load_folder_items(self, vod_id: str):
        self.folder_calls.append(vod_id)
        return [VodItem(vod_id="child-1", vod_name="Episode 1", vod_tag="file")], 1


class FakeJellyfinController(FakeDoubanController):
    def __init__(self) -> None:
        self.folder_calls: list[str] = []

    def build_request(self, vod_id: str):
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="Jellyfin Movie"),
            playlist=[PlayItem(title="Episode 1", url="", vod_id="ep-jellyfin-1")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )

    def load_folder_items(self, vod_id: str):
        self.folder_calls.append(vod_id)
        return [VodItem(vod_id="jf-child-1", vod_name="Episode 1", vod_tag="file")], 1


class RecordingDoubanController(FakeDoubanController):
    def __init__(self) -> None:
        self.category_calls = 0
        self.item_calls: list[tuple[str, int]] = []

    def load_categories(self):
        self.category_calls += 1
        return [DoubanCategory(type_id="1", type_name="推荐")]

    def load_items(self, category_id: str, page: int):
        self.item_calls.append((category_id, page))
        return [], 0


class RecordingBrowseController(FakeBrowseController):
    def __init__(self) -> None:
        self.load_calls: list[tuple[str, int, int]] = []

    def load_folder(self, path: str, page: int = 1, size: int = 50):
        self.load_calls.append((path, page, size))
        return [], 0


class RecordingHistoryController(FakeHistoryController):
    def __init__(self) -> None:
        self.load_calls: list[tuple[int, int]] = []

    def load_page(self, page: int, size: int):
        self.load_calls.append((page, size))
        return [], 0


class FakePlayerController:
    def create_session(
        self,
        vod,
        playlist,
        clicked_index: int,
        detail_resolver=None,
        resolved_vod_by_id=None,
        use_local_history=True,
        playback_loader=None,
        playback_progress_reporter=None,
        playback_stopper=None,
    ):
        return {
            "vod": vod,
            "playlist": playlist,
            "clicked_index": clicked_index,
            "detail_resolver": detail_resolver,
            "resolved_vod_by_id": resolved_vod_by_id or {},
            "use_local_history": use_local_history,
            "playback_loader": playback_loader,
            "playback_progress_reporter": playback_progress_reporter,
            "playback_stopper": playback_stopper,
        }


def test_main_window_starts_on_douban_tab(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.currentIndex() == 0
    assert window.nav_tabs.count() == 6
    assert window.nav_tabs.tabText(0) == "豆瓣电影"
    assert window.nav_tabs.tabText(1) == "电报影视"
    assert window.nav_tabs.tabText(2) == "Emby"
    assert window.nav_tabs.tabText(3) == "Jellyfin"
    assert window.nav_tabs.tabText(4) == "文件浏览"
    assert window.nav_tabs.tabText(5) == "播放记录"


def test_main_window_hides_emby_and_jellyfin_tabs_when_disabled(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_emby_tab=False,
        show_jellyfin_tab=False,
    )

    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.count() == 4
    assert window.nav_tabs.tabText(0) == "豆瓣电影"
    assert window.nav_tabs.tabText(1) == "电报影视"
    assert window.nav_tabs.tabText(2) == "文件浏览"
    assert window.nav_tabs.tabText(3) == "播放记录"


def test_main_window_loads_only_default_tab_on_startup_and_lazy_loads_others(qtbot) -> None:
    douban_controller = RecordingDoubanController()
    telegram_controller = RecordingDoubanController()
    browse_controller = RecordingBrowseController()
    history_controller = RecordingHistoryController()
    window = MainWindow(
        douban_controller=douban_controller,
        telegram_controller=telegram_controller,
        emby_controller=RecordingDoubanController(),
        jellyfin_controller=RecordingDoubanController(),
        browse_controller=browse_controller,
        history_controller=history_controller,
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )

    qtbot.addWidget(window)
    window.show()

    qtbot.waitUntil(lambda: douban_controller.category_calls == 1 and douban_controller.item_calls == [("1", 1)])
    assert telegram_controller.category_calls == 0
    assert browse_controller.load_calls == []
    assert history_controller.load_calls == []

    window.nav_tabs.setCurrentWidget(window.telegram_page)
    qtbot.waitUntil(lambda: telegram_controller.category_calls == 1 and telegram_controller.item_calls == [("1", 1)])

    window.nav_tabs.setCurrentWidget(window.browse_page)
    assert browse_controller.load_calls == [("/", 1, 50)]

    window.nav_tabs.setCurrentWidget(window.history_page)
    assert history_controller.load_calls == [(1, 100)]


def test_main_window_only_auto_loads_each_tab_once(qtbot) -> None:
    telegram_controller = RecordingDoubanController()
    browse_controller = RecordingBrowseController()
    history_controller = RecordingHistoryController()
    window = MainWindow(
        douban_controller=RecordingDoubanController(),
        telegram_controller=telegram_controller,
        emby_controller=RecordingDoubanController(),
        jellyfin_controller=RecordingDoubanController(),
        browse_controller=browse_controller,
        history_controller=history_controller,
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )

    qtbot.addWidget(window)
    window.show()

    window.nav_tabs.setCurrentWidget(window.telegram_page)
    qtbot.waitUntil(lambda: telegram_controller.category_calls == 1 and telegram_controller.item_calls == [("1", 1)])
    window.nav_tabs.setCurrentWidget(window.browse_page)
    assert browse_controller.load_calls == [("/", 1, 50)]
    window.nav_tabs.setCurrentWidget(window.history_page)
    assert history_controller.load_calls == [(1, 100)]

    window.nav_tabs.setCurrentWidget(window.douban_page)
    window.nav_tabs.setCurrentWidget(window.telegram_page)
    qtbot.waitUntil(lambda: telegram_controller.category_calls == 1)
    window.nav_tabs.setCurrentWidget(window.browse_page)
    window.nav_tabs.setCurrentWidget(window.history_page)

    assert telegram_controller.item_calls == [("1", 1)]
    assert browse_controller.load_calls == [("/", 1, 50)]
    assert history_controller.load_calls == [(1, 100)]


def test_main_window_logout_button_emits_logout_requested(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
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
        telegram_controller=FakeTelegramController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
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
        telegram_controller=FakeTelegramController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
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


def test_main_window_opens_player_from_telegram_card_signal(qtbot, monkeypatch) -> None:
    controller = FakeTelegramController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=controller,
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))

    window.telegram_page.open_requested.emit("https://pan.quark.cn/s/f518510ef92a")

    assert opened
    assert opened[0].vod.vod_name == "Telegram Movie"
    assert opened[0].source_vod_id == "https://pan.quark.cn/s/f518510ef92a"


def test_main_window_enables_search_controls_only_for_telegram_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)

    assert window.douban_page.keyword_edit.isHidden() is True
    assert window.telegram_page.keyword_edit.isHidden() is False


def test_main_window_enables_search_controls_for_emby_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)

    assert window.emby_page.keyword_edit.isHidden() is False


def test_main_window_opens_player_from_emby_card_signal(qtbot, monkeypatch) -> None:
    controller = FakeEmbyController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        emby_controller=controller,
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))

    window.emby_page.item_open_requested.emit(VodItem(vod_id="1-3281", vod_name="Episode 1", vod_tag="file"))

    assert opened
    assert opened[0].vod.vod_name == "Emby Movie"
    assert opened[0].source_vod_id == "1-3281"


def test_main_window_emby_folder_click_loads_folder_in_current_tab(qtbot, monkeypatch) -> None:
    controller = FakeEmbyController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        emby_controller=controller,
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened = []
    shown = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(
        window.emby_page,
        "show_items",
        lambda items, total, page=1, empty_message="当前分类暂无内容": shown.append((items, total, page, empty_message)),
    )

    window.emby_page.item_open_requested.emit(VodItem(vod_id="folder-1", vod_name="Season 1", vod_tag="folder"))

    assert opened == []
    assert controller.folder_calls == ["folder-1"]
    assert len(shown) == 1
    assert shown[0][1:] == (1, 1, "当前文件夹暂无内容")
    assert shown[0][0][0].vod_id == "child-1"


def test_main_window_enables_search_controls_for_jellyfin_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)

    assert window.jellyfin_page.keyword_edit.isHidden() is False


def test_main_window_opens_player_from_jellyfin_card_signal(qtbot, monkeypatch) -> None:
    controller = FakeJellyfinController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=controller,
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))

    window.jellyfin_page.item_open_requested.emit(VodItem(vod_id="1-4001", vod_name="Episode 1", vod_tag="file"))

    assert opened
    assert opened[0].vod.vod_name == "Jellyfin Movie"
    assert opened[0].source_vod_id == "1-4001"


def test_main_window_jellyfin_folder_click_loads_folder_in_current_tab(qtbot, monkeypatch) -> None:
    controller = FakeJellyfinController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=controller,
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened = []
    shown = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(
        window.jellyfin_page,
        "show_items",
        lambda items, total, page=1, empty_message="当前分类暂无内容": shown.append((items, total, page, empty_message)),
    )

    window.jellyfin_page.item_open_requested.emit(VodItem(vod_id="folder-1", vod_name="Season 1", vod_tag="folder"))

    assert opened == []
    assert controller.folder_calls == ["folder-1"]
    assert len(shown) == 1
    assert shown[0][1:] == (1, 1, "当前文件夹暂无内容")
    assert shown[0][0][0].vod_id == "jf-child-1"


def test_decide_start_view_prefers_login_without_token() -> None:
    assert decide_start_view(AppConfig(token="")) == "login"


def test_decide_start_view_uses_main_window_with_token() -> None:
    assert decide_start_view(AppConfig(token="token-123")) == "main"


def test_build_application_sets_window_icon_and_creates_repo(monkeypatch, tmp_path) -> None:
    class FakeApplication:
        def __init__(self, args) -> None:
            self.args = args
            self.application_name = ""
            self.window_icon = QIcon()

        def setApplicationName(self, name: str) -> None:
            self.application_name = name

        def setWindowIcon(self, icon: QIcon) -> None:
            self.window_icon = icon

    monkeypatch.setattr(app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(app_module.Path, "home", staticmethod(lambda: tmp_path))

    app, repo = app_module.build_application()

    assert app.application_name == "atv-player"
    assert not app.window_icon.isNull()
    assert (tmp_path / ".local" / "share" / "atv-player" / "app.db").exists()
    assert repo.load_config().base_url == "http://127.0.0.1:4567"


def test_build_application_creates_poster_cache_directory(monkeypatch, tmp_path) -> None:
    class FakeApplication:
        def __init__(self, args) -> None:
            self.args = args
            self.application_name = ""
            self.window_icon = QIcon()

        def setApplicationName(self, name: str) -> None:
            self.application_name = name

        def setWindowIcon(self, icon: QIcon) -> None:
            self.window_icon = icon

    monkeypatch.setattr(app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(app_module.Path, "home", staticmethod(lambda: tmp_path))

    app_module.build_application()

    assert (tmp_path / ".cache" / "atv-player" / "posters").is_dir()


def test_build_application_deletes_poster_cache_files_older_than_seven_days(monkeypatch, tmp_path) -> None:
    class FakeApplication:
        def __init__(self, args) -> None:
            self.args = args
            self.application_name = ""
            self.window_icon = QIcon()

        def setApplicationName(self, name: str) -> None:
            self.application_name = name

        def setWindowIcon(self, icon: QIcon) -> None:
            self.window_icon = icon

    monkeypatch.setattr(app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(app_module.Path, "home", staticmethod(lambda: tmp_path))

    cache_dir = tmp_path / ".cache" / "atv-player" / "posters"
    cache_dir.mkdir(parents=True)
    old_file = cache_dir / "old.img"
    new_file = cache_dir / "new.img"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    now = time.time()
    stale_age = now - (8 * 24 * 60 * 60)
    fresh_age = now - (2 * 24 * 60 * 60)
    os.utime(old_file, (stale_age, stale_age))
    os.utime(new_file, (fresh_age, fresh_age))

    app_module.build_application()

    assert old_file.exists() is False
    assert new_file.exists() is True


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


def test_app_coordinator_show_main_uses_capabilities_to_toggle_media_tabs(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.config = AppConfig(
                base_url="http://127.0.0.1:4567",
                username="alice",
                token="auth-123",
                vod_token="vod-123",
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

        def get_capabilities(self) -> dict[str, bool]:
            return {"emby": False, "jellyfin": True, "pansou": False}

    class FakeMainWindow:
        logout_requested = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)

    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(
        coordinator,
        "_build_api_client",
        lambda: FakeApiClient(repo.config.base_url, repo.config.token, repo.config.vod_token),
    )

    window = coordinator._show_main()

    assert isinstance(window, FakeMainWindow)
    assert window.kwargs["show_emby_tab"] is False
    assert window.kwargs["show_jellyfin_tab"] is True


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
    window.nav_tabs.setCurrentWidget(window.browse_page)

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


def test_main_window_restore_last_player_searches_later_folder_pages(qtbot) -> None:
    class RestoreBrowseController:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int, int]] = []
            self.request_calls: list[str] = []

        def load_folder(self, path: str, page: int = 1, size: int = 50):
            self.load_calls.append((path, page, size))
            if page == 1:
                return [VodItem(vod_id="page-1", vod_name="Episode 1", path="/TV/Ep1.mkv", type=2)], 51
            if page == 2:
                return [VodItem(vod_id="page-2-target", vod_name="Episode 51", path="/TV/Ep51.mkv", type=2)], 51
            return [], 51

        def build_request_from_folder_item(self, clicked, items):
            self.request_calls.append(clicked.vod_id)
            return OpenPlayerRequest(
                vod=VodItem(vod_id=clicked.vod_id, vod_name=clicked.vod_name),
                playlist=[PlayItem(title=clicked.vod_name, url="", vod_id=clicked.vod_id)],
                clicked_index=0,
                source_mode="folder",
                source_path="/TV",
                source_vod_id=clicked.vod_id,
                source_clicked_vod_id=clicked.vod_id,
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

    config = AppConfig(
        last_active_window="player",
        last_playback_mode="folder",
        last_playback_path="/TV",
        last_playback_clicked_vod_id="page-2-target",
        last_player_paused=True,
    )
    controller = RestoreBrowseController()
    window = MainWindow(
        browse_controller=controller,
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    controller.load_calls.clear()

    original = main_window_module.PlayerWindow
    main_window_module.PlayerWindow = RecordingPlayerWindow
    try:
        restored = window.restore_last_player()
    finally:
        main_window_module.PlayerWindow = original

    assert restored is window.player_window
    assert controller.load_calls == [("/TV", 1, 50), ("/TV", 2, 50)]
    assert controller.request_calls == ["page-2-target"]
    assert window.player_window.opened[0][1] is True
