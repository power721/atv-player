import os
import httpx
import threading
import time
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QTableWidget

import atv_player.app as app_module
import atv_player.ui.main_window as main_window_module
from atv_player.api import ApiClient
from atv_player.app import AppCoordinator, decide_start_view
from atv_player.models import AppConfig, DoubanCategory, HistoryRecord, OpenPlayerRequest, PlayItem, VodItem
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
    def __init__(self) -> None:
        self.category_calls = 0
        self.item_calls: list[tuple[str, int]] = []
        self.categories = [DoubanCategory(type_id="suggestion", type_name="推荐")]

    def load_categories(self):
        self.category_calls += 1
        return self.categories

    def load_items(self, category_id: str, page: int):
        self.item_calls.append((category_id, page))
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


class FakeLiveController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self.folder_calls: list[str] = []

    def build_request(self, vod_id: str):
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="Live Room"),
            playlist=[PlayItem(title="线路 1", url="https://stream.example/live.m3u8", vod_id="line-1")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
            use_local_history=False,
        )

    def load_folder_items(self, vod_id: str):
        self.folder_calls.append(vod_id)
        return [VodItem(vod_id="child-live-1", vod_name="直播间", vod_tag="file")], 1


class FakeLiveSourceManager:
    def list_sources(self):
        return []


class FakePluginManager:
    def load_enabled_plugins(self):
        return []


class FakeEmbyController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
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
        super().__init__()
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


class AsyncRequestController(FakeDoubanController):
    def __init__(self, request_factory) -> None:
        super().__init__()
        self.calls: list[str] = []
        self._main_thread_id = threading.get_ident()
        self._request_factory = request_factory
        self._events_by_vod_id: dict[str, list[threading.Event]] = {}
        self._results_by_vod_id: dict[str, list[OpenPlayerRequest]] = {}
        self._errors_by_vod_id: dict[str, list[Exception]] = {}

    def build_request(self, vod_id: str):
        self.calls.append(vod_id)
        assert threading.get_ident() != self._main_thread_id
        event = threading.Event()
        self._events_by_vod_id.setdefault(vod_id, []).append(event)
        assert event.wait(timeout=5), f"request for {vod_id!r} was never released"
        errors = self._errors_by_vod_id.get(vod_id)
        if errors:
            raise errors.pop(0)
        results = self._results_by_vod_id.get(vod_id)
        if results:
            return results.pop(0)
        return self._request_factory(vod_id)

    def finish_request(
        self,
        vod_id: str,
        *,
        request: OpenPlayerRequest | None = None,
        exc: Exception | None = None,
    ) -> None:
        if request is not None:
            self._results_by_vod_id.setdefault(vod_id, []).append(request)
        if exc is not None:
            self._errors_by_vod_id.setdefault(vod_id, []).append(exc)
        self._events_by_vod_id[vod_id].pop(0).set()


class AsyncFolderController(AsyncRequestController):
    def __init__(self, request_factory) -> None:
        super().__init__(request_factory)
        self.folder_calls: list[str] = []
        self._folder_events_by_vod_id: dict[str, list[threading.Event]] = {}
        self._folder_results_by_vod_id: dict[str, list[tuple[list[VodItem], int]]] = {}
        self._folder_errors_by_vod_id: dict[str, list[Exception]] = {}

    def load_folder_items(self, vod_id: str):
        self.folder_calls.append(vod_id)
        assert threading.get_ident() != self._main_thread_id
        event = threading.Event()
        self._folder_events_by_vod_id.setdefault(vod_id, []).append(event)
        assert event.wait(timeout=5), f"folder load for {vod_id!r} was never released"
        errors = self._folder_errors_by_vod_id.get(vod_id)
        if errors:
            raise errors.pop(0)
        results = self._folder_results_by_vod_id.get(vod_id)
        if results:
            return results.pop(0)
        return [], 0

    def finish_folder(
        self,
        vod_id: str,
        *,
        items: list[VodItem],
        total: int,
        exc: Exception | None = None,
    ) -> None:
        self._folder_results_by_vod_id.setdefault(vod_id, []).append((items, total))
        if exc is not None:
            self._folder_errors_by_vod_id.setdefault(vod_id, []).append(exc)
        self._folder_events_by_vod_id[vod_id].pop(0).set()


class AsyncHistoryBrowseController(FakeBrowseController):
    def __init__(self) -> None:
        self.detail_calls: list[str] = []
        self._main_thread_id = threading.get_ident()
        self._events_by_vod_id: dict[str, list[threading.Event]] = {}
        self._results_by_vod_id: dict[str, list[OpenPlayerRequest]] = {}
        self._errors_by_vod_id: dict[str, list[Exception]] = {}

    def build_request_from_detail(self, vod_id: str):
        self.detail_calls.append(vod_id)
        assert threading.get_ident() != self._main_thread_id
        event = threading.Event()
        self._events_by_vod_id.setdefault(vod_id, []).append(event)
        assert event.wait(timeout=5), f"history detail request for {vod_id!r} was never released"
        errors = self._errors_by_vod_id.get(vod_id)
        if errors:
            raise errors.pop(0)
        results = self._results_by_vod_id.get(vod_id)
        if results:
            return results.pop(0)
        return _make_history_request(vod_id)

    def finish_detail(
        self,
        vod_id: str,
        *,
        request: OpenPlayerRequest | None = None,
        exc: Exception | None = None,
    ) -> None:
        if request is not None:
            self._results_by_vod_id.setdefault(vod_id, []).append(request)
        if exc is not None:
            self._errors_by_vod_id.setdefault(vod_id, []).append(exc)
        self._events_by_vod_id[vod_id].pop(0).set()


class AsyncRestoreFolderBrowseController(FakeBrowseController):
    def __init__(self) -> None:
        self.load_calls: list[tuple[str, int, int]] = []
        self.request_calls: list[str] = []
        self._main_thread_id = threading.get_ident()
        self._load_events_by_key: dict[tuple[str, int, int], list[threading.Event]] = {}
        self._load_results_by_key: dict[tuple[str, int, int], list[tuple[list[VodItem], int]]] = {}

    def load_folder(self, path: str, page: int = 1, size: int = 50):
        self.load_calls.append((path, page, size))
        assert threading.get_ident() != self._main_thread_id
        key = (path, page, size)
        event = threading.Event()
        self._load_events_by_key.setdefault(key, []).append(event)
        assert event.wait(timeout=5), f"restore folder load for {key!r} was never released"
        results = self._load_results_by_key.get(key)
        if results:
            return results.pop(0)
        return [], 0

    def build_request_from_folder_item(self, clicked, items):
        self.request_calls.append(clicked.vod_id)
        assert threading.get_ident() != self._main_thread_id
        return OpenPlayerRequest(
            vod=VodItem(vod_id=clicked.vod_id, vod_name=clicked.vod_name),
            playlist=[PlayItem(title=clicked.vod_name, url="", vod_id=clicked.vod_id)],
            clicked_index=0,
            source_mode="folder",
            source_path="/TV",
            source_vod_id=clicked.vod_id,
            source_clicked_vod_id=clicked.vod_id,
        )

    def finish_load(
        self,
        path: str,
        *,
        page: int = 1,
        size: int = 50,
        items: list[VodItem],
        total: int,
    ) -> None:
        key = (path, page, size)
        self._load_results_by_key.setdefault(key, []).append((items, total))
        self._load_events_by_key[key].pop(0).set()


class AsyncPluginController(AsyncRequestController):
    def load_categories(self):
        return []

    def load_items(self, category_id: str, page: int):
        return [], 0


def _make_telegram_request(vod_id: str, vod_name: str = "Telegram Movie") -> OpenPlayerRequest:
    return OpenPlayerRequest(
        vod=VodItem(vod_id=vod_id, vod_name=vod_name),
        playlist=[PlayItem(title="Episode 1", url="", vod_id="ep-1")],
        clicked_index=0,
        source_mode="detail",
        source_vod_id=vod_id,
    )


def _make_live_request(vod_id: str, vod_name: str = "Live Room") -> OpenPlayerRequest:
    return OpenPlayerRequest(
        vod=VodItem(vod_id=vod_id, vod_name=vod_name),
        playlist=[PlayItem(title="线路 1", url="https://stream.example/live.m3u8", vod_id="line-1")],
        clicked_index=0,
        source_mode="detail",
        source_vod_id=vod_id,
        use_local_history=False,
    )


def _make_history_request(vod_id: str, vod_name: str = "History Movie") -> OpenPlayerRequest:
    return OpenPlayerRequest(
        vod=VodItem(vod_id=vod_id, vod_name=vod_name),
        playlist=[PlayItem(title="Episode 1", url="", vod_id="ep-history-1")],
        clicked_index=0,
        source_mode="detail",
        source_vod_id=vod_id,
    )


def _wait_for_request_call(qtbot, controller: AsyncRequestController, vod_id: str) -> None:
    qtbot.waitUntil(lambda: vod_id in controller.calls, timeout=1000)


def _wait_for_folder_call(qtbot, controller: AsyncFolderController, vod_id: str) -> None:
    qtbot.waitUntil(lambda: vod_id in controller.folder_calls, timeout=1000)


def _wait_for_history_detail_call(qtbot, controller: AsyncHistoryBrowseController, vod_id: str) -> None:
    qtbot.waitUntil(lambda: vod_id in controller.detail_calls, timeout=1000)


def _wait_for_restore_folder_call(
    qtbot,
    controller: AsyncRestoreFolderBrowseController,
    path: str,
    page: int = 1,
    size: int = 50,
) -> None:
    qtbot.waitUntil(lambda: (path, page, size) in controller.load_calls, timeout=1000)


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
        playlists=None,
        playlist_index: int = 0,
        detail_resolver=None,
        resolved_vod_by_id=None,
        use_local_history=True,
        restore_history=False,
        playback_loader=None,
        playback_progress_reporter=None,
        playback_stopper=None,
        playback_history_loader=None,
        playback_history_saver=None,
    ):
        return {
            "vod": vod,
            "playlist": playlist,
            "clicked_index": clicked_index,
            "playlists": playlists,
            "playlist_index": playlist_index,
            "detail_resolver": detail_resolver,
            "resolved_vod_by_id": resolved_vod_by_id or {},
            "use_local_history": use_local_history,
            "restore_history": restore_history,
            "playback_loader": playback_loader,
            "playback_progress_reporter": playback_progress_reporter,
            "playback_stopper": playback_stopper,
            "playback_history_loader": playback_history_loader,
            "playback_history_saver": playback_history_saver,
        }


def test_main_window_starts_on_douban_tab(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
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
    assert window.nav_tabs.count() == 7
    assert window.nav_tabs.tabText(0) == "豆瓣电影"
    assert window.nav_tabs.tabText(1) == "电报影视"
    assert window.nav_tabs.tabText(2) == "网络直播"
    assert window.nav_tabs.tabText(3) == "Emby"
    assert window.nav_tabs.tabText(4) == "Jellyfin"
    assert window.nav_tabs.tabText(5) == "文件浏览"
    assert window.nav_tabs.tabText(6) == "播放记录"


def test_app_coordinator_passes_loaded_spider_plugins_into_main_window(qtbot, monkeypatch, tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    repo.save_config(
        AppConfig(
            base_url="http://127.0.0.1:4567",
            username="alice",
            token="token-123",
            vod_token="vod-123",
        )
    )

    loaded_plugins = [
        {"title": "红果短剧", "controller": object(), "search_enabled": True},
    ]
    captured_loader = {"value": None}

    class FakePluginManager:
        def load_enabled_plugins(self, drive_detail_loader=None):
            captured_loader["value"] = drive_detail_loader
            return loaded_plugins

    def api_factory(*args, **kwargs):
        return ApiClient(
            "http://127.0.0.1:4567",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"token": "vod-123"})),
        )

    monkeypatch.setattr(app_module, "ApiClient", api_factory)
    monkeypatch.setattr(app_module, "SpiderPluginManager", lambda repository, loader: FakePluginManager())
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())

    coordinator = AppCoordinator(repo)
    widget = coordinator._show_main()
    qtbot.addWidget(widget)

    assert widget.nav_tabs.tabText(5) == "红果短剧"
    assert callable(captured_loader["value"])


def test_http_text_client_follows_redirects_for_live_source_text_requests() -> None:
    class FakeApiClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_text(self, url: str) -> str:
            self.calls.append(url)
            return "#EXTM3U"

    api_client = FakeApiClient()
    client = app_module._HttpTextClient(api_client)

    text = client.get_text("https://example.com/live.m3u")

    assert text == "#EXTM3U"
    assert api_client.calls == ["https://example.com/live.m3u"]


def test_http_text_client_follows_redirects_for_live_source_byte_requests() -> None:
    class FakeApiClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_bytes(self, url: str) -> bytes:
            self.calls.append(url)
            return b"\x1f\x8bpayload"

    api_client = FakeApiClient()
    client = app_module._HttpTextClient(api_client)

    payload = client.get_bytes("https://example.com/e9.xml.gz")

    assert payload == b"\x1f\x8bpayload"
    assert api_client.calls == ["https://example.com/e9.xml.gz"]


def test_main_window_hides_emby_and_jellyfin_tabs_when_disabled(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
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

    assert window.nav_tabs.count() == 5
    assert window.nav_tabs.tabText(0) == "豆瓣电影"
    assert window.nav_tabs.tabText(1) == "电报影视"
    assert window.nav_tabs.tabText(2) == "网络直播"
    assert window.nav_tabs.tabText(3) == "文件浏览"
    assert window.nav_tabs.tabText(4) == "播放记录"


def test_main_window_loads_only_default_tab_on_startup_and_lazy_loads_others(qtbot) -> None:
    douban_controller = RecordingDoubanController()
    telegram_controller = RecordingDoubanController()
    live_controller = RecordingDoubanController()
    browse_controller = RecordingBrowseController()
    history_controller = RecordingHistoryController()
    window = MainWindow(
        douban_controller=douban_controller,
        telegram_controller=telegram_controller,
        live_controller=live_controller,
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
    assert live_controller.category_calls == 0
    assert browse_controller.load_calls == []
    assert history_controller.load_calls == []

    window.nav_tabs.setCurrentWidget(window.telegram_page)
    qtbot.waitUntil(lambda: telegram_controller.category_calls == 1 and telegram_controller.item_calls == [("1", 1)])

    window.nav_tabs.setCurrentWidget(window.live_page)
    qtbot.waitUntil(lambda: live_controller.category_calls == 1 and live_controller.item_calls == [("1", 1)])

    window.nav_tabs.setCurrentWidget(window.browse_page)
    assert browse_controller.load_calls == [("/", 1, 50)]

    window.nav_tabs.setCurrentWidget(window.history_page)
    assert history_controller.load_calls == [(1, 100)]


def test_main_window_only_auto_loads_each_tab_once(qtbot) -> None:
    telegram_controller = RecordingDoubanController()
    live_controller = RecordingDoubanController()
    browse_controller = RecordingBrowseController()
    history_controller = RecordingHistoryController()
    window = MainWindow(
        douban_controller=RecordingDoubanController(),
        telegram_controller=telegram_controller,
        live_controller=live_controller,
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
    window.nav_tabs.setCurrentWidget(window.live_page)
    qtbot.waitUntil(lambda: live_controller.category_calls == 1 and live_controller.item_calls == [("1", 1)])
    window.nav_tabs.setCurrentWidget(window.browse_page)
    assert browse_controller.load_calls == [("/", 1, 50)]
    window.nav_tabs.setCurrentWidget(window.history_page)
    assert history_controller.load_calls == [(1, 100)]

    window.nav_tabs.setCurrentWidget(window.douban_page)
    window.nav_tabs.setCurrentWidget(window.telegram_page)
    qtbot.waitUntil(lambda: telegram_controller.category_calls == 1)
    window.nav_tabs.setCurrentWidget(window.live_page)
    qtbot.waitUntil(lambda: live_controller.category_calls == 1)
    window.nav_tabs.setCurrentWidget(window.browse_page)
    window.nav_tabs.setCurrentWidget(window.history_page)

    assert telegram_controller.item_calls == [("1", 1)]
    assert live_controller.item_calls == [("1", 1)]
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


def test_main_window_opens_live_source_manager_dialog_and_reloads_live_categories(qtbot, monkeypatch) -> None:
    controller = FakeLiveController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=controller,
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        live_source_manager=FakeLiveSourceManager(),
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    window.show()
    reloaded = []
    monkeypatch.setattr(window.live_page, "reload_categories", lambda: reloaded.append(True))

    class FakeDialog:
        def __init__(self, manager, parent=None) -> None:
            self.manager = manager

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "LiveSourceManagerDialog", FakeDialog)

    window._open_live_source_manager()

    assert reloaded == [True]


def test_main_window_opening_live_source_manager_closes_shortcut_help_dialog(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        live_source_manager=FakeLiveSourceManager(),
        plugin_manager=FakePluginManager(),
    )
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    window.setFocus()

    class FakeDialog:
        def __init__(self, manager, parent=None) -> None:
            self.manager = manager

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "LiveSourceManagerDialog", FakeDialog)

    QTest.keyClick(window, Qt.Key.Key_F1)
    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)

    window._open_live_source_manager()

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 0, timeout=1000)
    assert window.help_dialog is None


def test_main_window_opening_plugin_manager_closes_shortcut_help_dialog(qtbot, monkeypatch) -> None:
    plugin_manager = FakePluginManager()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        live_source_manager=FakeLiveSourceManager(),
        plugin_manager=plugin_manager,
    )
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    window.setFocus()

    class FakeDialog:
        def __init__(self, manager, parent=None) -> None:
            self.manager = manager

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "PluginManagerDialog", FakeDialog)

    QTest.keyClick(window, Qt.Key.Key_F1)
    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)

    window._open_plugin_manager()

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 0, timeout=1000)
    assert window.help_dialog is None


def test_main_window_reloads_plugins_with_drive_detail_loader_after_plugin_manager_closes(qtbot, monkeypatch) -> None:
    captured_loaders: list[object | None] = []
    drive_detail_loader = object()

    class DriveAwarePluginManager:
        def load_enabled_plugins(self, drive_detail_loader=None):
            captured_loaders.append(drive_detail_loader)
            return []

    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        live_source_manager=FakeLiveSourceManager(),
        plugin_manager=DriveAwarePluginManager(),
        drive_detail_loader=drive_detail_loader,
    )
    qtbot.addWidget(window)

    class FakeDialog:
        def __init__(self, manager, parent=None) -> None:
            self.manager = manager

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "PluginManagerDialog", FakeDialog)

    window._open_plugin_manager()

    assert captured_loaders == [drive_detail_loader]


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

    opened: list[tuple[OpenPlayerRequest, bool]] = []
    monkeypatch.setattr(
        window,
        "open_player",
        lambda request, restore_paused_state=False: opened.append((request, restore_paused_state)),
    )

    window.telegram_page.open_requested.emit("https://pan.quark.cn/s/f518510ef92a")

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)
    assert opened[0][0].vod.vod_name == "Telegram Movie"
    assert opened[0][0].source_vod_id == "https://pan.quark.cn/s/f518510ef92a"
    assert opened[0][1] is False


def test_main_window_uses_latest_async_open_request(qtbot, monkeypatch) -> None:
    controller = AsyncRequestController(_make_telegram_request)
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

    opened: list[tuple[OpenPlayerRequest, bool]] = []
    monkeypatch.setattr(
        window,
        "open_player",
        lambda request, restore_paused_state=False: opened.append((request, restore_paused_state)),
    )
    monkeypatch.setattr(window, "show_error", lambda message: None)

    window.telegram_page.open_requested.emit("vod-1")
    _wait_for_request_call(qtbot, controller, "vod-1")

    window.telegram_page.open_requested.emit("vod-2")
    _wait_for_request_call(qtbot, controller, "vod-2")

    controller.finish_request("vod-2", request=_make_telegram_request("vod-2", vod_name="Second"))
    qtbot.waitUntil(lambda: len(opened) == 1 and opened[0][0].source_vod_id == "vod-2", timeout=1000)

    controller.finish_request("vod-1", request=_make_telegram_request("vod-1", vod_name="First"))
    qtbot.wait(100)

    assert len(opened) == 1
    assert opened[0][0].vod.vod_name == "Second"
    assert opened[0][0].source_vod_id == "vod-2"
    assert opened[0][0].source_kind == "browse"


def test_main_window_async_open_request_surfaces_errors(qtbot, monkeypatch) -> None:
    controller = AsyncRequestController(_make_telegram_request)
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

    errors: list[str] = []
    monkeypatch.setattr(window, "show_error", lambda message: errors.append(message))

    window.telegram_page.open_requested.emit("broken")
    _wait_for_request_call(qtbot, controller, "broken")
    controller.finish_request("broken", exc=ValueError("打开失败"))

    qtbot.waitUntil(lambda: errors == ["打开失败"], timeout=1000)


def test_main_window_enables_search_controls_only_for_telegram_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
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
    assert window.live_page.keyword_edit.isHidden() is True


def test_main_window_keeps_search_controls_hidden_for_live_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)

    assert window.live_page.keyword_edit.isHidden() is True
    assert window.live_page.search_button.isHidden() is True
    assert window.live_page.clear_button.isHidden() is True


def visible_shortcut_help_dialogs() -> list[QDialog]:
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, QDialog)
        and widget.windowTitle() == "快捷键帮助"
        and widget.isVisible()
    ]


def shortcut_table_rows(dialog: QDialog) -> list[tuple[str, str]]:
    table = dialog.findChild(QTableWidget, "shortcutHelpTable")
    assert table is not None
    rows: list[tuple[str, str]] = []
    for row in range(table.rowCount()):
        rows.append((table.item(row, 0).text(), table.item(row, 1).text()))
    return rows


def test_main_window_f1_opens_shortcut_help_dialog(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    window.setFocus()

    QTest.keyClick(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    rows = shortcut_table_rows(visible_shortcut_help_dialogs()[0])

    assert ("F1", "打开快捷键帮助") in rows
    assert ("Ctrl+P", "显示或返回播放器") in rows
    assert ("Esc", "显示或返回播放器") in rows
    assert any(description == "退出应用" for _, description in rows)


def test_main_window_reuses_existing_shortcut_help_dialog(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    window.setFocus()

    QTest.keyClick(window, Qt.Key.Key_F1)
    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    first_dialog = visible_shortcut_help_dialogs()[0]

    QTest.keyClick(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    assert visible_shortcut_help_dialogs()[0] is first_dialog


def test_main_window_opening_player_closes_shortcut_help_dialog(qtbot, monkeypatch) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.opened: list[tuple[object, bool]] = []
            self.closed_to_main = type("Signal", (), {"connect": staticmethod(lambda _callback: None)})()

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    window.setFocus()

    QTest.keyClick(window, Qt.Key.Key_F1)
    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)

    request = OpenPlayerRequest(
        vod=VodItem(vod_id="vod-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
        clicked_index=0,
        source_mode="detail",
        source_vod_id="vod-1",
    )
    window._apply_open_player(request, {"session": "ok"})

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 0, timeout=1000)
    assert window.help_dialog is None


def test_main_window_restoring_existing_player_closes_shortcut_help_dialog(qtbot) -> None:
    class ExistingPlayerWindow:
        def __init__(self) -> None:
            self.session = object()
            self.resume_calls = 0
            self.show_calls = 0
            self.raise_calls = 0
            self.activate_calls = 0

        def resume_from_main(self) -> None:
            self.resume_calls += 1

        def show(self) -> None:
            self.show_calls += 1

        def raise_(self) -> None:
            self.raise_calls += 1

        def activateWindow(self) -> None:
            self.activate_calls += 1

    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.player_window = ExistingPlayerWindow()
    window.show()
    window.activateWindow()
    window.setFocus()

    QTest.keyClick(window, Qt.Key.Key_F1)
    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)

    window.show_or_restore_player()

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 0, timeout=1000)
    assert window.help_dialog is None


def test_main_window_enables_search_controls_for_emby_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
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
        live_controller=FakeLiveController(),
        emby_controller=controller,
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[tuple[OpenPlayerRequest, bool]] = []
    monkeypatch.setattr(
        window,
        "open_player",
        lambda request, restore_paused_state=False: opened.append((request, restore_paused_state)),
    )

    window.emby_page.item_open_requested.emit(VodItem(vod_id="1-3281", vod_name="Episode 1", vod_tag="file"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)
    assert opened[0][0].vod.vod_name == "Emby Movie"
    assert opened[0][0].source_vod_id == "1-3281"
    assert opened[0][1] is False


def test_main_window_emby_folder_click_loads_folder_in_current_tab(qtbot, monkeypatch) -> None:
    controller = FakeEmbyController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
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

    qtbot.waitUntil(lambda: controller.folder_calls == ["folder-1"] and len(shown) == 1, timeout=1000)
    assert opened == []
    assert controller.folder_calls == ["folder-1"]
    assert shown[0][1:] == (1, 1, "当前文件夹暂无内容")
    assert shown[0][0][0].vod_id == "child-1"


def test_main_window_opens_player_from_live_card_signal(qtbot, monkeypatch) -> None:
    controller = FakeLiveController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=controller,
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[tuple[OpenPlayerRequest, bool]] = []
    monkeypatch.setattr(
        window,
        "open_player",
        lambda request, restore_paused_state=False: opened.append((request, restore_paused_state)),
    )

    window.live_page.item_open_requested.emit(VodItem(vod_id="bili$1785607569", vod_name="直播间", vod_tag="file"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)
    assert opened[0][0].vod.vod_name == "Live Room"
    assert opened[0][0].source_vod_id == "bili$1785607569"
    assert opened[0][0].use_local_history is False
    assert opened[0][1] is False


def test_main_window_live_folder_click_loads_folder_in_current_tab(qtbot, monkeypatch) -> None:
    controller = FakeLiveController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=controller,
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
    shown = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(
        window.live_page,
        "show_items",
        lambda items, total, page=1, empty_message="当前分类暂无内容": shown.append((items, total, page, empty_message)),
    )

    window.live_page.item_open_requested.emit(VodItem(vod_id="bili-9", vod_name="分区", vod_tag="folder"))

    qtbot.waitUntil(lambda: controller.folder_calls == ["bili-9"] and len(shown) == 1, timeout=1000)
    assert opened == []
    assert controller.folder_calls == ["bili-9"]
    assert shown[0][1:] == (1, 1, "当前文件夹暂无内容")
    assert shown[0][0][0].vod_id == "child-live-1"


def test_main_window_live_folder_click_updates_breadcrumbs(qtbot, monkeypatch) -> None:
    controller = FakeLiveController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=controller,
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.live_page.ensure_loaded()

    qtbot.waitUntil(lambda: len(window.live_page.breadcrumb_buttons) == 2)
    monkeypatch.setattr(window.live_page, "show_items", lambda items, total, page=1, empty_message="当前分类暂无内容": None)

    window.live_page.item_open_requested.emit(VodItem(vod_id="bili-9", vod_name="分区", vod_tag="folder"))

    qtbot.waitUntil(lambda: [button.text() for button in window.live_page.breadcrumb_buttons] == ["首页", "推荐", "分区"])


def test_main_window_live_folder_uses_latest_async_result(qtbot, monkeypatch) -> None:
    controller = AsyncFolderController(_make_live_request)
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=controller,
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.live_page.ensure_loaded()
    qtbot.waitUntil(lambda: len(window.live_page.breadcrumb_buttons) == 2, timeout=1000)

    shown: list[tuple[list[VodItem], int, int, str]] = []
    monkeypatch.setattr(
        window.live_page,
        "show_items",
        lambda items, total, page=1, empty_message="当前分类暂无内容": shown.append((items, total, page, empty_message)),
    )
    monkeypatch.setattr(window, "show_error", lambda message: None)

    window.live_page.item_open_requested.emit(VodItem(vod_id="folder-a", vod_name="分区 A", vod_tag="folder"))
    _wait_for_folder_call(qtbot, controller, "folder-a")

    window.live_page.item_open_requested.emit(VodItem(vod_id="folder-b", vod_name="分区 B", vod_tag="folder"))
    _wait_for_folder_call(qtbot, controller, "folder-b")

    controller.finish_folder(
        "folder-b",
        items=[VodItem(vod_id="child-b", vod_name="直播 B", vod_tag="file")],
        total=1,
    )
    qtbot.waitUntil(lambda: len(shown) == 1 and shown[0][0][0].vod_name == "直播 B", timeout=1000)

    controller.finish_folder(
        "folder-a",
        items=[VodItem(vod_id="child-a", vod_name="直播 A", vod_tag="file")],
        total=1,
    )
    qtbot.wait(100)

    assert len(shown) == 1
    assert shown[0][0][0].vod_name == "直播 B"
    assert [button.text() for button in window.live_page.breadcrumb_buttons] == ["首页", "推荐", "分区 B"]


def test_main_window_live_breadcrumb_click_loads_category_root(qtbot, monkeypatch) -> None:
    controller = FakeLiveController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=controller,
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.live_page.ensure_loaded()

    qtbot.waitUntil(lambda: controller.item_calls == [("suggestion", 1)])
    monkeypatch.setattr(window.live_page, "show_items", lambda items, total, page=1, empty_message="当前分类暂无内容": None)
    window.live_page.item_open_requested.emit(VodItem(vod_id="bili-9", vod_name="分区", vod_tag="folder"))
    qtbot.waitUntil(lambda: [button.text() for button in window.live_page.breadcrumb_buttons] == ["首页", "推荐", "分区"])

    window.live_page.breadcrumb_buttons[1].click()

    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("suggestion", 1))
    qtbot.waitUntil(lambda: [button.text() for button in window.live_page.breadcrumb_buttons] == ["首页", "推荐"])


def test_main_window_enables_search_controls_for_jellyfin_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
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
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=controller,
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[tuple[OpenPlayerRequest, bool]] = []
    monkeypatch.setattr(
        window,
        "open_player",
        lambda request, restore_paused_state=False: opened.append((request, restore_paused_state)),
    )

    window.jellyfin_page.item_open_requested.emit(VodItem(vod_id="1-4001", vod_name="Episode 1", vod_tag="file"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)
    assert opened[0][0].vod.vod_name == "Jellyfin Movie"
    assert opened[0][0].source_vod_id == "1-4001"
    assert opened[0][1] is False


def test_main_window_jellyfin_folder_click_loads_folder_in_current_tab(qtbot, monkeypatch) -> None:
    controller = FakeJellyfinController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
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

    qtbot.waitUntil(lambda: controller.folder_calls == ["folder-1"] and len(shown) == 1, timeout=1000)
    assert opened == []
    assert controller.folder_calls == ["folder-1"]
    assert shown[0][1:] == (1, 1, "当前文件夹暂无内容")
    assert shown[0][0][0].vod_id == "jf-child-1"


def test_main_window_emby_folder_click_updates_breadcrumbs(qtbot, monkeypatch) -> None:
    controller = FakeEmbyController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=controller,
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.emby_page.ensure_loaded()

    qtbot.waitUntil(lambda: len(window.emby_page.breadcrumb_buttons) == 2)
    monkeypatch.setattr(window.emby_page, "show_items", lambda items, total, page=1, empty_message="当前分类暂无内容": None)

    window.emby_page.item_open_requested.emit(VodItem(vod_id="folder-1", vod_name="Season 1", vod_tag="folder"))

    qtbot.waitUntil(lambda: [button.text() for button in window.emby_page.breadcrumb_buttons] == ["首页", "推荐", "Season 1"])


def test_main_window_emby_breadcrumb_click_loads_category_root(qtbot, monkeypatch) -> None:
    controller = FakeEmbyController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=controller,
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.emby_page.ensure_loaded()

    qtbot.waitUntil(lambda: controller.item_calls == [("suggestion", 1)])
    monkeypatch.setattr(window.emby_page, "show_items", lambda items, total, page=1, empty_message="当前分类暂无内容": None)
    window.emby_page.item_open_requested.emit(VodItem(vod_id="folder-1", vod_name="Season 1", vod_tag="folder"))
    qtbot.waitUntil(lambda: [button.text() for button in window.emby_page.breadcrumb_buttons] == ["首页", "推荐", "Season 1"])

    window.emby_page.breadcrumb_buttons[1].click()

    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("suggestion", 1))
    qtbot.waitUntil(lambda: [button.text() for button in window.emby_page.breadcrumb_buttons] == ["首页", "推荐"])


def test_main_window_jellyfin_folder_click_updates_breadcrumbs(qtbot, monkeypatch) -> None:
    controller = FakeJellyfinController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=controller,
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.jellyfin_page.ensure_loaded()

    qtbot.waitUntil(lambda: len(window.jellyfin_page.breadcrumb_buttons) == 2)
    monkeypatch.setattr(window.jellyfin_page, "show_items", lambda items, total, page=1, empty_message="当前分类暂无内容": None)

    window.jellyfin_page.item_open_requested.emit(VodItem(vod_id="folder-1", vod_name="Season 1", vod_tag="folder"))

    qtbot.waitUntil(lambda: [button.text() for button in window.jellyfin_page.breadcrumb_buttons] == ["首页", "推荐", "Season 1"])


def test_main_window_jellyfin_breadcrumb_click_loads_category_root(qtbot, monkeypatch) -> None:
    controller = FakeJellyfinController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=controller,
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.jellyfin_page.ensure_loaded()

    qtbot.waitUntil(lambda: controller.item_calls == [("suggestion", 1)])
    monkeypatch.setattr(window.jellyfin_page, "show_items", lambda items, total, page=1, empty_message="当前分类暂无内容": None)
    window.jellyfin_page.item_open_requested.emit(VodItem(vod_id="folder-1", vod_name="Season 1", vod_tag="folder"))
    qtbot.waitUntil(lambda: [button.text() for button in window.jellyfin_page.breadcrumb_buttons] == ["首页", "推荐", "Season 1"])

    window.jellyfin_page.breadcrumb_buttons[1].click()

    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("suggestion", 1))
    qtbot.waitUntil(lambda: [button.text() for button in window.jellyfin_page.breadcrumb_buttons] == ["首页", "推荐"])


def test_main_window_plugin_card_signal_opens_player_asynchronously(qtbot, monkeypatch) -> None:
    controller = AsyncPluginController(_make_telegram_request)
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": controller, "search_enabled": False}],
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[OpenPlayerRequest] = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "show_error", lambda message: None)
    plugin_page = window._plugin_pages[0][0]

    plugin_page.open_requested.emit("plugin-vod-1")
    _wait_for_request_call(qtbot, controller, "plugin-vod-1")
    controller.finish_request("plugin-vod-1", request=_make_telegram_request("plugin-vod-1", vod_name="插件电影"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)

    assert opened[0].vod.vod_name == "插件电影"
    assert opened[0].source_kind == "plugin"
    assert opened[0].source_key == "plugin-1"


def test_main_window_opens_history_detail_asynchronously(qtbot, monkeypatch) -> None:
    browse_controller = AsyncHistoryBrowseController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=browse_controller,
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[OpenPlayerRequest] = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "show_error", lambda message: None)

    window.open_history_detail("history-vod-1")
    _wait_for_history_detail_call(qtbot, browse_controller, "history-vod-1")
    browse_controller.finish_detail("history-vod-1")

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)

    assert opened[0].vod.vod_name == "History Movie"
    assert opened[0].source_vod_id == "history-vod-1"


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
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    app, repo = app_module.build_application()

    assert app.application_name == "atv-player"
    assert not app.window_icon.isNull()
    assert (tmp_path / "app-data" / "app.db").exists()
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
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    app_module.build_application()

    assert (tmp_path / "app-cache" / "posters").is_dir()


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
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    cache_dir = tmp_path / "app-cache" / "posters"
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


def test_build_application_uses_shared_app_path_helpers(monkeypatch, tmp_path) -> None:
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
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    app_module.build_application()

    assert (tmp_path / "app-data" / "app.db").exists()
    assert (tmp_path / "app-cache" / "posters").is_dir()


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


def test_app_coordinator_start_returns_login_window_when_vod_token_fetch_raises_api_error(monkeypatch) -> None:
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

    class FailingApiClient:
        def __init__(self, base_url: str, token: str = "", vod_token: str = "") -> None:
            self.base_url = base_url
            self.token = token
            self.vod_token = vod_token

        def fetch_vod_token(self) -> str:
            raise app_module.ApiError("请求超时")

    class SignalStub:
        def connect(self, callback) -> None:
            self.callback = callback

    class FakeLoginWindow:
        def __init__(self, controller) -> None:
            self.controller = controller
            self.login_succeeded = SignalStub()
            self.error_message = ""

        def set_error_message(self, message: str) -> None:
            self.error_message = message

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)

    monkeypatch.setattr(app_module, "ApiClient", FailingApiClient)
    monkeypatch.setattr(app_module, "LoginWindow", FakeLoginWindow)

    widget = coordinator.start()

    assert isinstance(widget, FakeLoginWindow)
    assert widget.error_message == "请求超时"
    assert repo.config.token == "auth-123"
    assert repo.config.vod_token == ""


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


def test_app_coordinator_show_main_starts_async_player_restore_when_supported(monkeypatch) -> None:
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
            self.async_restore_calls = 0

        def _start_restore_last_player(self) -> None:
            self.async_restore_calls += 1

        def restore_last_player(self):
            raise AssertionError("sync restore should not be used when async restore is supported")

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)

    monkeypatch.setattr(app_module, "ApiClient", FakeApiClient)
    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(coordinator, "_build_api_client", lambda: FakeApiClient("http://127.0.0.1:4567", "auth-123", "vod-123"))

    widget = coordinator._show_main()

    assert isinstance(widget, FakeMainWindow)
    assert widget.async_restore_calls == 1


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


def test_app_coordinator_starts_epg_and_remote_live_refresh_in_background(monkeypatch, tmp_path) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.database_path = tmp_path / "app.db"
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

    class FakeMainWindow:
        logout_requested = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class FakeEpgService:
        def __init__(self) -> None:
            self.refresh_calls = 0
            self.event = threading.Event()

        def load_config(self):
            return type("Config", (), {"epg_url": "https://example.com/epg.xml.gz"})()

        def save_url(self, epg_url: str) -> None:
            return None

        def refresh(self) -> None:
            self.refresh_calls += 1
            self.event.set()

    class FakeLiveSourceManager:
        def __init__(self) -> None:
            self.event = threading.Event()

        def list_sources(self):
            return [type("Source", (), {"id": 1, "source_type": "remote"})()]

        def refresh_source(self, source_id: int):
            assert source_id == 1
            self.event.set()

        def load_categories(self):
            return []

    fake_epg_service = FakeEpgService()
    fake_live_source_manager = FakeLiveSourceManager()

    monkeypatch.setattr(app_module, "LiveSourceRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "LiveEpgRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())
    monkeypatch.setattr(app_module, "SpiderPluginManager", lambda repository, loader: FakePluginManager())
    monkeypatch.setattr(app_module, "LiveEpgService", lambda repository, http_client: fake_epg_service)
    monkeypatch.setattr(
        app_module,
        "CustomLiveService",
        lambda repository, http_client, epg_service=None: fake_live_source_manager,
    )
    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)
    monkeypatch.setattr(
        coordinator,
        "_build_api_client",
        lambda: FakeApiClient(repo.config.base_url, repo.config.token, repo.config.vod_token),
    )

    window = coordinator._show_main()

    assert isinstance(window, FakeMainWindow)
    assert fake_epg_service.event.wait(timeout=1)
    assert fake_live_source_manager.event.wait(timeout=1)


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
    qtbot.waitUntil(
        lambda: window.browse_page.breadcrumb_layout.itemAt(0).widget().text() == "/电影 | 加载文件列表超时",
        timeout=1000,
    )
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

    qtbot.waitUntil(lambda: window.player_window is not None)
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


def test_main_window_show_or_restore_player_resumes_existing_hidden_session(qtbot) -> None:
    class ExistingPlayerWindow:
        def __init__(self) -> None:
            self.session = object()
            self.show_calls = 0
            self.raise_calls = 0
            self.activate_calls = 0
            self.resume_calls = 0

        def show(self) -> None:
            self.show_calls += 1

        def raise_(self) -> None:
            self.raise_calls += 1

        def activateWindow(self) -> None:
            self.activate_calls += 1

        def resume_from_main(self) -> None:
            self.resume_calls += 1

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
    assert window.player_window.resume_calls == 1


def test_main_window_ctrl_p_restores_last_player_when_missing(qtbot, monkeypatch) -> None:
    class AsyncRecordingPlayerWindow:
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

    controller = AsyncHistoryBrowseController()
    config = AppConfig(last_active_window="main", last_playback_mode="detail", last_playback_vod_id="vod-1", last_player_paused=True)
    window = MainWindow(
        browse_controller=controller,
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(main_window_module, "PlayerWindow", AsyncRecordingPlayerWindow)

    restored = window.show_or_restore_player()
    assert restored is None
    _wait_for_history_detail_call(qtbot, controller, "vod-1")
    controller.finish_detail("vod-1", request=_make_history_request("vod-1", vod_name="Restored Movie"))

    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1, timeout=1000)

    assert window.player_window.opened[0][1] is True
    assert window.player_window.opened[0][0]["vod"].vod_name == "Restored Movie"


def test_main_window_show_or_restore_player_uses_latest_async_restore_result(qtbot, monkeypatch) -> None:
    class AsyncRecordingPlayerWindow:
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

    controller = AsyncHistoryBrowseController()
    config = AppConfig(last_active_window="main", last_playback_mode="detail", last_playback_vod_id="vod-1", last_player_paused=True)
    window = MainWindow(
        browse_controller=controller,
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(main_window_module, "PlayerWindow", AsyncRecordingPlayerWindow)

    window.show_or_restore_player()
    _wait_for_history_detail_call(qtbot, controller, "vod-1")

    config.last_playback_vod_id = "vod-2"
    window.show_or_restore_player()
    _wait_for_history_detail_call(qtbot, controller, "vod-2")

    controller.finish_detail("vod-2", request=_make_history_request("vod-2", vod_name="Second Restore"))
    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1, timeout=1000)

    controller.finish_detail("vod-1", request=_make_history_request("vod-1", vod_name="First Restore"))
    qtbot.wait(100)

    assert len(window.player_window.opened) == 1
    assert window.player_window.opened[0][1] is True
    assert window.player_window.opened[0][0]["vod"].vod_name == "Second Restore"


def test_main_window_show_or_restore_player_loads_folder_restore_off_main_thread(qtbot, monkeypatch) -> None:
    class AsyncRecordingPlayerWindow:
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

    controller = AsyncRestoreFolderBrowseController()
    config = AppConfig(
        last_active_window="main",
        last_playback_mode="folder",
        last_playback_path="/TV",
        last_playback_clicked_vod_id="target-vod",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=controller,
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(main_window_module, "PlayerWindow", AsyncRecordingPlayerWindow)

    restored = window.show_or_restore_player()
    assert restored is None
    _wait_for_restore_folder_call(qtbot, controller, "/TV", 1, 50)

    controller.finish_load(
        "/TV",
        page=1,
        size=50,
        items=[VodItem(vod_id="target-vod", vod_name="Episode 1", path="/TV/Ep1.mkv", type=2)],
        total=1,
    )

    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1, timeout=1000)

    assert controller.request_calls == ["target-vod"]
    assert window.player_window.opened[0][1] is True
    assert window.player_window.opened[0][0]["vod"].vod_name == "Episode 1"


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


def test_main_window_restore_last_player_routes_emby_detail_to_emby_controller(qtbot, monkeypatch) -> None:
    class RestoreBrowseController:
        def build_request_from_detail(self, vod_id: str):
            raise AssertionError(f"browse restore should not be used for {vod_id}")

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

    controller = FakeEmbyController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_source="emby",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        emby_controller=controller,
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    assert window.player_window.opened[0][0]["vod"].vod_name == "Emby Movie"
    assert window.player_window.opened[0][1] is True


def test_main_window_restore_last_player_routes_plugin_detail_to_plugin_controller_with_playback_history_loader(
    qtbot,
    monkeypatch,
) -> None:
    class RestoreBrowseController:
        def build_request_from_detail(self, vod_id: str):
            raise AssertionError(f"browse restore should not be used for {vod_id}")

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

    class RestorePluginController:
        def load_categories(self):
            return []

        def load_items(self, category_id: str, page: int):
            return [], 0

        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="插件电影"),
                playlist=[PlayItem(title="第2集", url="https://media.example/2.m3u8")],
                clicked_index=0,
                source_kind="plugin",
                source_mode="detail",
                source_vod_id=vod_id,
                use_local_history=False,
                playback_history_loader=lambda: HistoryRecord(
                    id=0,
                    key=vod_id,
                    vod_name="插件电影",
                    vod_pic="poster",
                    vod_remarks="第2集",
                    episode=0,
                    episode_url="https://media.example/2.m3u8",
                    position=45000,
                    opening=0,
                    ending=0,
                    speed=1.0,
                    create_time=1713206400000,
                ),
            )

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_source="plugin",
        last_playback_source_key="plugin-1",
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
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": RestorePluginController(), "search_enabled": False}],
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    session = window.player_window.opened[0][0]
    assert session["vod"].vod_name == "插件电影"
    assert session["use_local_history"] is False
    assert session["playback_history_loader"] is not None
    assert session["playback_history_loader"]().position == 45000
    assert window.player_window.opened[0][1] is True
