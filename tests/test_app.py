import os
import httpx
import logging
from pathlib import Path
import pytest
import threading
import time
from types import SimpleNamespace
from PySide6.QtCore import QRect, Qt, QUrl
from PySide6.QtGui import QIcon, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit, QPushButton, QTableWidget, QToolButton

import atv_player.app as app_module
import atv_player.ui.help_dialog as help_dialog_module
import atv_player.ui.main_window as main_window_module
from atv_player.api import ApiClient
from atv_player.ai.openai_compatible import OpenAICompatibleError
from atv_player.app import AppCoordinator, decide_start_view
from atv_player.diagnostics import SystemInfoEntry
from atv_player.log_store import AppLogEvent
from atv_player.metadata.bindings import MetadataBindingRepository
from atv_player.metadata.cache import MetadataCache
from atv_player.metadata.hydrator import MetadataHydrator
from atv_player.metadata.models import MetadataContext, MetadataMatch, MetadataQuery
from atv_player.models import (
    AppConfig,
    AppIdentity,
    DoubanCategory,
    FavoriteRecord,
    HistoryRecord,
    OpenPlayerRequest,
    PlayItem,
    PlaybackDetailField,
    PlaybackDetailFieldAction,
    PlaybackDetailValuePart,
    VodItem,
)
from atv_player.ui.main_window import MainWindow
from atv_player.ui.player_window import PlayerWindow
from atv_player.ui.theme import DARK_TOKENS


class RaisingTransport(httpx.BaseTransport):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise self.exc


class FakeAISettingsClient:
    def __init__(self, models: list[str] | None = None, error: Exception | None = None) -> None:
        self.models = models or []
        self.error = error
        self.list_model_calls = 0
        self.connectivity_calls = 0

    def list_models(self) -> list[str]:
        self.list_model_calls += 1
        if self.error is not None:
            raise self.error
        return list(self.models)

    def check_connectivity(self) -> bool:
        self.connectivity_calls += 1
        if self.error is not None:
            raise self.error
        return True


class _FakeRepoBase:
    """Shared base for inline ``FakeRepo`` doubles used by AppCoordinator tests.

    AppCoordinator._show_main reads the installation identity from the repo, so
    every repo double must satisfy that interface.
    """

    def ensure_app_identity(self) -> AppIdentity:
        return AppIdentity(installation_id="installation-id", created_at=0)


class _NoOpSignal:
    """Minimal Qt-signal double for dialog tests that only need ``connect``."""

    def connect(self, _slot) -> None:
        pass


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

    def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
        del filters
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


class FakeTelegramChannelController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self.build_calls: list[str] = []

    def build_request(self, vod_id: str):
        self.build_calls.append(vod_id)
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="Telegram Channel Movie"),
            playlist=[PlayItem(title="Episode 1", url="", vod_id="ep-tg-channel-1")],
            clicked_index=0,
            source_kind="telegram_channel",
            source_mode="detail",
            source_vod_id=vod_id,
        )


class FakePansouController(FakeDoubanController):
    def search_items(self, keyword: str, page: int, category_id: str = ""):
        return [], 0

    def resolve_search_result(self, item: VodItem) -> str:
        return item.vod_id


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


class FakeFeiniuController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self.folder_calls: list[str] = []

    def build_request(self, vod_id: str):
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="Feiniu Movie"),
            playlist=[PlayItem(title="Episode 1", url="", vod_id="ep-feiniu-1")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )

    def load_folder_items(self, vod_id: str):
        self.folder_calls.append(vod_id)
        return [VodItem(vod_id="fn-child-1", vod_name="Episode 1", vod_tag="file")], 1


class FakeBilibiliController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self.folder_calls: list[str] = []
        self.build_calls: list[str] = []

    def build_request(self, vod_id: str):
        self.build_calls.append(vod_id)
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="B站视频"),
            playlist=[PlayItem(title="第1话", url="", vod_id="BVep-1")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )

    def load_folder_items(self, vod_id: str):
        self.folder_calls.append(vod_id)
        return [VodItem(vod_id="BVchild-1", vod_name="第1话", vod_tag="file")], 1


class FakeYoutubeController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self.detail_calls: list[str] = []
        self.fast_calls: list[str] = []

    def build_request_from_item(self, item):
        self.fast_calls.append(item.vod_id)
        return OpenPlayerRequest(
            vod=VodItem(vod_id=item.vod_id, vod_name=item.vod_name),
            playlist=[PlayItem(title=item.vod_name, url="", vod_id=item.vod_id)],
            clicked_index=0,
            source_kind="youtube",
            source_mode="detail",
            source_vod_id=item.vod_id,
            playback_loader=lambda current_item: None,
            async_playback_loader=True,
        )

    def build_request(self, vod_id: str):
        self.detail_calls.append(vod_id)
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="YouTube视频"),
            playlist=[PlayItem(title="正片", url="", vod_id="yt:video:abc123")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )


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

    def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
        del filters
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

    def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
        del filters
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
        source_groups=None,
        source_group_index: int = 0,
        source_index: int = 0,
        source_kind: str = "",
        source_key: str = "",
        source_display_name: str = "",
        detail_resolver=None,
        resolved_vod_by_id=None,
        use_local_history=True,
        restore_history=False,
        playback_loader=None,
        detail_action_runner=None,
        detail_field_runner=None,
        metadata_hydrator=None,
        metadata_scrape_service=None,
        subtitle_search_service=None,
        metadata_binding_repository=None,
        episode_title_override_repository=None,
        episode_title_enhancer=None,
        danmaku_controller=None,
        playback_progress_reporter=None,
        playback_stopper=None,
        playback_history_loader=None,
        playback_history_saver=None,
        async_playback_loader=False,
        initial_log_message="",
        is_placeholder=False,
        drive_resource_id: str = "",
        drive_files_loader=None,
    ):
        return {
            "vod": vod,
            "playlist": playlist,
            "clicked_index": clicked_index,
            "playlists": playlists,
            "playlist_index": playlist_index,
            "source_groups": source_groups,
            "source_group_index": source_group_index,
            "source_index": source_index,
            "source_kind": source_kind,
            "source_key": source_key,
            "source_display_name": source_display_name,
            "detail_resolver": detail_resolver,
            "resolved_vod_by_id": resolved_vod_by_id or {},
            "use_local_history": use_local_history,
            "restore_history": restore_history,
            "playback_loader": playback_loader,
            "detail_action_runner": detail_action_runner,
            "detail_field_runner": detail_field_runner,
            "metadata_hydrator": metadata_hydrator,
            "metadata_scrape_service": metadata_scrape_service,
            "metadata_binding_repository": metadata_binding_repository,
            "episode_title_enhancer": episode_title_enhancer,
            "danmaku_controller": danmaku_controller,
            "playback_progress_reporter": playback_progress_reporter,
            "playback_stopper": playback_stopper,
            "playback_history_loader": playback_history_loader,
            "playback_history_saver": playback_history_saver,
            "async_playback_loader": async_playback_loader,
            "initial_log_message": initial_log_message,
            "is_placeholder": is_placeholder,
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
    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
        "豆瓣电影",
        "全球片单",
        "电报影视",
        "网络直播",
        "Emby",
        "Jellyfin",
        "飞牛影视",
        "文件浏览",
        "我的收藏",
        "我的追更",
        "播放记录",
    ]


def test_main_window_restores_last_selected_main_tab_on_startup(qtbot) -> None:
    douban_controller = RecordingDoubanController()
    browse_controller = RecordingBrowseController()
    history_controller = RecordingHistoryController()
    window = MainWindow(
        douban_controller=douban_controller,
        telegram_controller=RecordingDoubanController(),
        live_controller=RecordingDoubanController(),
        emby_controller=RecordingDoubanController(),
        jellyfin_controller=RecordingDoubanController(),
        browse_controller=browse_controller,
        history_controller=history_controller,
        player_controller=FakePlayerController(),
        config=AppConfig(last_selected_tab="history"),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.currentWidget() is window.history_page
    assert douban_controller.category_calls == 0
    assert browse_controller.load_calls == []
    assert history_controller.load_calls == [(1, 100)]


def test_main_window_remembers_selected_main_tab(qtbot) -> None:
    saved = {"count": 0}
    config = AppConfig()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )

    qtbot.addWidget(window)
    window.show()

    window.nav_tabs.setCurrentWidget(window.history_page)

    assert config.last_selected_tab == "history"
    assert saved["count"] >= 1


def test_main_window_remembers_selected_category_for_current_main_tab(qtbot) -> None:
    saved = {"count": 0}
    config = AppConfig()
    douban_controller = FakeDoubanController()
    douban_controller.categories = [
        DoubanCategory(type_id="suggestion", type_name="推荐"),
        DoubanCategory(type_id="movie", type_name="电影"),
    ]
    telegram_controller = FakeTelegramController()
    telegram_controller.categories = [
        DoubanCategory(type_id="suggestion", type_name="推荐"),
        DoubanCategory(type_id="movie", type_name="电影"),
    ]
    window = MainWindow(
        douban_controller=douban_controller,
        telegram_controller=telegram_controller,
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )

    qtbot.addWidget(window)
    window.show()

    qtbot.waitUntil(lambda: window.douban_page.selected_category_id == "suggestion")
    window.douban_page.category_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: config.last_selected_category_tab == "douban")
    assert config.last_selected_category_id == "movie"

    window.nav_tabs.setCurrentWidget(window.telegram_page)
    qtbot.waitUntil(lambda: window.telegram_page.selected_category_id == "suggestion")
    window.telegram_page.category_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: config.last_selected_category_tab == "telegram")
    assert config.last_selected_category_id == "movie"

    window.douban_page.category_list.setCurrentRow(0)
    qtbot.wait(50)

    assert config.last_selected_category_tab == "telegram"
    assert config.last_selected_category_id == "movie"
    assert saved["count"] >= 1


def test_main_window_restores_last_selected_category_for_selected_main_tab_on_startup(qtbot) -> None:
    douban_controller = FakeDoubanController()
    telegram_controller = FakeTelegramController()
    douban_controller.categories = [
        DoubanCategory(type_id="suggestion", type_name="推荐"),
        DoubanCategory(type_id="movie", type_name="电影"),
    ]
    telegram_controller.categories = [
        DoubanCategory(type_id="suggestion", type_name="推荐"),
        DoubanCategory(type_id="movie", type_name="电影"),
    ]
    window = MainWindow(
        douban_controller=douban_controller,
        telegram_controller=telegram_controller,
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(
            last_selected_tab="telegram",
            last_selected_category_tab="telegram",
            last_selected_category_id="movie",
        ),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.currentWidget() is window.telegram_page
    qtbot.waitUntil(lambda: window.telegram_page.selected_category_id == "movie")
    assert telegram_controller.item_calls == [("movie", 1)]
    assert douban_controller.item_calls == []


def test_main_window_restores_saved_category_when_returning_to_tab_after_startup(qtbot) -> None:
    douban_controller = FakeDoubanController()
    telegram_controller = FakeTelegramController()
    douban_controller.categories = [
        DoubanCategory(type_id="suggestion", type_name="推荐"),
        DoubanCategory(type_id="movie", type_name="电影"),
    ]
    telegram_controller.categories = [
        DoubanCategory(type_id="suggestion", type_name="推荐"),
        DoubanCategory(type_id="movie", type_name="电影"),
    ]
    window = MainWindow(
        douban_controller=douban_controller,
        telegram_controller=telegram_controller,
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(
            last_selected_tab="history",
            last_selected_category_tab="telegram",
            last_selected_category_id="movie",
        ),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.currentWidget() is window.history_page
    window.nav_tabs.setCurrentWidget(window.douban_page)
    qtbot.waitUntil(lambda: window.douban_page.selected_category_id == "movie")
    assert douban_controller.item_calls == [("movie", 1)]
    window.nav_tabs.setCurrentWidget(window.telegram_page)
    qtbot.waitUntil(lambda: window.telegram_page.selected_category_id == "movie")
    assert telegram_controller.item_calls == [("movie", 1)]
    assert douban_controller.item_calls == [("movie", 1)]


def test_main_window_persists_and_restores_global_category_across_restart(qtbot, tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    config = repo.load_config()
    douban_controller = FakeDoubanController()
    douban_controller.categories = [
        DoubanCategory(type_id="suggestion", type_name="推荐"),
        DoubanCategory(type_id="movie", type_name="电影"),
    ]
    window = MainWindow(
        douban_controller=douban_controller,
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: repo.save_config(config),
    )

    qtbot.addWidget(window)
    window.show()

    qtbot.waitUntil(lambda: window.douban_page.selected_category_id == "suggestion")
    window.douban_page.category_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: config.last_selected_category_id == "movie")
    window.close()

    restored = MainWindow(
        douban_controller=douban_controller,
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=repo.load_config(),
    )

    qtbot.addWidget(restored)
    restored.show()

    qtbot.waitUntil(lambda: restored.douban_page.selected_category_id == "movie")


def test_main_window_delayed_plugin_tabs_restore_startup_global_category(qtbot) -> None:
    load_started = threading.Event()
    release_load = threading.Event()
    plugin_controller = FakeDoubanController()
    plugin_controller.categories = [
        DoubanCategory(type_id="suggestion", type_name="推荐"),
        DoubanCategory(type_id="movie", type_name="电影"),
    ]
    douban_controller = FakeDoubanController()
    douban_controller.categories = [
        DoubanCategory(type_id="suggestion", type_name="推荐"),
        DoubanCategory(type_id="movie", type_name="电影"),
    ]

    def plugin_loader_task():
        load_started.set()
        assert release_load.wait(timeout=5), "plugin load was never released"
        return [{"id": "plugin-1", "title": "红果短剧", "controller": plugin_controller, "search_enabled": True}]

    window = MainWindow(
        douban_controller=douban_controller,
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(last_selected_tab="history", last_selected_category_id="movie"),
        plugin_loader_task=plugin_loader_task,
    )

    qtbot.addWidget(window)
    window.show()

    assert load_started.wait(timeout=1)

    window.nav_tabs.setCurrentWidget(window.douban_page)
    qtbot.waitUntil(lambda: window.douban_page.selected_category_id == "movie")
    window.douban_page.category_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: window.config.last_selected_category_id == "suggestion")
    window.nav_tabs.setCurrentWidget(window.history_page)

    release_load.set()
    qtbot.waitUntil(lambda: any(plugin_id == "plugin-1" for _page, _controller, plugin_id in window._plugin_pages))

    plugin_page = next(page for page, _controller, plugin_id in window._plugin_pages if plugin_id == "plugin-1")
    window.nav_tabs.setCurrentWidget(plugin_page)

    qtbot.waitUntil(lambda: plugin_page.selected_category_id == "movie")
    assert plugin_controller.item_calls == [("movie", 1)]


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
    captured_loader = {"drive": None, "offline": None}

    class FakePluginManager:
        def load_enabled_plugins(self, drive_detail_loader=None, offline_download_detail_loader=None):
            captured_loader["drive"] = drive_detail_loader
            captured_loader["offline"] = offline_download_detail_loader
            return loaded_plugins

    def api_factory(*args, **kwargs):
        return ApiClient(
            "http://127.0.0.1:4567",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"token": "vod-123"})),
        )

    monkeypatch.setattr(app_module, "ApiClient", api_factory)
    monkeypatch.setattr(
        app_module,
        "SpiderPluginManager",
        lambda repository, loader, playback_history_repository, **_kwargs: FakePluginManager(),
    )
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())

    coordinator = AppCoordinator(repo)
    coordinator._yt_dlp_service = SimpleNamespace(is_available=lambda: False)
    widget = coordinator._show_main()
    qtbot.addWidget(widget)
    widget.resize(920, 520)
    widget.show()

    qtbot.waitUntil(
        lambda: "红果短剧" in [widget.nav_tabs.tabText(i) for i in range(widget.nav_tabs.count())]
    )
    assert callable(captured_loader["drive"])
    assert callable(captured_loader["offline"])


def test_app_coordinator_shows_main_window_before_startup_plugins_finish_loading(qtbot, monkeypatch, tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    repo.save_config(
        AppConfig(
            base_url="http://127.0.0.1:4567",
            username="alice",
            token="token-123",
            vod_token="vod-123",
        )
    )

    load_started = threading.Event()
    release_load = threading.Event()

    class FakePluginManager:
        def load_enabled_plugins(self, drive_detail_loader=None, offline_download_detail_loader=None):
            load_started.set()
            assert release_load.wait(timeout=5), "plugin load was never released"
            return [{"id": "plugin-1", "title": "红果短剧", "controller": object(), "search_enabled": True}]

    def api_factory(*args, **kwargs):
        return ApiClient(
            "http://127.0.0.1:4567",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"token": "vod-123"})),
        )

    monkeypatch.setattr(app_module, "ApiClient", api_factory)
    monkeypatch.setattr(
        app_module,
        "SpiderPluginManager",
        lambda repository, loader, playback_history_repository, **_kwargs: FakePluginManager(),
    )
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())

    coordinator = AppCoordinator(repo)
    widget = coordinator._show_main()
    qtbot.addWidget(widget)
    widget.show()

    assert load_started.wait(timeout=1)
    assert widget is coordinator.main_window
    assert "插件加载中" in [widget.nav_tabs.tabText(i) for i in range(widget.nav_tabs.count())]

    release_load.set()


def test_app_coordinator_passes_playback_parser_service_into_main_window(qtbot, monkeypatch, tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(base_url="http://127.0.0.1:4567", token="token-123", vod_token="vod-123"))
    captured = {"parser_service": None, "offline_loader": None}

    class FakeSignal:
        def connect(self, callback) -> None:
            return None

    class FakeMainWindow:
        def __init__(self, *args, **kwargs) -> None:
            captured["parser_service"] = kwargs.get("playback_parser_service")
            captured["offline_loader"] = kwargs.get("offline_download_detail_loader")
            self.logout_requested = FakeSignal()

    class FakePluginManager:
        def load_enabled_plugins(self, drive_detail_loader=None, offline_download_detail_loader=None):
            return []

    def api_factory(*args, **kwargs):
        return ApiClient(
            "http://127.0.0.1:4567",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"token": "vod-123"})),
        )

    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(app_module, "ApiClient", api_factory)
    monkeypatch.setattr(
        app_module,
        "SpiderPluginManager",
        lambda repository, loader, playback_history_repository, **_kwargs: FakePluginManager(),
    )
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())
    monkeypatch.setattr(app_module, "LocalPlaybackHistoryRepository", lambda db_path: object())

    coordinator = AppCoordinator(repo)
    coordinator._show_main()

    assert captured["parser_service"] is not None
    assert callable(captured["offline_loader"])


def test_app_coordinator_passes_startup_plugin_loader_task_into_main_window(monkeypatch, tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(base_url="http://127.0.0.1:4567", token="token-123", vod_token="vod-123"))
    captured = {"plugin_loader_task": None}

    class FakeSignal:
        def connect(self, callback) -> None:
            return None

    class FakeMainWindow:
        def __init__(self, *args, **kwargs) -> None:
            captured["plugin_loader_task"] = kwargs.get("plugin_loader_task")
            self.logout_requested = FakeSignal()

    class FakePluginManager:
        def load_enabled_plugins(self, drive_detail_loader=None, offline_download_detail_loader=None):
            return []

    def api_factory(*args, **kwargs):
        return ApiClient(
            "http://127.0.0.1:4567",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"token": "vod-123"})),
        )

    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(app_module, "ApiClient", api_factory)
    monkeypatch.setattr(
        app_module,
        "SpiderPluginManager",
        lambda repository, loader, playback_history_repository, **_kwargs: FakePluginManager(),
    )
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())
    monkeypatch.setattr(app_module, "LocalPlaybackHistoryRepository", lambda db_path: object())

    coordinator = AppCoordinator(repo)
    coordinator._show_main()

    assert callable(captured["plugin_loader_task"])


def test_app_coordinator_wires_fixed_heat_controller_into_main_window(monkeypatch, tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(base_url="http://127.0.0.1:4567", token="token-123", vod_token="vod-123"))
    identity = repo.ensure_app_identity()
    captured = {"heat_controller": None, "service": None, "installation_id": ""}

    class FakeSignal:
        def connect(self, callback) -> None:
            return None

    class FakeMainWindow:
        def __init__(self, *args, **kwargs) -> None:
            captured["heat_controller"] = kwargs.get("heat_controller")
            self.logout_requested = FakeSignal()

    class FakeHeatService:
        def __init__(self) -> None:
            captured["service"] = self

    class FakeHeatController:
        def __init__(self, service, *, installation_id: str) -> None:
            self.service = service
            self.installation_id = installation_id
            captured["installation_id"] = installation_id

    class FakePluginManager:
        def load_enabled_plugins(self, drive_detail_loader=None, offline_download_detail_loader=None):
            return []

    def api_factory(*args, **kwargs):
        return ApiClient(
            "http://127.0.0.1:4567",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"token": "vod-123"})),
        )

    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(app_module, "HeatService", FakeHeatService)
    monkeypatch.setattr(app_module, "HeatController", FakeHeatController)
    monkeypatch.setattr(app_module, "ApiClient", api_factory)
    monkeypatch.setattr(
        app_module,
        "SpiderPluginManager",
        lambda repository, loader, playback_history_repository, **_kwargs: FakePluginManager(),
    )
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())
    monkeypatch.setattr(app_module, "LocalPlaybackHistoryRepository", lambda db_path: object())

    coordinator = AppCoordinator(repo)
    coordinator._show_main()

    assert captured["heat_controller"] is not None
    assert captured["heat_controller"].service is captured["service"]
    assert captured["installation_id"] == identity.installation_id


def test_app_coordinator_startup_plugin_loader_prioritizes_last_plugin_restore_targets(
    monkeypatch, tmp_path,
) -> None:
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    repo.save_config(
        AppConfig(
            base_url="http://127.0.0.1:4567",
            token="token-123",
            vod_token="vod-123",
            last_playback_source="plugin",
            last_playback_source_key="plugin-9",
            last_selected_tab="plugin:plugin-2",
        )
    )
    captured = {"plugin_loader_task": None, "prioritized_plugin_ids": None}

    class FakeSignal:
        def connect(self, callback) -> None:
            return None

    class FakeMainWindow:
        def __init__(self, *args, **kwargs) -> None:
            captured["plugin_loader_task"] = kwargs.get("plugin_loader_task")
            self.logout_requested = FakeSignal()

    class FakePluginManager:
        def iter_enabled_plugins(
            self,
            drive_detail_loader=None,
            offline_download_detail_loader=None,
            *,
            prioritized_plugin_ids=(),
        ):
            del drive_detail_loader, offline_download_detail_loader
            captured["prioritized_plugin_ids"] = tuple(prioritized_plugin_ids)
            return iter([])

    def api_factory(*args, **kwargs):
        return ApiClient(
            "http://127.0.0.1:4567",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"token": "vod-123"})),
        )

    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(app_module, "ApiClient", api_factory)
    monkeypatch.setattr(
        app_module,
        "SpiderPluginManager",
        lambda repository, loader, playback_history_repository, **_kwargs: FakePluginManager(),
    )
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())
    monkeypatch.setattr(app_module, "LocalPlaybackHistoryRepository", lambda db_path: object())

    coordinator = AppCoordinator(repo)
    coordinator._show_main()

    assert callable(captured["plugin_loader_task"])
    list(captured["plugin_loader_task"]())
    assert captured["prioritized_plugin_ids"] == ("plugin-9", "plugin-2")


def test_app_coordinator_classic_mode_startup_plugin_loader_only_loads_selected_plugin(
    monkeypatch, tmp_path,
) -> None:
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    repo.save_config(
        AppConfig(
            base_url="http://127.0.0.1:4567",
            token="token-123",
            vod_token="vod-123",
            home_mode="classic",
            last_playback_source="plugin",
            last_playback_source_key="plugin-9",
            last_selected_tab="plugin:plugin-2",
        )
    )
    captured = {"plugin_loader_task": None, "plugin_ids": None}

    class FakeSignal:
        def connect(self, callback) -> None:
            return None

    class FakeMainWindow:
        def __init__(self, *args, **kwargs) -> None:
            captured["plugin_loader_task"] = kwargs.get("plugin_loader_task")
            self.logout_requested = FakeSignal()

    class FakePluginManager:
        def load_plugins(self, plugin_ids, drive_detail_loader=None, offline_download_detail_loader=None):
            del drive_detail_loader, offline_download_detail_loader
            captured["plugin_ids"] = tuple(plugin_ids)
            return []

    def api_factory(*args, **kwargs):
        return ApiClient(
            "http://127.0.0.1:4567",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"token": "vod-123"})),
        )

    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(app_module, "ApiClient", api_factory)
    monkeypatch.setattr(
        app_module,
        "SpiderPluginManager",
        lambda repository, loader, playback_history_repository, **_kwargs: FakePluginManager(),
    )
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())
    monkeypatch.setattr(app_module, "LocalPlaybackHistoryRepository", lambda db_path: object())

    coordinator = AppCoordinator(repo)
    coordinator._show_main()

    assert callable(captured["plugin_loader_task"])
    list(captured["plugin_loader_task"]())
    assert captured["plugin_ids"] == ("plugin-2",)


def test_app_coordinator_wires_danmaku_service_into_plugin_manager(monkeypatch, tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(base_url="http://127.0.0.1:4567", token="token-123", vod_token="vod-123"))
    captured = {"manager": None, "preference_store": None}

    class FakePluginManager:
        def __init__(self) -> None:
            captured["manager"] = self

    def plugin_manager_factory(
        repository,
        loader,
        playback_history_repository,
        **kwargs,
    ) -> FakePluginManager:
        captured["preference_store"] = kwargs.get("danmaku_preference_store")
        return FakePluginManager()

    monkeypatch.setattr(
        app_module,
        "SpiderPluginManager",
        plugin_manager_factory,
    )
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())
    monkeypatch.setattr(app_module, "LocalPlaybackHistoryRepository", lambda db_path: object())

    coordinator = app_module.AppCoordinator(repo)

    assert getattr(captured["manager"], "_danmaku_service", None) is not None
    assert captured["preference_store"] is coordinator._danmaku_preference_store


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


def test_main_window_hides_emby_jellyfin_and_feiniu_tabs_when_disabled(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        feiniu_controller=FakeFeiniuController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_emby_tab=False,
        show_jellyfin_tab=False,
        show_feiniu_tab=False,
    )

    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.count() == 8
    assert window.nav_tabs.tabText(0) == "豆瓣电影"
    assert window.nav_tabs.tabText(1) == "全球片单"
    assert window.nav_tabs.tabText(2) == "电报影视"
    assert window.nav_tabs.tabText(3) == "网络直播"
    assert window.nav_tabs.tabText(4) == "文件浏览"
    assert window.nav_tabs.tabText(5) == "我的收藏"
    assert window.nav_tabs.tabText(6) == "我的追更"
    assert window.nav_tabs.tabText(7) == "播放记录"


def test_main_window_inserts_bilibili_tab_immediately_after_telegram(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        bilibili_controller=FakeBilibiliController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        feiniu_controller=FakeFeiniuController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_bilibili_tab=True,
    )

    qtbot.addWidget(window)
    window.show()

    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())][:7] == [
        "豆瓣电影",
        "全球片单",
        "电报影视",
        "B站",
        "网络直播",
        "Emby",
        "Jellyfin",
    ]


def test_main_window_inserts_youtube_tab_when_enabled(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        youtube_controller=FakeYoutubeController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        feiniu_controller=FakeFeiniuController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_youtube_tab=True,
    )

    qtbot.addWidget(window)
    window.show()

    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())][:4] == [
        "豆瓣电影",
        "全球片单",
        "电报影视",
        "YouTube",
    ]
    assert window.youtube_page.keyword_edit.isHidden() is False


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

    assert window.logout_button.toolTip() == "退出登录"
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
        def __init__(self, manager, parent=None, **kwargs) -> None:
            self.manager = manager
            self.builtin_tabs_saved = _NoOpSignal()

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
        def __init__(self, manager, parent=None, **kwargs) -> None:
            self.manager = manager
            self.builtin_tabs_saved = _NoOpSignal()

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
        def __init__(self, manager, parent=None, **kwargs) -> None:
            self.manager = manager
            self.builtin_tabs_saved = _NoOpSignal()

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
        def __init__(self, manager, parent=None, **kwargs) -> None:
            self.manager = manager
            self.builtin_tabs_saved = _NoOpSignal()
            self.plugin_tabs_dirty = True

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "PluginManagerDialog", FakeDialog)

    window._open_plugin_manager()

    assert captured_loaders == [drive_detail_loader]


def test_main_window_reloads_plugins_with_offline_download_loader_after_plugin_manager_closes(qtbot, monkeypatch) -> None:
    captured_loaders: list[object | None] = []
    offline_download_detail_loader = object()

    class OfflineAwarePluginManager:
        def load_enabled_plugins(self, drive_detail_loader=None, offline_download_detail_loader=None):
            captured_loaders.append(offline_download_detail_loader)
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
        plugin_manager=OfflineAwarePluginManager(),
        offline_download_detail_loader=offline_download_detail_loader,
    )
    qtbot.addWidget(window)

    class FakeDialog:
        def __init__(self, manager, parent=None, **kwargs) -> None:
            self.manager = manager
            self.builtin_tabs_saved = _NoOpSignal()
            self.parent = parent
            self.plugin_tabs_dirty = True

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "PluginManagerDialog", FakeDialog)

    window._open_plugin_manager()

    assert captured_loaders == [offline_download_detail_loader]


def test_main_window_does_not_reload_plugins_when_plugin_manager_closes_without_structural_changes(qtbot, monkeypatch) -> None:
    captured_rebuilds: list[str] = []

    class QuietPluginManager:
        def load_enabled_plugins(self, drive_detail_loader=None, offline_download_detail_loader=None):
            captured_rebuilds.append("load")
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
        plugin_manager=QuietPluginManager(),
    )
    qtbot.addWidget(window)

    class FakeDialog:
        def __init__(self, manager, parent=None, **kwargs) -> None:
            self.manager = manager
            self.builtin_tabs_saved = _NoOpSignal()
            self.parent = parent
            self.plugin_tabs_dirty = False

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "PluginManagerDialog", FakeDialog)
    monkeypatch.setattr(window, "_rebuild_spider_plugin_tabs", lambda: captured_rebuilds.append("rebuild"))

    window._open_plugin_manager()

    assert captured_rebuilds == []


def test_main_window_reloads_only_changed_plugins_when_plugin_manager_closes(qtbot, monkeypatch) -> None:
    changed_definition = {"id": "plugin-2", "title": "插件2-新", "controller": object(), "search_enabled": True}
    load_all_calls: list[str] = []
    load_changed_calls: list[tuple[str, ...]] = []

    class SelectivePluginManager:
        def load_enabled_plugins(self, drive_detail_loader=None, offline_download_detail_loader=None):
            load_all_calls.append("all")
            return []

        def load_plugins(self, plugin_ids, drive_detail_loader=None, offline_download_detail_loader=None):
            load_changed_calls.append(tuple(str(plugin_id) for plugin_id in plugin_ids))
            return [changed_definition]

        def list_plugins(self):
            return [
                type("Plugin", (), {"id": "plugin-1", "enabled": True})(),
                type("Plugin", (), {"id": "plugin-2", "enabled": True})(),
            ]

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
        plugin_manager=SelectivePluginManager(),
        spider_plugins=[
            {"id": "plugin-1", "title": "插件1", "controller": object(), "search_enabled": True},
            {"id": "plugin-2", "title": "插件2", "controller": object(), "search_enabled": True},
        ],
    )
    qtbot.addWidget(window)

    class FakeDialog:
        def __init__(self, manager, parent=None, **kwargs) -> None:
            self.manager = manager
            self.builtin_tabs_saved = _NoOpSignal()
            self.parent = parent
            self.plugin_tabs_dirty = True
            self.changed_plugin_ids = ["plugin-2"]

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(main_window_module, "PluginManagerDialog", FakeDialog)

    window._open_plugin_manager()

    assert load_all_calls == []
    assert load_changed_calls == [("plugin-2",)]
    assert [definition["id"] for definition in window._plugin_definitions] == ["plugin-1", "plugin-2"]
    assert window._plugin_definitions[1]["title"] == "插件2-新"


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


def test_main_window_starts_global_search_from_douban_signal(qtbot, monkeypatch) -> None:
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

    started_keywords: list[str] = []
    monkeypatch.setattr(window, "_start_global_search", lambda: started_keywords.append(window.global_search_edit.text()))

    window.douban_page.search_requested.emit("霸王别姬")

    assert started_keywords == ["霸王别姬"]
    assert window.global_search_edit.text() == "霸王别姬"


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


def test_main_window_telegram_item_open_prefers_card_title_over_obfuscated_detail_title(qtbot, monkeypatch) -> None:
    class ObfuscatedTelegramController(FakeTelegramController):
        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="8@swf2fkq3zrk@t58d"),
                playlist=[PlayItem(title="查看", url="", vod_id="detail-ep-1", media_title="8@swf2fkq3zrk@t58d")],
                clicked_index=0,
                source_mode="detail",
                source_vod_id=vod_id,
            )

    controller = ObfuscatedTelegramController()
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

    window.telegram_page.item_open_requested.emit(
        VodItem(vod_id="detail-1", vod_name="📺 电视剧：良陈美锦 (2026) S01E25", type_name="电视剧")
    )

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)
    assert opened[0][0].vod.vod_name == "📺 电视剧：良陈美锦 (2026) S01E25"
    assert opened[0][0].playlist[0].media_title == "📺 电视剧：良陈美锦 (2026) S01E25"
    assert opened[0][0].playlist[0].type_name == "电视剧"


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


def test_main_window_enables_drive_filter_for_telegram_sources_and_pansou(
    qtbot,
) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        telegram_channel_controller=FakeTelegramChannelController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        pansou_controller=FakePansouController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_telegram_channel_tab=True,
    )
    qtbot.addWidget(window)

    assert window.telegram_page._search_drive_filter_enabled is True
    assert window.telegram_channel_page._search_drive_filter_enabled is True
    assert window.pansou_page._search_drive_filter_enabled is True
    assert window.douban_page._search_drive_filter_enabled is False
    assert window.live_page._search_drive_filter_enabled is False

    window.pansou_page.show_external_results(
        [VodItem(vod_id="p1", vod_name="盘搜结果", share_type="5")],
        total=1,
    )

    assert window.pansou_page.search_drive_filter_combo.isHidden() is False


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
        and widget.windowTitle() == "帮助"
        and widget.isVisible()
    ]


def shortcut_table_rows(dialog: QDialog) -> list[tuple[str, str]]:
    table = dialog.findChild(QTableWidget, "shortcutHelpTable")
    assert table is not None
    rows: list[tuple[str, str]] = []
    for row in range(table.rowCount()):
        rows.append((table.item(row, 0).text(), table.item(row, 1).text()))
    return rows


def system_info_table_rows(dialog: QDialog) -> list[tuple[str, str]]:
    table = dialog.findChild(QTableWidget, "systemInfoTable")
    assert table is not None
    rows: list[tuple[str, str]] = []
    for row in range(table.rowCount()):
        value_widget = table.cellWidget(row, 1)
        if isinstance(value_widget, QLabel):
            document = QTextDocument()
            document.setHtml(value_widget.text())
            value = document.toPlainText().strip()
        else:
            value = table.item(row, 1).text()
        rows.append((table.item(row, 0).text(), value))
    return rows


def click_system_info_value_cell(qtbot, dialog: QDialog, row: int) -> None:
    table = dialog.findChild(QTableWidget, "systemInfoTable")
    assert table is not None
    cell_widget = table.cellWidget(row, 1)
    if isinstance(cell_widget, QLabel):
        link = cell_widget.text()
        assert 'href="' in link
        href = link.split('href="', 1)[1].split('"', 1)[0]
        cell_widget.linkActivated.emit(href)
        return
    index = table.model().index(row, 1)
    rect = table.visualRect(index)
    assert rect.isValid()
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())


def system_info_value_widget(dialog: QDialog, row: int):
    table = dialog.findChild(QTableWidget, "systemInfoTable")
    assert table is not None
    return table.cellWidget(row, 1)


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
    window._build_main_window_help_payload = lambda: (
        [
            SystemInfoEntry("atv-player", "0.8.2", "https://github.com/power721/atv-player/releases/latest"),
            SystemInfoEntry("Python", "3.12.8", "https://www.python.org/downloads/"),
            SystemInfoEntry("PySide6", "6.8.1", "https://doc.qt.io/qtforpython-6/"),
            SystemInfoEntry("mpv", "0.39", "https://mpv.io/installation/"),
            SystemInfoEntry("ffmpeg", "7.1", "https://www.ffmpeg.org/download.html"),
            SystemInfoEntry("yt-dlp", "2026.05.17", "https://github.com/yt-dlp/yt-dlp/releases/latest"),
            SystemInfoEntry("Platform", "Linux", None),
        ],
        "diagnostic text",
        "detailed diagnostic text",
    )

    QTest.keyClick(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    dialog = visible_shortcut_help_dialogs()[0]
    rows = shortcut_table_rows(dialog)
    qtbot.waitUntil(lambda: ("atv-player", "0.8.2") in system_info_table_rows(dialog))
    system_rows = system_info_table_rows(dialog)

    assert ("F1", "打开帮助") in rows
    assert ("Ctrl+P", "显示或返回播放器") in rows
    assert ("Esc", "显示或返回播放器") in rows
    assert any(description == "退出应用" for _, description in rows)
    assert ("atv-player", "0.8.2") in system_rows
    assert ("Python", "3.12.8") in system_rows
    assert ("PySide6", "6.8.1") in system_rows
    assert ("mpv", "0.39") in system_rows
    assert ("ffmpeg", "7.1") in system_rows
    assert ("yt-dlp", "2026.05.17") in system_rows
    assert ("Platform", "Linux") in system_rows


def test_main_window_f1_opens_help_dialog_before_dependency_checks_finish(qtbot) -> None:
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

    release_payload = threading.Event()
    payload_started = threading.Event()

    def slow_payload():
        payload_started.set()
        assert release_payload.wait(timeout=1)
        return (
            [
                SystemInfoEntry("Node.js", "20.11.1", "https://nodejs.org/en/download"),
                SystemInfoEntry("yt-dlp", "2026.05.17", "https://github.com/yt-dlp/yt-dlp/releases/latest"),
            ],
            "系统信息\nNode.js: 20.11.1\nyt-dlp: 2026.05.17",
            "详细诊断",
        )

    window._build_main_window_help_payload = slow_payload

    QTest.keyClick(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    dialog = visible_shortcut_help_dialogs()[0]
    assert system_info_table_rows(dialog) == [("依赖状态", "检查中...")]
    assert payload_started.wait(timeout=1)

    release_payload.set()

    qtbot.waitUntil(
        lambda: ("Node.js", "20.11.1") in system_info_table_rows(dialog),
        timeout=2000,
    )
    assert ("yt-dlp", "2026.05.17") in system_info_table_rows(dialog)


def test_main_window_help_dialog_opens_system_info_links_except_platform(qtbot, monkeypatch) -> None:
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
    window._build_main_window_help_payload = lambda: (
        [
            SystemInfoEntry("atv-player", "0.8.2", "https://github.com/power721/atv-player/releases/latest"),
            SystemInfoEntry("Python", "3.12.8", "https://www.python.org/downloads/"),
            SystemInfoEntry("PySide6", "6.8.1", "https://doc.qt.io/qtforpython-6/"),
            SystemInfoEntry("mpv", "0.39", "https://mpv.io/installation/"),
            SystemInfoEntry("ffmpeg", "7.1", "https://www.ffmpeg.org/download.html"),
            SystemInfoEntry("yt-dlp", "2026.05.17", "https://github.com/yt-dlp/yt-dlp/releases/latest"),
            SystemInfoEntry("Platform", "Linux", None),
        ],
        "diagnostic text",
        "detailed diagnostic text",
    )

    opened: list[str] = []
    monkeypatch.setattr(
        help_dialog_module.QDesktopServices,
        "openUrl",
        lambda url: opened.append(QUrl(url).toString()) or True,
    )

    QTest.keyClick(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    dialog = visible_shortcut_help_dialogs()[0]
    qtbot.waitUntil(
        lambda: (
            (table := dialog.findChild(QTableWidget, "systemInfoTable")) is not None
            and table.rowCount() == 7
        )
    )

    click_system_info_value_cell(qtbot, dialog, 0)
    click_system_info_value_cell(qtbot, dialog, 1)
    click_system_info_value_cell(qtbot, dialog, 2)
    click_system_info_value_cell(qtbot, dialog, 3)
    click_system_info_value_cell(qtbot, dialog, 4)
    click_system_info_value_cell(qtbot, dialog, 5)
    click_system_info_value_cell(qtbot, dialog, 6)

    assert opened == [
        "https://github.com/power721/atv-player/releases/latest",
        "https://www.python.org/downloads/",
        "https://doc.qt.io/qtforpython-6/",
        "https://mpv.io/installation/",
        "https://www.ffmpeg.org/download.html",
        "https://github.com/yt-dlp/yt-dlp/releases/latest",
    ]


def test_main_window_help_dialog_renders_system_info_links_like_player_metadata(qtbot) -> None:
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
    window._build_main_window_help_payload = lambda: (
        [
            SystemInfoEntry("Python", "3.12.8", "https://www.python.org/downloads/"),
            SystemInfoEntry("Platform", "Linux", None),
        ],
        "diagnostic text",
        "detailed diagnostic text",
    )

    QTest.keyClick(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    dialog = visible_shortcut_help_dialogs()[0]
    qtbot.waitUntil(lambda: isinstance(system_info_value_widget(dialog, 0), QLabel))
    link_widget = system_info_value_widget(dialog, 0)
    plain_widget = system_info_value_widget(dialog, 1)
    table = dialog.findChild(QTableWidget, "systemInfoTable")
    assert table is not None

    player_window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(player_window)
    expected_link_html = player_window._external_metadata_link_html(
        "https://www.python.org/downloads/",
        "3.12.8",
    )

    assert isinstance(link_widget, QLabel)
    assert plain_widget is None
    assert table.item(0, 1) is None
    assert table.item(1, 1).text() == "Linux"
    assert bool(link_widget.alignment() & Qt.AlignmentFlag.AlignVCenter)
    expected_accent = expected_link_html.split("color:")[1].split(";")[0]
    rendered_html = link_widget.text()
    assert 'href="https://www.python.org/downloads/"' in rendered_html
    assert "font-weight:600" in expected_link_html
    assert "font-weight:600" in rendered_html
    assert "text-decoration:none" in expected_link_html
    assert "text-decoration: underline" not in rendered_html
    assert expected_accent in rendered_html


def test_main_window_help_dialog_renders_install_links_for_missing_ytdlp_and_nodejs(
    qtbot, monkeypatch
) -> None:
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
    window._build_main_window_help_payload = lambda: (
        [
            SystemInfoEntry("Node.js", "未安装", "https://nodejs.org/en/download"),
            SystemInfoEntry("mpv", "未安装", "https://mpv.io/installation/"),
            SystemInfoEntry("yt-dlp", "未安装", "https://github.com/yt-dlp/yt-dlp/releases/latest"),
        ],
        "diagnostic text",
        "detailed diagnostic text",
    )

    opened: list[str] = []
    installed: list[str] = []
    monkeypatch.setattr(
        help_dialog_module.QDesktopServices,
        "openUrl",
        lambda url: opened.append(QUrl(url).toString()) or True,
    )
    monkeypatch.setattr(
        help_dialog_module,
        "install_dependency",
        lambda component: installed.append(component),
    )
    monkeypatch.setattr(
        help_dialog_module.QMessageBox,
        "information",
        lambda *args: None,
    )

    QTest.keyClick(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    dialog = visible_shortcut_help_dialogs()[0]
    table = dialog.findChild(QTableWidget, "systemInfoTable")
    assert table is not None
    # System info rows are populated asynchronously via a background thread;
    # wait for the Node.js install link to be rendered before asserting.
    qtbot.waitUntil(lambda: isinstance(table.cellWidget(0, 1), QLabel), timeout=5000)

    node_widget = system_info_value_widget(dialog, 0)
    mpv_widget = system_info_value_widget(dialog, 1)
    ytdlp_widget = system_info_value_widget(dialog, 2)

    assert isinstance(node_widget, QLabel)
    assert isinstance(mpv_widget, QLabel)
    assert isinstance(ytdlp_widget, QLabel)
    assert QTextDocument(html=node_widget.text()).toPlainText().strip() == "一键安装"
    assert QTextDocument(html=mpv_widget.text()).toPlainText().strip() == "未安装"
    assert QTextDocument(html=ytdlp_widget.text()).toPlainText().strip() == "一键安装"

    click_system_info_value_cell(qtbot, dialog, 0)
    click_system_info_value_cell(qtbot, dialog, 2)

    assert installed == ["Node.js", "yt-dlp"]
    assert opened == []


def test_main_window_help_dialog_copy_diagnostics_button_copies_text(qtbot) -> None:
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
    window._build_main_window_help_payload = lambda: (
        [SystemInfoEntry("atv-player", "0.8.2", "https://github.com/power721/atv-player/releases/latest")],
        "atv-player: 0.8.2\nPython: 3.12.8",
        "系统信息\natv-player: 0.8.2",
    )

    clipboard = QApplication.clipboard()
    assert clipboard is not None
    clipboard.clear()

    QTest.keyClick(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    dialog = visible_shortcut_help_dialogs()[0]
    copy_button = dialog.findChild(QPushButton, "copyDiagnosticsButton")
    assert copy_button is not None
    qtbot.waitUntil(
        lambda: dialog._diagnostics_text == "atv-player: 0.8.2\nPython: 3.12.8"
    )

    QTest.mouseClick(copy_button, Qt.MouseButton.LeftButton)

    assert clipboard.text() == "atv-player: 0.8.2\nPython: 3.12.8"


def test_main_window_help_dialog_export_diagnostics_button_writes_file(qtbot, monkeypatch, tmp_path) -> None:
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
    window._build_main_window_help_payload = lambda: (
        [SystemInfoEntry("atv-player", "0.8.2", "https://github.com/power721/atv-player/releases/latest")],
        "atv-player: 0.8.2\nPython: 3.12.8",
        "系统信息\natv-player: 0.8.2",
    )

    export_path = tmp_path / "diagnostics.txt"
    monkeypatch.setattr(
        "atv_player.ui.help_dialog.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(export_path), "Text Files (*.txt)"),
    )

    QTest.keyClick(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    dialog = visible_shortcut_help_dialogs()[0]
    export_button = dialog.findChild(QPushButton, "exportDiagnosticsButton")
    assert export_button is not None
    qtbot.waitUntil(
        lambda: dialog._diagnostics_text == "atv-player: 0.8.2\nPython: 3.12.8"
    )

    QTest.mouseClick(export_button, Qt.MouseButton.LeftButton)

    assert export_path.read_text(encoding="utf-8") == "atv-player: 0.8.2\nPython: 3.12.8"


def test_main_window_help_dialog_export_detailed_diagnostics_button_writes_file(
    qtbot, monkeypatch, tmp_path
) -> None:
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
    window._build_main_window_help_payload = lambda: (
        [SystemInfoEntry("atv-player", "0.8.2", "https://github.com/power721/atv-player/releases/latest")],
        "atv-player: 0.8.2",
        (
            "系统信息\n"
            "atv-player: 0.8.2\n\n"
            "运行环境\n"
            "Qt 平台: xcb\n\n"
            "应用配置摘要\n"
            "主题: dark\n\n"
            "插件摘要\n"
            "已启用插件数: 1\n\n"
            "最近日志\n"
            "[2026-05-22T12:00:00.000] INFO app/app 启动完成"
        ),
    )

    export_path = tmp_path / "detailed.txt"
    monkeypatch.setattr(
        "atv_player.ui.help_dialog.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(export_path), "Text Files (*.txt)"),
    )

    QTest.keyClick(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    dialog = visible_shortcut_help_dialogs()[0]
    export_button = dialog.findChild(QPushButton, "exportDetailedDiagnosticsButton")
    assert export_button is not None
    qtbot.waitUntil(lambda: "Qt 平台: xcb" in dialog._detailed_diagnostics_text)

    QTest.mouseClick(export_button, Qt.MouseButton.LeftButton)

    assert export_path.read_text(encoding="utf-8") == (
        "系统信息\n"
        "atv-player: 0.8.2\n\n"
        "运行环境\n"
        "Qt 平台: xcb\n\n"
        "应用配置摘要\n"
        "主题: dark\n\n"
        "插件摘要\n"
        "已启用插件数: 1\n\n"
        "最近日志\n"
        "[2026-05-22T12:00:00.000] INFO app/app 启动完成"
    )


def test_main_window_help_payload_diagnostics_text_excludes_shortcuts(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(
        main_window_module,
        "collect_system_info_entries",
        lambda: (
            SystemInfoEntry("atv-player", "0.8.2", "https://github.com/power721/atv-player/releases/latest"),
            SystemInfoEntry("Platform", "Linux 6.8.0 (x86_64)"),
        ),
    )

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

    system_info_rows, diagnostics_text, detailed_diagnostics_text = window._build_main_window_help_payload()

    assert system_info_rows == [
        SystemInfoEntry("atv-player", "0.8.2", "https://github.com/power721/atv-player/releases/latest"),
        SystemInfoEntry("Platform", "Linux 6.8.0 (x86_64)"),
    ]
    assert diagnostics_text == "系统信息\natv-player: 0.8.2\nPlatform: Linux 6.8.0 (x86_64)"
    assert "系统信息" in detailed_diagnostics_text
    assert "快捷键" not in detailed_diagnostics_text


def test_main_window_help_payload_builds_detailed_diagnostics_text(qtbot, monkeypatch, tmp_path) -> None:
    class FakePluginManager:
        def list_plugins(self):
            return [
                SimpleNamespace(display_name="插件一", enabled=True),
                SimpleNamespace(display_name="插件二", enabled=False),
            ]

    class FakeLogService:
        def load_records(self, *, limit, log_filter):
            del log_filter
            assert limit == 20
            return [
                AppLogEvent(
                    timestamp="2026-05-22T12:00:00.000",
                    level="INFO",
                    source="app",
                    category="app",
                    message="启动完成",
                    module="main",
                )
            ]

    monkeypatch.setattr(
        main_window_module,
        "collect_system_info_entries",
        lambda: (
            SystemInfoEntry("atv-player", "0.8.2", "https://github.com/power721/atv-player/releases/latest"),
            SystemInfoEntry("Platform", "Linux 6.8.0 (x86_64)"),
        ),
    )
    monkeypatch.setattr(main_window_module.QGuiApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(main_window_module, "app_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(main_window_module, "app_cache_dir", lambda: tmp_path / "cache")

    config = AppConfig(theme_mode="dark", network_proxy_mode="direct", base_url="http://127.0.0.1:4567")
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        plugin_manager=FakePluginManager(),
        app_log_service=FakeLogService(),
    )
    qtbot.addWidget(window)

    system_info_rows, diagnostics_text, detailed_text = window._build_main_window_help_payload()

    assert system_info_rows == [
        SystemInfoEntry("atv-player", "0.8.2", "https://github.com/power721/atv-player/releases/latest"),
        SystemInfoEntry("Platform", "Linux 6.8.0 (x86_64)"),
    ]
    assert diagnostics_text == "系统信息\natv-player: 0.8.2\nPlatform: Linux 6.8.0 (x86_64)"
    assert "系统信息" in detailed_text
    assert "运行环境" in detailed_text
    assert "Qt 平台: xcb" in detailed_text
    assert "应用配置摘要" in detailed_text
    assert "主题: dark" in detailed_text
    assert "代理模式: direct" in detailed_text
    assert "后端地址: http://127.0.0.1:4567" in detailed_text
    assert "插件摘要" in detailed_text
    assert "已启用插件数: 1" in detailed_text
    assert "插件一" in detailed_text
    assert "最近日志" in detailed_text
    assert "[2026-05-22T12:00:00.000] INFO app/app 启动完成" in detailed_text


def test_main_window_help_dialog_detailed_export_uses_dedicated_default_filename(
    qtbot, monkeypatch
) -> None:
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
    window._build_main_window_help_payload = lambda: (
        [SystemInfoEntry("atv-player", "0.8.2", "https://github.com/power721/atv-player/releases/latest")],
        "atv-player: 0.8.2",
        "系统信息\natv-player: 0.8.2",
    )

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "atv_player.ui.help_dialog.QFileDialog.getSaveFileName",
        lambda parent, title, default_name, filters: calls.append((title, default_name)) or ("", filters),
    )

    QTest.keyClick(window, Qt.Key.Key_F1)
    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    dialog = visible_shortcut_help_dialogs()[0]
    button = dialog.findChild(QPushButton, "exportDetailedDiagnosticsButton")
    assert button is not None

    QTest.mouseClick(button, Qt.MouseButton.LeftButton)

    assert calls == [("导出详细诊断", "atv-player-diagnostics-detailed.txt")]


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


def test_main_window_enables_search_controls_for_feiniu_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        feiniu_controller=FakeFeiniuController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)

    assert window.feiniu_page.keyword_edit.isHidden() is False


def test_main_window_enables_search_controls_for_bilibili_page(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        bilibili_controller=FakeBilibiliController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_bilibili_tab=True,
    )
    qtbot.addWidget(window)

    assert window.bilibili_page.keyword_edit.isHidden() is False


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


def test_main_window_opens_player_from_feiniu_card_signal(qtbot, monkeypatch) -> None:
    controller = FakeFeiniuController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        feiniu_controller=controller,
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

    window.feiniu_page.item_open_requested.emit(VodItem(vod_id="1-5001", vod_name="Episode 1", vod_tag="file"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)
    assert opened[0][0].vod.vod_name == "Feiniu Movie"
    assert opened[0][0].source_vod_id == "1-5001"
    assert opened[0][1] is False


def test_main_window_opens_player_from_bilibili_card_signal(qtbot, monkeypatch) -> None:
    controller = FakeBilibiliController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        bilibili_controller=controller,
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_bilibili_tab=True,
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[tuple[OpenPlayerRequest, bool]] = []
    monkeypatch.setattr(
        window,
        "open_player",
        lambda request, restore_paused_state=False: opened.append((request, restore_paused_state)),
    )

    window.bilibili_page.item_open_requested.emit(VodItem(vod_id="BV1xx411c7mD", vod_name="第1话", vod_tag="file"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)
    assert opened[0][0].vod.vod_name == "B站视频"
    assert opened[0][0].source_vod_id == "BV1xx411c7mD"
    assert opened[0][1] is False


def test_main_window_opens_player_from_telegram_channel_card_signal(qtbot, monkeypatch) -> None:
    controller = FakeTelegramChannelController()
    bilibili_controller = FakeBilibiliController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        telegram_channel_controller=controller,
        bilibili_controller=bilibili_controller,
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_telegram_channel_tab=True,
        show_bilibili_tab=True,
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[tuple[OpenPlayerRequest, bool]] = []
    monkeypatch.setattr(
        window,
        "open_player",
        lambda request, restore_paused_state=False: opened.append((request, restore_paused_state)),
    )

    window.telegram_channel_page.item_open_requested.emit(
        VodItem(vod_id="tg-channel-vod-1", vod_name="频道视频", vod_tag="file")
    )

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)
    assert controller.build_calls == ["tg-channel-vod-1"]
    assert bilibili_controller.build_calls == []
    assert opened[0][0].vod.vod_name == "Telegram Channel Movie"
    assert opened[0][0].source_kind == "telegram_channel"
    assert opened[0][0].source_vod_id == "tg-channel-vod-1"
    assert opened[0][1] is False


def test_main_window_opens_player_from_youtube_card_signal(qtbot, monkeypatch) -> None:
    controller = FakeYoutubeController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        youtube_controller=controller,
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_youtube_tab=True,
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[tuple[OpenPlayerRequest, bool]] = []
    monkeypatch.setattr(
        window,
        "open_player",
        lambda request, restore_paused_state=False: opened.append((request, restore_paused_state)),
    )
    monkeypatch.setattr(window, "_open_player_immediately", lambda request, restore_paused_state=False: None)

    window.youtube_page.item_open_requested.emit(VodItem(vod_id="yt:video:abc123", vod_name="正片", vod_tag="file"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)
    assert opened[0][0].vod.vod_name == "正片"
    assert opened[0][0].source_vod_id == "yt:video:abc123"
    assert opened[0][1] is False
    assert controller.fast_calls == ["yt:video:abc123"]
    assert controller.detail_calls == []


def test_main_window_youtube_card_opens_fast_request_without_waiting_for_detail(qtbot, monkeypatch) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.session = None
            self.opened: list[tuple[object, bool]] = []
            self.logs: list[str] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.session = session
            self.opened.append((session, start_paused))
            message = session.get("initial_log_message", "")
            if message:
                self.logs.append(message)

        def append_status_log(self, message: str) -> None:
            self.logs.append(message)

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    controller = FakeYoutubeController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        youtube_controller=controller,
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_youtube_tab=True,
    )
    qtbot.addWidget(window)
    window.show()

    window.youtube_page.item_open_requested.emit(
        VodItem(vod_id="yt:video:abc123", vod_name="卡片标题", vod_pic="card-poster")
    )

    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1, timeout=1000)
    assert window.player_window.opened[0][0]["vod"].vod_name == "卡片标题"
    assert window.player_window.opened[0][0]["playlist"][0].title == "卡片标题"
    assert window.player_window.opened[0][0]["playback_loader"] is not None
    assert window.player_window.opened[0][0]["async_playback_loader"] is True
    assert controller.fast_calls == ["yt:video:abc123"]
    assert controller.detail_calls == []


def test_main_window_youtube_card_does_not_start_slow_detail_request(qtbot, monkeypatch) -> None:
    controller = FakeYoutubeController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        youtube_controller=controller,
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_youtube_tab=True,
    )
    qtbot.addWidget(window)
    window.show()

    monkeypatch.setattr(
        window,
        "open_player",
        lambda request, restore_paused_state=False: None,
    )

    item = VodItem(vod_id="yt:video:abc123", vod_name="卡片标题", vod_tag="file")
    window.youtube_page.item_open_requested.emit(item)
    window.youtube_page.item_open_requested.emit(item)
    qtbot.wait(50)

    assert controller.fast_calls == ["yt:video:abc123", "yt:video:abc123"]
    assert controller.detail_calls == []


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


def test_main_window_feiniu_folder_click_loads_folder_in_current_tab(qtbot, monkeypatch) -> None:
    controller = FakeFeiniuController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        feiniu_controller=controller,
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
        window.feiniu_page,
        "show_items",
        lambda items, total, page=1, empty_message="当前分类暂无内容": shown.append((items, total, page, empty_message)),
    )

    window.feiniu_page.item_open_requested.emit(VodItem(vod_id="folder-1", vod_name="Season 1", vod_tag="folder"))

    qtbot.waitUntil(lambda: controller.folder_calls == ["folder-1"] and len(shown) == 1, timeout=1000)
    assert opened == []
    assert controller.folder_calls == ["folder-1"]
    assert shown[0][1:] == (1, 1, "当前文件夹暂无内容")
    assert shown[0][0][0].vod_id == "fn-child-1"


def test_main_window_bilibili_folder_click_loads_folder_in_current_tab(qtbot, monkeypatch) -> None:
    controller = FakeBilibiliController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        bilibili_controller=controller,
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_bilibili_tab=True,
    )
    qtbot.addWidget(window)
    window.show()

    opened = []
    shown = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(
        window.bilibili_page,
        "show_items",
        lambda items, total, page=1, empty_message="当前分类暂无内容": shown.append((items, total, page, empty_message)),
    )

    window.bilibili_page.item_open_requested.emit(VodItem(vod_id="folder-1", vod_name="第一季", vod_tag="folder"))

    qtbot.waitUntil(lambda: controller.folder_calls == ["folder-1"] and len(shown) == 1, timeout=1000)
    assert opened == []
    assert shown[0][1:] == (1, 1, "当前文件夹暂无内容")
    assert shown[0][0][0].vod_id == "BVchild-1"


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


def test_main_window_feiniu_breadcrumb_click_loads_category_root(qtbot, monkeypatch) -> None:
    controller = FakeFeiniuController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        feiniu_controller=controller,
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()
    window.feiniu_page.ensure_loaded()

    qtbot.waitUntil(lambda: controller.item_calls == [("suggestion", 1)])
    monkeypatch.setattr(window.feiniu_page, "show_items", lambda items, total, page=1, empty_message="当前分类暂无内容": None)
    window.feiniu_page.item_open_requested.emit(VodItem(vod_id="folder-1", vod_name="Season 1", vod_tag="folder"))
    qtbot.waitUntil(lambda: [button.text() for button in window.feiniu_page.breadcrumb_buttons] == ["首页", "推荐", "Season 1"])

    window.feiniu_page.breadcrumb_buttons[1].click()

    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("suggestion", 1))
    qtbot.waitUntil(lambda: [button.text() for button in window.feiniu_page.breadcrumb_buttons] == ["首页", "推荐"])


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
    monkeypatch.setattr(window, "_open_player_immediately", lambda request, restore_paused_state=False: None)
    monkeypatch.setattr(window, "show_error", lambda message: None)
    plugin_page = window._plugin_pages[0][0]

    plugin_page.item_open_requested.emit(VodItem(vod_id="plugin-vod-1", vod_name="插件电影"))
    _wait_for_request_call(qtbot, controller, "plugin-vod-1")
    controller.finish_request("plugin-vod-1", request=_make_telegram_request("plugin-vod-1", vod_name="插件电影"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)

    assert opened[0].vod.vod_name == "插件电影"
    assert opened[0].source_kind == "plugin"
    assert opened[0].source_key == "plugin-1"


def test_main_window_plugin_card_opens_placeholder_player_immediately_and_hydrates_later(qtbot, monkeypatch) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.session = None
            self.opened: list[tuple[object, bool]] = []
            self.logs: list[str] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.session = session
            self.opened.append((session, start_paused))
            message = session.get("initial_log_message", "")
            if message:
                self.logs.append(message)

        def append_status_log(self, message: str) -> None:
            self.logs.append(message)

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    controller = AsyncPluginController(_make_telegram_request)
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
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": controller, "search_enabled": False}],
    )
    qtbot.addWidget(window)
    window.show()

    plugin_page = window._plugin_pages[0][0]
    plugin_page.item_open_requested.emit(
        VodItem(vod_id="plugin-vod-1", vod_name="占位电影", vod_pic="poster-card")
    )

    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1, timeout=1000)
    assert window.player_window.opened[0][0]["vod"].vod_name == "占位电影"
    assert window.player_window.opened[0][0]["vod"].vod_pic == "poster-card"
    assert window.player_window.opened[0][0]["playlist"] == []
    assert window.player_window.logs == ["正在加载详情..."]

    _wait_for_request_call(qtbot, controller, "plugin-vod-1")
    assert len(window.player_window.opened) == 1

    controller.finish_request("plugin-vod-1", request=_make_telegram_request("plugin-vod-1", vod_name="插件电影"))

    qtbot.waitUntil(lambda: len(window.player_window.opened) == 2, timeout=1000)
    assert window.player_window.opened[1][0]["vod"].vod_name == "插件电影"
    assert window.player_window.opened[1][0]["playlist"][0].title == "Episode 1"


def test_main_window_plugin_card_preserves_card_category_name_when_request_detail_omits_it(qtbot, monkeypatch) -> None:
    def build_request(vod_id: str) -> OpenPlayerRequest:
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="插件电影"),
            playlist=[PlayItem(title="百度", url="", vod_id="https://pan.baidu.com/s/fake-movie")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )

    controller = AsyncPluginController(build_request)
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
    monkeypatch.setattr(window, "_open_player_immediately", lambda request, restore_paused_state=False: None)
    monkeypatch.setattr(window, "show_error", lambda message: None)

    plugin_page = window._plugin_pages[0][0]
    plugin_page.item_open_requested.emit(
        VodItem(vod_id="plugin-vod-1", vod_name="占位电影", category_name="多多电影")
    )

    _wait_for_request_call(qtbot, controller, "plugin-vod-1")
    controller.finish_request("plugin-vod-1", request=build_request("plugin-vod-1"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)
    assert opened[0].vod.category_name == "多多电影"
    assert opened[0].playlist[0].category_name == "多多电影"


def test_main_window_plugin_card_failure_keeps_placeholder_player_open_and_logs_error(qtbot, monkeypatch) -> None:
    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.session = None
            self.opened: list[tuple[object, bool]] = []
            self.logs: list[str] = []

        def open_session(self, session, start_paused: bool = False) -> None:
            self.session = session
            self.opened.append((session, start_paused))
            message = session.get("initial_log_message", "")
            if message:
                self.logs.append(message)

        def append_status_log(self, message: str) -> None:
            self.logs.append(message)

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    controller = AsyncPluginController(_make_telegram_request)
    errors: list[str] = []
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
        spider_plugins=[{"id": "plugin-1", "title": "插件一", "controller": controller, "search_enabled": False}],
    )
    qtbot.addWidget(window)
    window.show()
    monkeypatch.setattr(window, "show_error", lambda message: errors.append(message))

    plugin_page = window._plugin_pages[0][0]
    plugin_page.item_open_requested.emit(
        VodItem(vod_id="plugin-vod-1", vod_name="占位电影", vod_pic="poster-card")
    )

    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1, timeout=1000)
    _wait_for_request_call(qtbot, controller, "plugin-vod-1")

    controller.finish_request("plugin-vod-1", exc=RuntimeError("detail boom"))

    qtbot.waitUntil(lambda: window.player_window.logs[-1] == "详情加载失败: detail boom", timeout=1000)
    assert len(window.player_window.opened) == 1
    assert errors == []


def test_main_window_opens_remote_history_detail_asynchronously(qtbot, monkeypatch) -> None:
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

    window.open_history_detail(
        HistoryRecord(
            id=9,
            key="history-vod-1",
            vod_name="History Movie",
            vod_pic="",
            vod_remarks="Ep",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="remote",
        )
    )
    _wait_for_history_detail_call(qtbot, browse_controller, "history-vod-1")
    browse_controller.finish_detail("history-vod-1")

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)

    assert opened[0].vod.vod_name == "History Movie"
    assert opened[0].source_vod_id == "history-vod-1"


def test_main_window_opens_telegram_history_detail_asynchronously(qtbot, monkeypatch) -> None:
    controller = AsyncRequestController(lambda vod_id: _make_telegram_request(vod_id, vod_name="电报影视剧"))

    class BrowseController:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def build_request_from_detail(self, vod_id: str):
            self.calls.append(vod_id)
            return _make_telegram_request(vod_id, vod_name="不应走到这里")

    browse_controller = BrowseController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=controller,
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

    window.open_history_detail(
        HistoryRecord(
            id=9,
            key="1$125208$1",
            vod_name="电报影视剧",
            vod_pic="",
            vod_remarks="第1集",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="telegram",
            source_name="电报影视",
        )
    )
    _wait_for_request_call(qtbot, controller, "1$125208$1")
    controller.finish_request("1$125208$1", request=_make_telegram_request("1$125208$1", vod_name="电报影视剧"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)

    assert browse_controller.calls == []
    assert opened[0].vod.vod_name == "电报影视剧"
    assert opened[0].source_vod_id == "1$125208$1"


def test_main_window_opens_telegram_channel_history_detail_asynchronously(qtbot, monkeypatch) -> None:
    controller = FakeTelegramChannelController()

    class BrowseController:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def build_request_from_detail(self, vod_id: str):
            self.calls.append(vod_id)
            return _make_telegram_request(vod_id, vod_name="不应走到这里")

    browse_controller = BrowseController()
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        telegram_channel_controller=controller,
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

    window.open_history_detail(
        HistoryRecord(
            id=10,
            key="tg-channel-vod-1",
            vod_name="电报频道剧",
            vod_pic="",
            vod_remarks="第1集",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="telegram_channel",
            source_name="电报频道",
        )
    )

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)
    assert controller.build_calls == ["tg-channel-vod-1"]
    assert browse_controller.calls == []
    assert opened[0].vod.vod_name == "Telegram Channel Movie"
    assert opened[0].source_kind == "telegram_channel"
    assert opened[0].source_vod_id == "tg-channel-vod-1"


def test_main_window_opens_telegram_channel_favorite_record(qtbot, monkeypatch) -> None:
    controller = FakeTelegramChannelController()
    opened: list[OpenPlayerRequest] = []
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        telegram_channel_controller=controller,
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_open_favorite_placeholder", lambda record: None)
    monkeypatch.setattr(window, "_start_open_request", lambda builder: opened.append(builder()) or 1)

    window.open_favorite_detail(
        FavoriteRecord(
            source_kind="telegram_channel",
            source_key="",
            source_name="电报频道",
            vod_id="tg-channel-vod-1",
            vod_name_snapshot="频道视频",
            latest_vod_name="频道视频",
            vod_pic="",
            vod_remarks="",
            title_changed=False,
            created_at=10,
            updated_at=10,
        )
    )

    assert controller.build_calls == ["tg-channel-vod-1"]
    assert opened[0].source_kind == "telegram_channel"
    assert opened[0].source_vod_id == "tg-channel-vod-1"


def test_main_window_opens_direct_parse_history_detail_asynchronously(qtbot, monkeypatch) -> None:
    class FakeParserService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def resolve(self, flag: str, url: str, preferred_key: str = ""):
            self.calls.append((flag, url, preferred_key))
            return type(
                "Result",
                (),
                {
                    "parser_key": "jx1",
                    "url": "https://media.example/parsed.m3u8",
                    "headers": {"Referer": "https://site.example"},
                },
            )()

    def load_detail(url: str) -> dict:
        assert url == "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html"
        return {
            "vod_title": "剑来 第二季",
            "vod_form": "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html",
            "vod_episodes": [
                {"name": "第09话", "url": "https://v.qq.com/x/cover/mzc00200xxpsogl/y4101fhe180.html"},
                {"name": "第10话", "url": "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html"},
            ],
        }

    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(preferred_parse_key="jx1"),
        playback_parser_service=FakeParserService(),
        direct_parse_detail_loader=load_detail,
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[OpenPlayerRequest] = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "show_error", lambda message: None)

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html",
            vod_name="剑来 第二季",
            vod_pic="",
            vod_remarks="第10话",
            episode=1,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="direct_parse",
            source_name="全局解析",
        )
    )

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)

    assert opened[0].source_kind == "direct_parse"
    assert opened[0].source_vod_id == "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html"
    assert opened[0].clicked_index == 1
    assert opened[0].vod.vod_name == "剑来 第二季"


def test_main_window_opens_plugin_history_detail_asynchronously(qtbot, monkeypatch) -> None:
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
        spider_plugins=[{"id": 7, "title": "红果短剧", "controller": controller, "search_enabled": False}],
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[OpenPlayerRequest] = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "show_error", lambda message: None)

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="detail-1",
            vod_name="Plugin Movie",
            vod_pic="",
            vod_remarks="第1集",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="spider_plugin",
            source_plugin_id=7,
            source_plugin_name="红果短剧",
        )
    )
    _wait_for_request_call(qtbot, controller, "detail-1")
    controller.finish_request("detail-1", request=_make_telegram_request("detail-1", vod_name="插件电影"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)

    assert opened[0].vod.vod_name == "插件电影"
    assert opened[0].source_vod_id == "detail-1"
    assert opened[0].source_key == "7"


def test_main_window_opens_plugin_history_detail_with_record_episode_and_playlist_fallback(
    qtbot,
    monkeypatch,
) -> None:
    class RestoreAwarePluginController:
        def load_categories(self):
            return []

        def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
            del filters
            return [], 0

        def build_request(self, vod_id: str):
            first_group = [
                PlayItem(title="第1集", url="https://media.example/1-1.m3u8"),
                PlayItem(title="第2集", url="https://media.example/1-2.m3u8"),
            ]
            second_group = [
                PlayItem(title="第1集", url="https://media.example/2-1.m3u8"),
                PlayItem(title="第2集", url="https://media.example/2-2.m3u8"),
            ]
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="插件电影"),
                playlist=first_group,
                playlists=[first_group, second_group],
                playlist_index=0,
                clicked_index=0,
                source_kind="plugin",
                source_mode="detail",
                source_vod_id=vod_id,
            )

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
        spider_plugins=[{"id": 7, "title": "红果短剧", "controller": RestoreAwarePluginController(), "search_enabled": False}],
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[OpenPlayerRequest] = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "show_error", lambda message: None)

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="detail-1",
            vod_name="Plugin Movie",
            vod_pic="",
            vod_remarks="第2集",
            episode=1,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            playlist_index=1,
            source_kind="spider_plugin",
            source_plugin_id=7,
            source_plugin_name="红果短剧",
            source_key="7",
            source_name="红果短剧",
        )
    )

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)

    assert opened[0].playlist_index == 1
    assert opened[0].clicked_index == 1


def test_main_window_opens_plugin_history_detail_prefers_request_history_loader_over_record_fallback(
    qtbot,
    monkeypatch,
) -> None:
    class RestoreAwarePluginController:
        def load_categories(self):
            return []

        def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
            del filters
            return [], 0

        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="插件电影"),
                playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
                clicked_index=0,
                playlist_index=0,
                source_kind="plugin",
                source_mode="detail",
                source_vod_id=vod_id,
                playback_history_loader=lambda: HistoryRecord(
                    id=0,
                    key=vod_id,
                    vod_name="插件电影",
                    vod_pic="",
                    vod_remarks="第1集",
                    episode=0,
                    episode_url="https://media.example/1.m3u8",
                    position=45000,
                    opening=0,
                    ending=0,
                    speed=1.0,
                    create_time=1,
                ),
            )

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
        spider_plugins=[{"id": 7, "title": "红果短剧", "controller": RestoreAwarePluginController(), "search_enabled": False}],
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[OpenPlayerRequest] = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "show_error", lambda message: None)

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="detail-1",
            vod_name="Plugin Movie",
            vod_pic="",
            vod_remarks="第2集",
            episode=1,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            playlist_index=1,
            source_kind="spider_plugin",
            source_plugin_id=7,
            source_plugin_name="红果短剧",
            source_key="7",
            source_name="红果短剧",
        )
    )

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)

    assert opened[0].playback_history_loader is not None
    assert opened[0].playlist_index == 0
    assert opened[0].clicked_index == 0


def test_main_window_opens_emby_history_detail_asynchronously(qtbot, monkeypatch) -> None:
    controller = AsyncRequestController(lambda vod_id: _make_telegram_request(vod_id, vod_name="Emby Movie"))
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

    opened: list[OpenPlayerRequest] = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "show_error", lambda message: None)

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="emby-1",
            vod_name="Emby Movie",
            vod_pic="",
            vod_remarks="Episode 1",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="emby",
            source_name="Emby",
        )
    )
    _wait_for_request_call(qtbot, controller, "emby-1")
    controller.finish_request("emby-1", request=_make_telegram_request("emby-1", vod_name="Emby Movie"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)

    assert opened[0].vod.vod_name == "Emby Movie"
    assert opened[0].source_vod_id == "emby-1"


def test_main_window_opens_jellyfin_history_detail_asynchronously(qtbot, monkeypatch) -> None:
    controller = AsyncRequestController(lambda vod_id: _make_telegram_request(vod_id, vod_name="Jellyfin Movie"))
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

    opened: list[OpenPlayerRequest] = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "show_error", lambda message: None)

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="jf-1",
            vod_name="Jellyfin Movie",
            vod_pic="",
            vod_remarks="Episode 1",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="jellyfin",
            source_name="Jellyfin",
        )
    )
    _wait_for_request_call(qtbot, controller, "jf-1")
    controller.finish_request("jf-1", request=_make_telegram_request("jf-1", vod_name="Jellyfin Movie"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)

    assert opened[0].vod.vod_name == "Jellyfin Movie"
    assert opened[0].source_vod_id == "jf-1"


def test_main_window_opens_bilibili_history_detail_asynchronously(qtbot, monkeypatch) -> None:
    controller = AsyncRequestController(lambda vod_id: _make_telegram_request(vod_id, vod_name="B站视频"))
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        bilibili_controller=controller,
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        show_bilibili_tab=True,
    )
    qtbot.addWidget(window)
    window.show()

    opened: list[OpenPlayerRequest] = []
    monkeypatch.setattr(window, "open_player", lambda request, restore_paused_state=False: opened.append(request))
    monkeypatch.setattr(window, "show_error", lambda message: None)

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="BV1xx411c7mD",
            vod_name="B站视频",
            vod_pic="",
            vod_remarks="第1话",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="bilibili",
            source_name="B站",
        )
    )
    _wait_for_request_call(qtbot, controller, "BV1xx411c7mD")
    controller.finish_request("BV1xx411c7mD", request=_make_telegram_request("BV1xx411c7mD", vod_name="B站视频"))

    qtbot.waitUntil(lambda: len(opened) == 1, timeout=1000)

    assert opened[0].vod.vod_name == "B站视频"
    assert opened[0].source_vod_id == "BV1xx411c7mD"


def test_main_window_shows_error_when_plugin_history_source_is_missing(qtbot, monkeypatch) -> None:
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
        spider_plugins=[],
    )
    qtbot.addWidget(window)
    window.show()

    errors: list[str] = []
    monkeypatch.setattr(window, "show_error", lambda message: errors.append(message))

    window.open_history_detail(
        HistoryRecord(
            id=0,
            key="detail-1",
            vod_name="Plugin Movie",
            vod_pic="",
            vod_remarks="第1集",
            episode=0,
            episode_url="",
            position=0,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=1,
            source_kind="spider_plugin",
            source_plugin_id=999,
            source_plugin_name="失效插件",
        )
    )

    qtbot.waitUntil(lambda: errors == ["没有可播放的项目: 失效插件"], timeout=1000)


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

    app, repo, _service = app_module.build_application()

    assert app.application_name == "atv-player"
    assert not app.window_icon.isNull()
    assert (tmp_path / "app-data" / "app.db").exists()
    assert repo.load_config().base_url == "http://127.0.0.1:4567"


def test_build_application_ensures_app_identity(monkeypatch, tmp_path) -> None:
    class FakeApplication:
        def __init__(self, args) -> None:
            self.args = args
            self.application_name = ""
            self.window_icon = QIcon()

        def setApplicationName(self, name: str) -> None:
            self.application_name = name

        def setWindowIcon(self, icon: QIcon) -> None:
            self.window_icon = icon

    repo = app_module.SettingsRepository(tmp_path / "app.db")
    calls = {"count": 0}
    original_ensure = repo.ensure_app_identity

    def ensure_app_identity():
        calls["count"] += 1
        return original_ensure()

    repo.ensure_app_identity = ensure_app_identity

    monkeypatch.setattr(app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(app_module, "SettingsRepository", lambda _path: repo)
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    app_module.build_application()

    assert calls["count"] == 1


def test_build_application_passes_process_argv_to_qapplication(monkeypatch, tmp_path) -> None:
    captured = {"args": None}

    class FakeApplication:
        def __init__(self, args) -> None:
            captured["args"] = list(args)
            self.window_icon = QIcon()

        def setApplicationName(self, name: str) -> None:
            return None

        def setWindowIcon(self, icon: QIcon) -> None:
            self.window_icon = icon

    monkeypatch.setattr(app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(app_module.sys, "argv", ["atv-player", "--demo"])

    app_module.build_application()

    assert captured["args"] == ["atv-player", "--demo"]


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


def test_purge_stale_poster_cache_skips_recently_scanned_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    cache_dir = tmp_path / "app-cache" / "posters"
    cache_dir.mkdir(parents=True)
    old_file = cache_dir / "old.img"
    old_file.write_bytes(b"old")
    now = time.time()
    stale_age = now - (8 * 24 * 60 * 60)
    os.utime(old_file, (stale_age, stale_age))

    app_module.purge_stale_poster_cache(now=now)

    assert old_file.exists() is False

    second_old_file = cache_dir / "second-old.img"
    second_old_file.write_bytes(b"old")
    os.utime(second_old_file, (stale_age, stale_age))

    app_module.purge_stale_poster_cache(now=now + 60)

    assert second_old_file.exists() is True


def test_build_application_starts_async_danmaku_cache_cleanup(monkeypatch, tmp_path) -> None:
    class FakeApplication:
        def __init__(self, args) -> None:
            self.args = args
            self.application_name = ""
            self.window_icon = QIcon()

        def setApplicationName(self, name: str) -> None:
            self.application_name = name

        def setWindowIcon(self, icon: QIcon) -> None:
            self.window_icon = icon

    cleanup_calls: list[str] = []

    class FakeThread:
        def __init__(self, *, target, daemon: bool) -> None:
            self._target = target
            self.daemon = daemon

        def start(self) -> None:
            cleanup_calls.append("started")
            self._target()

    monkeypatch.setattr(app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(app_module, "purge_stale_danmaku_cache", lambda: cleanup_calls.append("cleaned"))
    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)

    app_module.build_application()

    assert cleanup_calls == ["started", "cleaned"]


def test_build_application_installs_crash_diagnostics(monkeypatch, tmp_path) -> None:
    class FakeApplication:
        def __init__(self, args) -> None:
            self.args = args
            self.application_name = ""
            self.window_icon = QIcon()

        def setApplicationName(self, name: str) -> None:
            self.application_name = name

        def setWindowIcon(self, icon: QIcon) -> None:
            self.window_icon = icon

    installed_paths: list[Path] = []

    monkeypatch.setattr(app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(app_module, "install_crash_diagnostics", lambda path: installed_paths.append(Path(path)))

    app_module.build_application()

    assert installed_paths == [tmp_path / "app-data" / "logs"]


def test_build_application_skips_crash_diagnostics_on_windows(monkeypatch, tmp_path) -> None:
    class FakeApplication:
        def __init__(self, args) -> None:
            self.args = args
            self.application_name = ""
            self.window_icon = QIcon()

        def setApplicationName(self, name: str) -> None:
            self.application_name = name

        def setWindowIcon(self, icon: QIcon) -> None:
            self.window_icon = icon

    installed_paths: list[Path] = []

    monkeypatch.setattr(app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(app_module, "install_crash_diagnostics", lambda path: installed_paths.append(Path(path)))
    monkeypatch.setattr(app_module.sys, "platform", "win32")

    app_module.build_application()

    assert installed_paths == []


def test_build_application_uses_main_thread_gc_timer_on_python_3_14(monkeypatch, tmp_path) -> None:
    class FakeSignal:
        def __init__(self) -> None:
            self.callbacks: list[object] = []

        def connect(self, callback) -> None:
            self.callbacks.append(callback)

    timer_state: dict[str, object] = {}

    class FakeTimer:
        def __init__(self, parent=None) -> None:
            timer_state["parent"] = parent
            self.timeout = FakeSignal()

        def setInterval(self, interval: int) -> None:
            timer_state["interval"] = interval

        def start(self) -> None:
            timer_state["started"] = True

    gc_calls: list[str] = []
    app = QApplication.instance() or QApplication([])

    monkeypatch.setattr(app_module, "QApplication", lambda args: app)
    monkeypatch.setattr(app_module, "QTimer", FakeTimer)
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(app_module.gc, "isenabled", lambda: True)
    monkeypatch.setattr(app_module.gc, "disable", lambda: gc_calls.append("disable"))
    monkeypatch.setattr(app_module.gc, "collect", lambda: gc_calls.append("collect"))
    monkeypatch.setattr(app_module.sys, "version_info", (3, 14, 0))

    app, _repo, _service = app_module.build_application()

    assert gc_calls == ["disable"]
    assert timer_state == {
        "parent": app,
        "interval": app_module._MAIN_THREAD_GC_INTERVAL_MS,
        "started": True,
    }
    assert getattr(app, "_main_thread_gc_timer").timeout.callbacks == [app_module.gc.collect]


def test_build_application_leaves_gc_enabled_before_python_3_14(monkeypatch, tmp_path) -> None:
    gc_calls: list[str] = []
    app = QApplication.instance() or QApplication([])
    if hasattr(app, "_main_thread_gc_timer"):
        delattr(app, "_main_thread_gc_timer")

    monkeypatch.setattr(app_module, "QApplication", lambda args: app)
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(app_module.gc, "disable", lambda: gc_calls.append("disable"))
    monkeypatch.setattr(app_module.sys, "version_info", (3, 13, 7))

    app, _repo, _service = app_module.build_application()

    assert gc_calls == []
    assert hasattr(app, "_main_thread_gc_timer") is False


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


def test_build_application_installs_pointing_hand_cursor_for_buttons(monkeypatch, tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    monkeypatch.setattr(app_module, "QApplication", lambda args: app)
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    built_app, _repo, _service = app_module.build_application()

    assert built_app is app

    push_button = QPushButton("Push")
    tool_button = QToolButton()
    push_button.show()
    tool_button.show()
    app.processEvents()

    assert push_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert tool_button.cursor().shape() == Qt.CursorShape.PointingHandCursor

    push_button.close()
    tool_button.close()


def test_build_application_does_not_change_non_button_cursor(monkeypatch, tmp_path) -> None:
    app = QApplication.instance() or QApplication([])

    monkeypatch.setattr(app_module, "QApplication", lambda args: app)
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    app_module.build_application()

    line_edit = QLineEdit()
    line_edit.show()
    app.processEvents()

    assert line_edit.cursor().shape() != Qt.CursorShape.PointingHandCursor

    line_edit.close()


def test_build_application_installs_theme_manager_from_saved_config(monkeypatch, tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(theme_mode="dark"))

    monkeypatch.setattr(app_module, "QApplication", lambda args: app)
    monkeypatch.setattr(app_module, "SettingsRepository", lambda _path: repo)
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path / "app-data")
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    built_app, built_repo, _service = app_module.build_application()

    assert built_app is app
    assert built_repo is repo
    assert hasattr(built_app, "_theme_manager")
    assert built_app.property("resolved_theme") == "dark"
    assert built_app.property("theme_mode") == "dark"


def test_apply_saved_theme_refreshes_main_window_without_recursive_callback(qtbot, tmp_path) -> None:
    app = QApplication.instance()
    assert app is not None
    app_module.install_theme(app, app_module.ThemeManager(system_theme_getter=lambda: "light"), "light")
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    config = AppConfig(theme_mode="dark")
    repo.save_config(config)

    window = MainWindow(
        douban_controller=FakeDoubanController(),
        telegram_controller=FakeTelegramController(),
        live_controller=FakeLiveController(),
        emby_controller=FakeEmbyController(),
        jellyfin_controller=FakeJellyfinController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
        apply_theme=lambda: app_module.apply_saved_theme(app, repo),
    )
    qtbot.addWidget(window)
    window.show()

    resolved = app_module.apply_saved_theme(app, repo)

    assert resolved == "dark"
    assert app.property("resolved_theme") == "dark"
    assert DARK_TOKENS.window_chrome_bg in window._window_chrome_root.styleSheet()


def test_apply_saved_theme_ignores_advanced_settings_callback_storage(qtbot, tmp_path) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    app = QApplication.instance()
    assert app is not None
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    config = AppConfig(theme_mode="dark")
    repo.save_config(config)

    dialog = AdvancedSettingsDialog(
        config,
        save_config=lambda: None,
        apply_theme=lambda: app_module.apply_saved_theme(app, repo),
    )
    qtbot.addWidget(dialog)
    dialog.show()

    resolved = app_module.apply_saved_theme(app, repo)

    assert resolved == "dark"
    assert app.property("resolved_theme") == "dark"


def test_advanced_settings_dialog_saves_youtube_max_height(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig()
    save_calls: list[int] = []
    dialog = AdvancedSettingsDialog(
        config,
        save_config=lambda: save_calls.append(config.youtube_max_height),
    )
    qtbot.addWidget(dialog)

    dialog.youtube_max_height_combo.setCurrentIndex(
        dialog.youtube_max_height_combo.findData(1080)
    )

    dialog._save()

    assert config.youtube_max_height == 1080
    assert save_calls == [1080]
    assert "1080P 及以下时通常启播更快" in dialog.youtube_scope_label.text()


def test_advanced_settings_dialog_defaults_youtube_quality_to_1080p(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.youtube_max_height_combo.currentData() == 1080


def test_advanced_settings_dialog_loads_dandan_base_url(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig(dandan_base_url="http://192.168.1.10:9321/87654321")
    dialog = AdvancedSettingsDialog(config, save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.dandan_base_url_edit.text() == "http://192.168.1.10:9321/87654321"


def test_advanced_settings_dialog_saves_dandan_base_url(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig()
    save_calls: list[str] = []
    dialog = AdvancedSettingsDialog(
        config,
        save_config=lambda: save_calls.append(config.dandan_base_url),
    )
    qtbot.addWidget(dialog)

    dialog.dandan_base_url_edit.setText("  http://host:9321  ")
    dialog._save()

    assert config.dandan_base_url == "http://host:9321"  # whitespace stripped
    assert save_calls == ["http://host:9321"]


def test_advanced_settings_dialog_test_button_requires_url(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig()
    dialog = AdvancedSettingsDialog(config, save_config=lambda: None)
    qtbot.addWidget(dialog)

    dialog.dandan_base_url_edit.setText("   ")
    dialog._test_dandan_server()

    assert dialog.dandan_test_status_label.text() == "请先填写服务器地址"
    assert dialog._dandan_test_running is False


def test_advanced_settings_dialog_saves_bilibili_grouped_playlist_tree_enabled(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig()
    save_calls: list[bool] = []
    dialog = AdvancedSettingsDialog(
        config,
        save_config=lambda: save_calls.append(config.bilibili_grouped_playlist_tree_enabled),
    )
    qtbot.addWidget(dialog)

    dialog.bilibili_grouped_playlist_tree_enabled_checkbox.setChecked(True)
    dialog._save()

    assert config.bilibili_grouped_playlist_tree_enabled is True
    assert save_calls == [True]


def test_advanced_settings_dialog_restores_bilibili_grouped_playlist_tree_enabled(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(
        AppConfig(bilibili_grouped_playlist_tree_enabled=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(dialog)

    assert dialog.bilibili_grouped_playlist_tree_enabled_checkbox.isChecked() is True


def test_advanced_settings_dialog_saves_ai_provider_settings(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig()
    saved: list[tuple[bool, str, str, str, int, bool, bool, bool, bool]] = []
    dialog = AdvancedSettingsDialog(
        config,
        save_config=lambda: saved.append(
            (
                config.ai_enabled,
                config.ai_base_url,
                config.ai_api_key,
                config.ai_chat_model,
                config.ai_request_timeout_seconds,
                config.ai_metadata_enrichment_enabled,
                config.ai_danmaku_enrichment_enabled,
                config.ai_episode_title_rewrite_enabled,
                config.ai_following_summary_enabled,
            )
        ),
    )
    qtbot.addWidget(dialog)

    dialog.ai_enabled_checkbox.setChecked(True)
    dialog.ai_base_url_edit.setText(" https://api.example.com/v1/ ")
    dialog.ai_api_key_edit.setText(" sk-test ")
    dialog.ai_chat_model_combo.setEditText(" gpt-4o-mini ")
    dialog.ai_timeout_edit.setText("45")
    dialog.ai_metadata_enrichment_checkbox.setChecked(False)
    dialog.ai_danmaku_enrichment_checkbox.setChecked(True)
    dialog.ai_episode_title_rewrite_checkbox.setChecked(False)
    dialog.ai_following_summary_checkbox.setChecked(True)
    dialog._save()

    assert saved == [
        (
            True,
            "https://api.example.com/v1/",
            "sk-test",
            "gpt-4o-mini",
            45,
            False,
            True,
            False,
            True,
        )
    ]


def test_advanced_settings_dialog_loads_ai_models(qtbot, monkeypatch) -> None:
    from atv_player.ui import advanced_settings_dialog as module
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    messages: list[tuple[str, str]] = []
    fake_client = FakeAISettingsClient(models=["gpt-4o-mini", "gpt-4.1-mini"])
    dialog = AdvancedSettingsDialog(
        AppConfig(
            ai_base_url="https://api.example.com",
            ai_api_key="sk-test",
            ai_chat_model="manual-model",
        ),
        save_config=lambda: None,
        ai_client_factory=lambda config: fake_client,
    )
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        module.QMessageBox,
        "information",
        lambda _parent, title, text: messages.append((title, text)),
    )

    dialog._load_ai_models()

    assert fake_client.list_model_calls == 1
    assert dialog.ai_chat_model_combo.currentText() == "manual-model"
    assert [
        dialog.ai_chat_model_combo.itemText(index)
        for index in range(dialog.ai_chat_model_combo.count())
    ] == ["manual-model", "gpt-4o-mini", "gpt-4.1-mini"]
    assert messages == [("AI 模型列表", "已拉取 2 个模型")]


def test_advanced_settings_dialog_checks_ai_connectivity(qtbot, monkeypatch) -> None:
    from atv_player.ui import advanced_settings_dialog as module
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    messages: list[tuple[str, str]] = []
    fake_client = FakeAISettingsClient()
    dialog = AdvancedSettingsDialog(
        AppConfig(
            ai_base_url="https://api.example.com",
            ai_api_key="sk-test",
            ai_chat_model="gpt-4o-mini",
        ),
        save_config=lambda: None,
        ai_client_factory=lambda config: fake_client,
    )
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        module.QMessageBox,
        "information",
        lambda _parent, title, text: messages.append((title, text)),
    )
    times = iter([10.0, 11.234])
    monkeypatch.setattr(module.time, "perf_counter", lambda: next(times))

    dialog._check_ai_connectivity()

    assert fake_client.connectivity_calls == 1
    assert messages == [("AI 连通性", "连接正常，用时 1234 ms")]


def test_advanced_settings_dialog_shows_ai_connectivity_error(qtbot, monkeypatch) -> None:
    from atv_player.ui import advanced_settings_dialog as module
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    warnings: list[tuple[str, str]] = []
    fake_client = FakeAISettingsClient(error=OpenAICompatibleError("bad key [redacted]"))
    dialog = AdvancedSettingsDialog(
        AppConfig(
            ai_base_url="https://api.example.com",
            ai_api_key="sk-test",
            ai_chat_model="gpt-4o-mini",
        ),
        save_config=lambda: None,
        ai_client_factory=lambda config: fake_client,
    )
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        module.QMessageBox,
        "warning",
        lambda _parent, title, text: warnings.append((title, text)),
    )
    times = iter([20.0, 20.045])
    monkeypatch.setattr(module.time, "perf_counter", lambda: next(times))

    dialog._check_ai_connectivity()

    assert fake_client.connectivity_calls == 1
    assert warnings == [("AI 连通性失败", "bad key [redacted]\n用时 45 ms")]


def test_settings_repository_persists_ai_enrichment_workflow_toggles(tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "settings.db")
    config = repo.load_config()
    config.ai_metadata_enrichment_enabled = False
    config.ai_danmaku_enrichment_enabled = False
    config.ai_episode_title_rewrite_enabled = True
    config.ai_following_summary_enabled = False

    repo.save_config(config)
    loaded = repo.load_config()

    assert loaded.ai_metadata_enrichment_enabled is False
    assert loaded.ai_danmaku_enrichment_enabled is False
    assert loaded.ai_episode_title_rewrite_enabled is True
    assert loaded.ai_following_summary_enabled is False


def test_advanced_settings_dialog_masks_ai_api_key(qtbot) -> None:
    from PySide6.QtWidgets import QLineEdit

    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    dialog = AdvancedSettingsDialog(AppConfig(ai_api_key="sk-test"), save_config=lambda: None)
    qtbot.addWidget(dialog)

    assert dialog.ai_api_key_edit.text() == "sk-test"
    assert dialog.ai_api_key_edit.echoMode() == QLineEdit.EchoMode.Password


def test_app_coordinator_injects_smart_search_controller_when_ai_enabled(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class FakeSignal:
        def connect(self, callback) -> None:
            del callback

    class CapturingMainWindow:
        def __init__(self, *args, **kwargs) -> None:
            captured.update(kwargs)
            self.logout_requested = FakeSignal()

        def show(self) -> None:
            pass

    monkeypatch.setattr(app_module, "MainWindow", CapturingMainWindow)
    monkeypatch.setattr(
        app_module,
        "ApiClient",
        lambda *args, **kwargs: ApiClient(
            "http://127.0.0.1:4567",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"token": "vod-123"})),
        ),
    )
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())
    monkeypatch.setattr(
        app_module,
        "SpiderPluginManager",
        lambda repository, loader, playback_history_repository, **_kwargs: FakePluginManager(),
    )
    monkeypatch.setattr(app_module, "LocalPlaybackHistoryRepository", lambda db_path: object())
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    repo.save_config(
        AppConfig(
            token="token",
            ai_enabled=True,
            ai_base_url="https://api.example.com/v1",
            ai_api_key="sk-test",
            ai_chat_model="model-a",
        )
    )
    coordinator = app_module.AppCoordinator(repo)

    coordinator._show_main()

    assert captured["smart_search_controller"] is not None


def test_app_coordinator_builds_ai_enrichment_service_when_configured(tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "settings.db")
    config = repo.load_config()
    config.ai_enabled = True
    config.ai_base_url = "https://api.example.com"
    config.ai_api_key = "key"
    config.ai_chat_model = "model"
    repo.save_config(config)
    coordinator = app_module.AppCoordinator(repo)

    service = coordinator._build_ai_enrichment_service(config)

    assert service is not None


def test_app_coordinator_skips_ai_enrichment_service_for_disabled_workflow(tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "settings.db")
    config = repo.load_config()
    config.ai_enabled = True
    config.ai_base_url = "https://api.example.com"
    config.ai_api_key = "key"
    config.ai_chat_model = "model"
    config.ai_danmaku_enrichment_enabled = False
    repo.save_config(config)
    coordinator = app_module.AppCoordinator(repo)

    assert coordinator._build_ai_enrichment_service(config, workflow="danmaku") is None
    assert coordinator._build_ai_enrichment_service(config, workflow="metadata") is not None


def test_app_coordinator_skips_ai_enrichment_service_when_incomplete(tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "settings.db")
    config = repo.load_config()
    config.ai_enabled = True
    config.ai_base_url = ""
    config.ai_api_key = "key"
    config.ai_chat_model = "model"
    repo.save_config(config)
    coordinator = app_module.AppCoordinator(repo)

    assert coordinator._build_ai_enrichment_service(config) is None


def test_build_application_installs_app_log_service(monkeypatch, tmp_path) -> None:
    created: dict[str, object] = {}

    class FakeService:
        def __init__(self, logs_dir, *, enabled_getter, max_bytes, max_archives) -> None:
            created["logs_dir"] = logs_dir
            created["enabled"] = enabled_getter()
            created["max_bytes"] = max_bytes
            created["max_archives"] = max_archives
            self.active_path = logs_dir / "application.jsonl"

        def write_event(self, event) -> None:
            created["last_event"] = event

    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(app_module, "AppLogService", FakeService, raising=False)

    app, repo, service = app_module.build_application()

    assert created["logs_dir"] == tmp_path / "logs"
    assert created["enabled"] is True
    assert created["max_bytes"] == 10 * 1024 * 1024
    assert created["max_archives"] == 5
    assert service is not None
    assert getattr(app, "_app_log_service", None) is None
    assert repo.load_config().logging_enabled is True


def test_app_coordinator_reconfigures_logging_with_structured_handler(monkeypatch, tmp_path) -> None:
    repo = app_module.SettingsRepository(tmp_path / "app.db")
    repo.save_config(AppConfig(logging_enabled=False))

    configure_calls: list[tuple[str, object]] = []

    def record_configure(level: str, structured_handler=None) -> None:
        configure_calls.append((level, structured_handler))

    class FakeService:
        def write_event(self, event) -> None:
            del event

    monkeypatch.setattr(app_module, "configure_logging", record_configure, raising=False)
    monkeypatch.setattr(app_module.AppCoordinator, "_show_login", lambda self, error_message="": QDialog())

    coordinator = app_module.AppCoordinator(repo, app_log_service=FakeService())
    coordinator.start()

    assert configure_calls[-1][0] == "INFO"
    assert configure_calls[-1][1] is not None


def test_app_coordinator_show_main_passes_app_log_service_to_main_window(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
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

    captured_services: list[object] = []

    class FakeMainWindow:
        logout_requested = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, **kwargs) -> None:
            captured_services.append(kwargs["app_log_service"])

    service = object()
    repo = FakeRepo()
    coordinator = AppCoordinator(repo, app_log_service=service)

    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(coordinator, "_build_api_client", lambda: object())
    monkeypatch.setattr(coordinator, "_load_capabilities", lambda client: {"emby": False, "jellyfin": False})

    coordinator._show_main()

    assert captured_services == [service]


def test_app_coordinator_start_does_not_require_vod_root_probe(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
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


def test_app_coordinator_uses_username_as_vod_token_for_normal_user(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def __init__(self) -> None:
            self.config = AppConfig(username="alice", token="auth-123", vod_token="old-token")

        def load_config(self) -> AppConfig:
            return self.config

        def save_config(self, config: AppConfig) -> None:
            self.config = config

    class FakeApiClient:
        def __init__(self) -> None:
            self.vod_token = ""

        def get_vod_token_info(self) -> dict[str, object]:
            return {"tokens": ["web", "shared"], "role": "USER"}

        def set_vod_token(self, value: str) -> None:
            self.vod_token = value

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)
    client = FakeApiClient()

    assert coordinator._ensure_vod_token(client) == "alice"
    assert client.vod_token == "alice"
    assert repo.config.vod_token == "alice"
    assert coordinator._vod_token_is_admin is False


def test_app_coordinator_prompts_admin_to_choose_vod_token_after_login(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def __init__(self) -> None:
            self.config = AppConfig(base_url="http://demo", username="admin", token="auth-123")

        def load_config(self) -> AppConfig:
            return self.config

        def save_config(self, config: AppConfig) -> None:
            self.config = config

    class FakeApiClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.closed = False

        def get_vod_token_info(self) -> dict[str, object]:
            return {"tokens": ["web", "Harold", "115"], "role": "ADMIN"}

        def close(self) -> None:
            self.closed = True

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)
    coordinator.login_window = object()
    monkeypatch.setattr(app_module, "ApiClient", FakeApiClient)
    monkeypatch.setattr(
        app_module.QInputDialog,
        "getItem",
        lambda parent, title, label, options, current, editable: (
            "Harold",
            parent is coordinator.login_window
            and title == "选择 VOD Token"
            and label == "请选择要使用的 VOD Token："
            and options == ["web", "Harold", "115"]
            and current == 0
            and editable is False,
        ),
    )

    assert coordinator._select_vod_token_after_login() is True
    assert repo.config.vod_token == "Harold"
    assert coordinator._vod_token_options == ["web", "Harold", "115"]
    assert coordinator._vod_token_is_admin is True


def test_advanced_settings_dialog_allows_admin_to_change_vod_token(qtbot) -> None:
    from atv_player.ui.advanced_settings_dialog import AdvancedSettingsDialog

    config = AppConfig(vod_token="web")
    saved: list[str] = []
    applied: list[str] = []
    dialog = AdvancedSettingsDialog(
        config,
        save_config=lambda: saved.append(config.vod_token),
        vod_token_options=["web", "Harold", "115"],
        can_select_vod_token=True,
        on_vod_token_changed=applied.append,
    )
    qtbot.addWidget(dialog)

    assert dialog.vod_token_combo.currentData() == "web"
    assert dialog.vod_token_group.title() == "VOD Token"
    assert dialog.vod_token_group.layout().labelForField(dialog.vod_token_combo).text() == "当前 Token"
    assert dialog.vod_token_combo.styleSheet() == dialog.home_mode_combo.styleSheet()
    dialog.vod_token_combo.setCurrentIndex(1)
    dialog._save()

    assert config.vod_token == "Harold"
    assert saved == ["Harold"]
    assert applied == ["Harold"]


def test_app_coordinator_start_returns_login_window_when_vod_token_fetch_raises_api_error(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
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
    class FakeRepo(_FakeRepoBase):
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

        def get_text(self, url: str) -> str:
            del url
            return ""

        def get_text(self, url: str) -> str:
            del url
            return ""

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


def test_app_coordinator_passes_shared_m3u8_filter_into_main_window(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
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

    captured_filters: list[object] = []

    class DummyMainWindow:
        logout_requested = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, **kwargs) -> None:
            captured_filters.append(kwargs["m3u8_ad_filter"])

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)

    monkeypatch.setattr(app_module, "MainWindow", DummyMainWindow)
    monkeypatch.setattr(coordinator, "_build_api_client", lambda: object())
    monkeypatch.setattr(coordinator, "_load_capabilities", lambda client: {"emby": False, "jellyfin": False})

    coordinator._show_main()

    assert captured_filters[0] is coordinator._m3u8_ad_filter


def test_app_coordinator_passes_saved_m3u_proxy_segment_prefetch_size_to_local_hls_proxy_server(monkeypatch) -> None:
    captured: list[int] = []

    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(m3u_proxy_segment_prefetch_size=7)

    class DummyProxyServer:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs["segment_prefetch_size"])

    class DummyFilter:
        def __init__(self, proxy_server, get) -> None:
            self.proxy_server = proxy_server
            self.get = get

    monkeypatch.setattr(app_module, "LocalHlsProxyServer", DummyProxyServer)
    monkeypatch.setattr(app_module, "M3U8AdFilter", DummyFilter)

    app_module.AppCoordinator(FakeRepo())

    assert captured == [7]


def test_app_coordinator_show_main_save_config_updates_live_proxy_prefetch_size(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def __init__(self) -> None:
            self.config = AppConfig(
                base_url="http://127.0.0.1:4567",
                username="alice",
                token="auth-123",
                vod_token="vod-123",
                m3u_proxy_segment_prefetch_size=2,
            )

        def load_config(self) -> AppConfig:
            return self.config

        def save_config(self, config: AppConfig) -> None:
            self.config = config

        def clear_token(self) -> None:
            self.config.token = ""
            self.config.vod_token = ""

    class FakeMainWindow:
        logout_requested = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, **kwargs) -> None:
            config = kwargs["config"]
            config.m3u_proxy_segment_prefetch_size = 6
            kwargs["save_config"]()

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)

    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(coordinator, "_build_api_client", lambda: object())
    monkeypatch.setattr(coordinator, "_load_capabilities", lambda client: {"emby": False, "jellyfin": False})

    coordinator._show_main()

    assert repo.config.m3u_proxy_segment_prefetch_size == 6
    assert coordinator._m3u8_ad_filter._proxy_server._segment_proxy._segment_prefetch_size == 6


def test_app_coordinator_closes_m3u8_filter_when_shutting_down() -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig()

    coordinator = AppCoordinator(FakeRepo())

    class DummyFilter:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    coordinator._m3u8_ad_filter = DummyFilter()
    coordinator.close()

    assert coordinator._m3u8_ad_filter.closed is True


def test_app_coordinator_show_main_starts_async_player_restore_when_supported(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
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
    class FakeRepo(_FakeRepoBase):
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
            return {"emby": False, "jellyfin": True, "bilibili": True, "pansou": False}

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

    coordinator._yt_dlp_service = SimpleNamespace(is_available=lambda: False)
    window = coordinator._show_main()

    assert isinstance(window, FakeMainWindow)
    assert window.kwargs["show_emby_tab"] is False
    assert window.kwargs["show_jellyfin_tab"] is True
    assert window.kwargs["show_bilibili_tab"] is True
    assert window.kwargs["show_youtube_tab"] is False

    coordinator._yt_dlp_service = SimpleNamespace(is_available=lambda: True)
    youtube_window = coordinator._show_main()

    assert youtube_window.kwargs["show_youtube_tab"] is True
    assert youtube_window.kwargs["youtube_controller"] is not None


def test_app_coordinator_show_main_injects_pansou_controller_when_capability_enabled(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
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
            return {"emby": False, "jellyfin": False, "feiniu": False, "pansou": True}

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
    assert window.kwargs["pansou_controller"] is not None


def test_app_coordinator_show_main_injects_shared_local_playback_history_repository(monkeypatch, tmp_path) -> None:
    class FakeRepo(_FakeRepoBase):
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

        def get_capabilities(self) -> dict[str, bool]:
            return {"emby": True, "jellyfin": True, "feiniu": True, "bilibili": True}

    captured: dict[str, object] = {}

    class RecordingSpiderPluginManager:
        def __init__(
            self,
            repository,
            loader,
            playback_history_repository=None,
            **_kwargs,
        ) -> None:
            captured["plugin_repository"] = playback_history_repository

        def load_enabled_plugins(self, drive_detail_loader=None):
            return []

    class RecordingEmbyController:
        def __init__(self, api_client, playback_history_loader=None, playback_history_saver=None) -> None:
            captured["emby_loader"] = playback_history_loader
            captured["emby_saver"] = playback_history_saver

    class RecordingTelegramController:
        def __init__(self, api_client, playback_history_loader=None, playback_history_saver=None) -> None:
            captured["telegram_loader"] = playback_history_loader
            captured["telegram_saver"] = playback_history_saver

    class RecordingJellyfinController:
        def __init__(self, api_client, playback_history_loader=None, playback_history_saver=None) -> None:
            captured["jellyfin_loader"] = playback_history_loader
            captured["jellyfin_saver"] = playback_history_saver

    class RecordingFeiniuController:
        def __init__(self, api_client, playback_history_loader=None, playback_history_saver=None) -> None:
            captured["feiniu_loader"] = playback_history_loader
            captured["feiniu_saver"] = playback_history_saver

    class RecordingBilibiliController:
        def __init__(self, api_client, playback_history_loader=None, playback_history_saver=None) -> None:
            captured["bilibili_loader"] = playback_history_loader
            captured["bilibili_saver"] = playback_history_saver

    class RecordingHistoryController:
        def __init__(self, api_client, playback_history_repository=None) -> None:
            captured["history_repository"] = playback_history_repository

    class DummyMainWindow:
        logout_requested = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, **kwargs) -> None:
            captured["window_kwargs"] = kwargs

    monkeypatch.setattr(app_module, "ApiClient", FakeApiClient)
    monkeypatch.setattr(app_module, "SpiderPluginRepository", lambda db_path: object())
    monkeypatch.setattr(app_module, "SpiderPluginLoader", lambda cache_dir: object())
    monkeypatch.setattr(app_module, "SpiderPluginManager", RecordingSpiderPluginManager)
    monkeypatch.setattr(app_module, "TelegramSearchController", RecordingTelegramController)
    monkeypatch.setattr(app_module, "EmbyController", RecordingEmbyController)
    monkeypatch.setattr(app_module, "JellyfinController", RecordingJellyfinController)
    monkeypatch.setattr(app_module, "FeiniuController", RecordingFeiniuController)
    monkeypatch.setattr(app_module, "BilibiliController", RecordingBilibiliController)
    monkeypatch.setattr(app_module, "HistoryController", RecordingHistoryController)
    monkeypatch.setattr(app_module, "MainWindow", DummyMainWindow)

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)
    monkeypatch.setattr(coordinator, "_build_api_client", lambda: FakeApiClient(repo.config.base_url, repo.config.token, repo.config.vod_token))
    monkeypatch.setattr(coordinator, "_start_live_background_refresh", lambda *args: None)

    coordinator._show_main()

    shared_repo = captured["history_repository"]
    assert shared_repo is not None
    assert captured["plugin_repository"] is shared_repo
    assert callable(captured["telegram_loader"])
    assert callable(captured["telegram_saver"])
    assert callable(captured["emby_loader"])
    assert callable(captured["emby_saver"])
    assert callable(captured["jellyfin_loader"])
    assert callable(captured["jellyfin_saver"])
    assert callable(captured["feiniu_loader"])
    assert callable(captured["feiniu_saver"])
    assert callable(captured["bilibili_loader"])
    assert callable(captured["bilibili_saver"])
    assert captured["window_kwargs"]["show_feiniu_tab"] is True
    assert captured["window_kwargs"]["show_bilibili_tab"] is True


def test_app_coordinator_show_main_wires_metadata_hydrator_factory(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
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

        def get_text(self, url: str) -> str:
            del url
            return ""

    captured: dict[str, object] = {}

    class FakeMainWindow:
        logout_requested = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, **kwargs) -> None:
            captured["window_kwargs"] = kwargs

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)
    marker = object()
    plugin_manager = SimpleNamespace(
        load_enabled_plugins=lambda drive_detail_loader=None, offline_download_detail_loader=None: [],
        _metadata_hydrator_factory=None,
    )
    coordinator._plugin_manager = plugin_manager

    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(
        coordinator,
        "_build_api_client",
        lambda: FakeApiClient(repo.config.base_url, repo.config.token, repo.config.vod_token),
    )
    monkeypatch.setattr(coordinator, "_start_live_background_refresh", lambda *args: None)
    monkeypatch.setattr(coordinator, "_build_metadata_hydrator_factory", lambda api_client: marker, raising=False)

    coordinator._show_main()

    assert captured["window_kwargs"]["metadata_hydrator_factory"] is marker
    assert plugin_manager._metadata_hydrator_factory is marker


def test_app_coordinator_show_main_wires_favorites_controller(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def __init__(self) -> None:
            self.config = AppConfig(
                base_url="http://127.0.0.1:4567",
                username="alice",
                token="auth-123",
                vod_token="vod-123",
            )
            self.database_path = Path("/tmp/app.db")

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

        def get_text(self, url: str) -> str:
            del url
            return ""

    captured: dict[str, object] = {}

    class FakeMainWindow:
        logout_requested = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, **kwargs) -> None:
            captured["window_kwargs"] = kwargs

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)
    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(
        coordinator,
        "_build_api_client",
        lambda: FakeApiClient(repo.config.base_url, repo.config.token, repo.config.vod_token),
    )
    monkeypatch.setattr(coordinator, "_start_live_background_refresh", lambda *args: None)

    coordinator._show_main()

    assert captured["window_kwargs"]["favorites_controller"] is not None


def test_app_coordinator_show_main_wires_following_controller(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def __init__(self) -> None:
            self.config = AppConfig(
                base_url="http://127.0.0.1:4567",
                username="alice",
                token="auth-123",
                vod_token="vod-123",
            )
            self.database_path = Path("/tmp/app.db")

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

        def get_text(self, url: str) -> str:
            del url
            return ""

    captured: dict[str, object] = {}

    class FakeMainWindow:
        logout_requested = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, **kwargs) -> None:
            captured["window_kwargs"] = kwargs

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)
    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(
        coordinator,
        "_build_api_client",
        lambda: FakeApiClient(repo.config.base_url, repo.config.token, repo.config.vod_token),
    )
    monkeypatch.setattr(coordinator, "_start_live_background_refresh", lambda *args: None)

    coordinator._show_main()

    assert captured["window_kwargs"]["following_controller"] is not None
    assert captured["window_kwargs"]["following_update_service"] is not None


def test_app_danmaku_controller_factory_supports_browse_and_msub_sources(monkeypatch) -> None:
    # 文件浏览（browse）此前不在弹幕控制器工厂的白名单里：会话没有弹幕控制器，
    # 对话框集数无人计算、搜索无法执行。browse 与 telegram 等一样按标题+集数搜索，
    # 应当获得 GenericDanmakuController；服务端追剧同样依赖标题+集数搜索。
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig()

    coordinator = AppCoordinator(FakeRepo())
    coordinator._danmaku_service = object()
    factory = coordinator._build_danmaku_controller_factory()

    controller = factory(
        source_kind="browse",
        vod=VodItem(vod_id="1$190089$1", vod_name="X 心动的XH9"),
    )

    assert isinstance(controller, app_module.GenericDanmakuController)
    assert isinstance(
        factory(
            source_kind="msub",
            vod=VodItem(vod_id="msub:38", vod_name="仙剑奇侠传三"),
        ),
        app_module.GenericDanmakuController,
    )
    assert factory(source_kind="youtube", vod=VodItem(vod_id="yt:1", vod_name="视频")) is None


def test_app_coordinator_show_main_wires_danmaku_controller_factory(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
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

    captured: dict[str, object] = {}

    class FakeMainWindow:
        logout_requested = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, **kwargs) -> None:
            captured["window_kwargs"] = kwargs

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)
    marker = object()
    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(
        coordinator,
        "_build_api_client",
        lambda: FakeApiClient(repo.config.base_url, repo.config.token, repo.config.vod_token),
    )
    monkeypatch.setattr(coordinator, "_start_live_background_refresh", lambda *args: None)
    monkeypatch.setattr(coordinator, "_build_danmaku_controller_factory", lambda: marker, raising=False)

    coordinator._show_main()

    assert captured["window_kwargs"]["danmaku_controller_factory"] is marker


def test_app_coordinator_show_main_wires_episode_title_enhancer_factory(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
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

    captured: dict[str, object] = {}

    class FakeMainWindow:
        logout_requested = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, **kwargs) -> None:
            captured["window_kwargs"] = kwargs

    repo = FakeRepo()
    coordinator = AppCoordinator(repo)
    marker = object()
    plugin_manager = SimpleNamespace(
        load_enabled_plugins=lambda drive_detail_loader=None, offline_download_detail_loader=None: [],
        _episode_title_enhancer_factory=None,
    )
    coordinator._plugin_manager = plugin_manager

    monkeypatch.setattr(app_module, "MainWindow", FakeMainWindow)
    monkeypatch.setattr(
        coordinator,
        "_build_api_client",
        lambda: FakeApiClient(repo.config.base_url, repo.config.token, repo.config.vod_token),
    )
    monkeypatch.setattr(coordinator, "_start_live_background_refresh", lambda *args: None)
    monkeypatch.setattr(coordinator, "_build_episode_title_enhancer_factory", lambda api_client: marker, raising=False)

    coordinator._show_main()

    assert captured["window_kwargs"]["episode_title_enhancer_factory"] is marker
    assert plugin_manager._episode_title_enhancer_factory is marker


def test_app_coordinator_builds_episode_title_enhancer_only_when_switch_and_tmdb_key_are_enabled(tmp_path, monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    coordinator = AppCoordinator(FakeRepo())
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(source_kind="plugin", vod=VodItem(vod_id="v1", vod_name="深空彼岸"))

    assert callable(enhance)


def test_app_coordinator_builds_episode_title_enhancer_for_browse_when_enabled(tmp_path, monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    coordinator = AppCoordinator(FakeRepo())
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(source_kind="browse", vod=VodItem(vod_id="v1", vod_name="深空彼岸"))

    assert callable(enhance)


@pytest.mark.parametrize("source_kind", ["telegram", "emby", "jellyfin", "feiniu"])
def test_app_coordinator_builds_episode_title_enhancer_for_supported_remote_sources(
    tmp_path,
    monkeypatch,
    source_kind: str,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    coordinator = AppCoordinator(FakeRepo())
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(source_kind=source_kind, vod=VodItem(vod_id="v1", vod_name="深空彼岸"))

    assert callable(enhance)


def test_app_coordinator_episode_title_enhancer_maps_shuffled_playlist_by_episode_marker(tmp_path, monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            assert title == "超能路人甲"
            assert year == "2026"
            return [{"id": 42, "name": "超能路人甲", "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            assert tmdb_id == "42"
            assert season_number == 1
            return {
                "episodes": [
                    {"episode_number": 1, "name": "星门初启"},
                    {"episode_number": 2, "name": "星火初燃"},
                ]
            }

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="超能路人甲", vod_year="2026", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="超能路人甲", vod_year="2026", category_name="电视剧"),
            playlist=[
                PlayItem(title="S01E02.mkv", url="http://m/2.mp4", original_title="S01E02.mkv"),
                PlayItem(title="S01E01.mkv", url="http://m/1.mp4", original_title="S01E01.mkv"),
            ],
        )
    )

    assert updated is not None
    assert [item.episode_display_title for item in updated] == ["第1集 星门初启", "第2集 星火初燃"]
    assert [item.original_title for item in updated] == ["S01E01.mkv", "S01E02.mkv"]


def test_app_coordinator_episode_title_enhancer_uses_query_redirect_title(
    tmp_path,
    monkeypatch,
) -> None:
    # 网盘混淆目录名（"X 心动的XH9"）在 TMDB 搜不到分集标题；保存的查询重定向
    # （用户在刮削对话框修正过标题）应当把增强器的搜索标题换成修正后的剧名，
    # 且重定向行本身不能被当作 provider 绑定候选。
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            del api_key, proxy_decider
            self.queries: list[tuple[str, str]] = []

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            self.queries.append((title, year))
            if title == "心动的信号":
                return [{"id": 42, "name": "心动的信号 第九季", "first_air_date": "2026-01-01"}]
            return []

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            assert tmdb_id == "42"
            return {
                "episodes": [
                    {"episode_number": 1, "name": "心动开局"},
                    {"episode_number": 2, "name": "初次约会"},
                ]
            }

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    bindings = MetadataBindingRepository(tmp_path / "bindings.db")
    bindings.save_query_redirect("X 心动的XH9", "", "心动的信号 第九季", "")
    monkeypatch.setattr(coordinator, "_metadata_binding_repository", bindings)
    factory = coordinator._build_episode_title_enhancer_factory(object())
    vod = VodItem(vod_id="v1", vod_name="X 心动的XH9")
    enhance = factory(source_kind="plugin", vod=vod)

    updated = enhance(
        SimpleNamespace(
            vod=vod,
            playlist=[
                PlayItem(title="S01E01.mkv", url="http://m/1.mp4", original_title="S01E01.mkv"),
                PlayItem(title="S01E02.mkv", url="http://m/2.mp4", original_title="S01E02.mkv"),
            ],
        )
    )

    assert updated is not None
    assert [item.episode_display_title for item in updated] == ["第1集 心动开局", "第2集 初次约会"]


def test_app_coordinator_episode_title_enhancer_preserves_variety_playlist_order(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            assert title == "开始推理吧"
            assert year == ""
            return [{"id": 42, "name": "开始推理吧", "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(
            self,
            tmdb_id: str | int,
            season_number: int,
        ) -> dict[str, object]:
            assert tmdb_id == "42"
            assert season_number == 4
            return {
                "episodes": [
                    {"episode_number": 1, "name": "初入推理世界"},
                    {"episode_number": 2, "name": "第二个谜案"},
                ]
            }

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    vod = VodItem(
        vod_id="v1",
        vod_name="开始推理吧 第4季",
        vod_year="2026",
        type_name="剧情,爱情",
    )
    enhance = factory(source_kind="telegram", vod=vod)
    original_titles = [
        "2026-05-20 序章.mp4",
        "2026-05-26 《副本解锁中》第1期.mp4",
        "2026-06-02 《副本解锁中》第2期.mp4",
        "2026-05-27 第1期上.mp4",
    ]

    updated = enhance(
        SimpleNamespace(
            vod=vod,
            playlist=[
                PlayItem(title=title, url=f"http://m/{index}.mp4", original_title=title)
                for index, title in enumerate(original_titles)
            ],
        )
    )

    assert updated is not None
    assert [item.original_title for item in updated] == original_titles
    assert sum(bool(item.episode_display_title) for item in updated) >= 3

    cached = enhance(
        SimpleNamespace(
            vod=vod,
            playlist=[
                PlayItem(title=title, url=f"http://m/{index}.mp4", original_title=title)
                for index, title in enumerate(original_titles)
            ],
        )
    )

    assert cached is not None
    assert [item.original_title for item in cached] == original_titles


def test_app_coordinator_episode_title_enhancer_maps_multi_season_playlist(tmp_path, monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider
            self.requested_seasons: list[int] = []

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            return [{"id": 42, "name": "超能路人甲", "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            self.requested_seasons.append(season_number)
            if season_number == 1:
                return {"episodes": [{"episode_number": 2, "name": "第一季终章"}]}
            if season_number == 2:
                return {"episodes": [{"episode_number": 1, "name": "第二季开篇"}]}
            return {"episodes": []}

    client_holder: dict[str, FakeTMDBClient] = {}

    def build_tmdb_client(api_key: str, proxy_decider=None) -> FakeTMDBClient:
        client = FakeTMDBClient(api_key, proxy_decider=proxy_decider)
        client_holder["client"] = client
        return client

    monkeypatch.setattr(app_module, "TMDBClient", build_tmdb_client)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="超能路人甲", vod_year="2026", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="超能路人甲", vod_year="2026", category_name="电视剧"),
            playlist=[
                PlayItem(title="S02E01.mkv", url="http://m/201.mp4", original_title="S02E01.mkv"),
                PlayItem(title="S01E02.mkv", url="http://m/102.mp4", original_title="S01E02.mkv"),
            ],
        )
    )

    assert updated is not None
    assert [item.episode_display_title for item in updated] == ["第1季 第2集 第一季终章", "第2季 第1集 第二季开篇"]
    assert [item.original_title for item in updated] == ["S01E02.mkv", "S02E01.mkv"]
    assert client_holder["client"].requested_seasons == [1, 2]


def test_app_coordinator_episode_title_enhancer_strips_season_suffix_from_tmdb_search(tmp_path, monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    seen: dict[str, object] = {}

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            seen["title"] = title
            seen["year"] = year
            return [{"id": 42, "name": "掩耳盗邻", "first_air_date": "2025-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            seen["season_number"] = season_number
            return {"episodes": [{"episode_number": 1, "name": "第二季首集"}]}

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="掩耳盗邻第二季", vod_year="2025", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="掩耳盗邻第二季", vod_year="2025", category_name="电视剧"),
            playlist=[PlayItem(title="S02E01.mkv", url="http://m/201.mp4", original_title="S02E01.mkv")],
        )
    )

    assert updated is not None
    assert seen == {"title": "掩耳盗邻", "year": "", "season_number": 2}
    assert updated[0].episode_display_title == "第1集 第二季首集"


def test_app_coordinator_episode_title_enhancer_accepts_series_with_different_first_air_year(tmp_path, monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    seen: dict[str, object] = {}

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            seen["title"] = title
            seen["year"] = year
            return [{"id": 42, "name": "掩耳盗邻", "first_air_date": "2025-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            seen["season_number"] = season_number
            return {"episodes": [{"episode_number": 1, "name": "第二季首集"}]}

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="掩耳盗邻第二季", vod_year="2026", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="掩耳盗邻第二季", vod_year="2026", category_name="电视剧"),
            playlist=[PlayItem(title="S02E01.mkv", url="http://m/201.mp4", original_title="S02E01.mkv")],
        )
    )

    assert updated is not None
    assert seen == {"title": "掩耳盗邻", "year": "", "season_number": 2}
    assert updated[0].episode_display_title == "第1集 第二季首集"


def test_app_coordinator_episode_title_enhancer_does_not_fallback_to_raw_season_title_search(tmp_path, monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    calls: list[tuple[str, str]] = []

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            calls.append((title, year))
            return []

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            raise AssertionError((tmdb_id, season_number))

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="掩耳盗邻第二季", vod_year="2026", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="掩耳盗邻第二季", vod_year="2026", category_name="电视剧"),
            playlist=[PlayItem(title="S02E01.mkv", url="http://m/201.mp4", original_title="S02E01.mkv")],
        )
    )

    assert updated is None
    assert calls == [("掩耳盗邻", "")]


def test_app_coordinator_episode_title_enhancer_falls_back_to_raw_season_title_for_standalone_sequel_entries(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    search_calls: list[tuple[str, str]] = []
    season_calls: list[tuple[str, int]] = []

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            search_calls.append((title, year))
            if title == "成何体统":
                return [{"id": 1, "name": "成何体统", "first_air_date": "2024-01-01"}]
            if title == "成何体统第二季":
                return [{"id": 2, "name": "成何体统第二季", "first_air_date": "2025-01-01"}]
            raise AssertionError((title, year))

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            season_calls.append((str(tmdb_id), season_number))
            if str(tmdb_id) == "1" and season_number == 1:
                return {"episodes": [{"episode_number": 1, "name": "认亲了"}]}
            if str(tmdb_id) == "1" and season_number == 2:
                return {"episodes": []}
            if str(tmdb_id) == "2" and season_number == 1:
                return {"episodes": [{"episode_number": 1, "name": "回宫了"}]}
            raise AssertionError((tmdb_id, season_number))

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="成何体统第二季", vod_year="2025", category_name="动漫"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="成何体统第二季", vod_year="2025", category_name="动漫"),
            playlist=[
                PlayItem(
                    title="第一季 - 01(557.99 MB)",
                    original_title="第一季 - 01(557.99 MB)",
                    path="/show/第一季/01.mp4",
                    url="http://m/1-1.mp4",
                ),
                PlayItem(
                    title="01.mp4(706.96 MB)",
                    original_title="01.mp4(706.96 MB)",
                    path="/show/01.mp4",
                    url="http://m/2-1.mp4",
                ),
            ],
        )
    )

    assert updated is not None
    assert search_calls == [("成何体统", ""), ("成何体统第二季", "")]
    assert season_calls == [("1", 1), ("1", 2), ("2", 1)]
    assert [item.episode_display_title for item in updated] == [
        "第1季 第1集 认亲了",
        "第2季 第1集 回宫了",
    ]


def test_app_coordinator_episode_title_enhancer_uses_provider_fallback_for_unresolved_standalone_sequel_entries(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeIqiyiProvider:
        name = "iqiyi"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            if candidate.title != "成何体统第二季":
                return []
            return [
                MetadataMatch(
                    provider="iqiyi",
                    provider_id="iqiyi:season2",
                    title="成何体统第二季",
                    year=candidate.year,
                    raw={
                        "videos": [
                            {"itemNumber": 1, "itemTitle": "回宫了"},
                            {"itemNumber": 2, "itemTitle": "装上了"},
                        ]
                    },
                )
            ]

    class EmptyTencentProvider:
        name = "tencent"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            return []

    class EmptyBangumiProvider:
        name = "bangumi"

        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            return []

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            if title == "成何体统":
                return [{"id": 1, "name": "成何体统", "first_air_date": "2024-01-01"}]
            if title == "成何体统第二季":
                return []
            raise AssertionError(title)

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            assert str(tmdb_id) == "1"
            if season_number == 1:
                return {"episodes": [{"episode_number": 1, "name": "认亲了"}]}
            if season_number == 2:
                return {"episodes": []}
            raise AssertionError((tmdb_id, season_number))

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "BangumiMetadataProvider", EmptyBangumiProvider)
    monkeypatch.setattr(app_module, "TencentMetadataProvider", EmptyTencentProvider)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", FakeIqiyiProvider)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="browse",
        vod=VodItem(vod_id="v1", vod_name="成何体统第二季", vod_year="", category_name="动漫"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="成何体统第二季", vod_year="", category_name="动漫"),
            playlist=[
                PlayItem(
                    title="第一季 - 01(557.99 MB)",
                    original_title="第一季 - 01(557.99 MB)",
                    path="/show/第一季/01.mp4",
                    url="http://m/1-1.mp4",
                ),
                PlayItem(
                    title="01.mp4(706.96 MB)",
                    original_title="01.mp4(706.96 MB)",
                    path="/show/01.mp4",
                    url="http://m/2-1.mp4",
                ),
                PlayItem(
                    title="02.mp4(722.67 MB)",
                    original_title="02.mp4(722.67 MB)",
                    path="/show/02.mp4",
                    url="http://m/2-2.mp4",
                ),
            ],
        )
    )

    assert updated is not None
    assert [item.episode_display_title for item in updated] == [
        "第1季 第1集 认亲了",
        "第2季 第1集 回宫了",
        "第2季 第2集 装上了",
    ]


def test_app_coordinator_episode_title_enhancer_prefers_animation_tmdb_match_for_anime_category(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    seen: list[tuple[str, int]] = []

    class EmptyProvider:
        def __init__(self, name: str) -> None:
            self.name = name

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            return []

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            if title == "成何体统":
                return [
                    {"id": 1, "name": "成何体统", "genre_ids": [18], "first_air_date": "2024-01-01"},
                    {"id": 256783, "name": "成何体统", "genre_ids": [16, 35], "first_air_date": "2024-01-01"},
                    {"id": 3, "name": "成何体统", "genre_ids": [10766], "first_air_date": "2024-01-01"},
                ]
            if title == "成何体统第二季":
                return []
            raise AssertionError(title)

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            seen.append((str(tmdb_id), season_number))
            title = {
                "1": "电视剧版首集",
                "256783": "动画版首集",
                "3": "短剧版首集",
            }[str(tmdb_id)]
            return {"episodes": [{"episode_number": 1, "name": title}]}

    class EmptyBangumiClient:
        def __init__(self, access_token: str = "", proxy_decider=None) -> None:
            del access_token, proxy_decider

        def search_subjects(self, title: str) -> list[dict[str, object]]:
            del title
            return []

    class EmptyProvider:
        def __init__(self, name: str) -> None:
            self.name = name

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            del candidate
            return []

    monkeypatch.setattr(app_module, "BangumiClient", EmptyBangumiClient)
    monkeypatch.setattr(app_module, "BilibiliMetadataProvider", lambda: EmptyProvider("bilibili"))
    monkeypatch.setattr(app_module, "TencentMetadataProvider", lambda: EmptyProvider("tencent"))
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", lambda: EmptyProvider("iqiyi"))
    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "BangumiMetadataProvider", lambda *args, **kwargs: EmptyProvider("bangumi"))
    monkeypatch.setattr(app_module, "TencentMetadataProvider", lambda: EmptyProvider("tencent"))
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", lambda: EmptyProvider("iqiyi"))
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="browse",
        vod=VodItem(vod_id="v1", vod_name="成何体统第二季", vod_year="", category_name="动漫"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="成何体统第二季", vod_year="", category_name="动漫"),
            playlist=[PlayItem(title="01.mp4", original_title="01.mp4", path="/show/01.mp4", url="http://m/1.mp4")],
        )
    )

    assert updated is not None
    assert ("256783", 2) in seen
    assert updated[0].episode_display_title == "第1集 动画版首集"


def test_app_coordinator_episode_title_enhancer_prefers_live_action_tmdb_match_for_tv_category(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    seen: dict[str, object] = {}

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            assert title == "棋魂"
            return [
                {"id": 1, "name": "棋魂", "genre_ids": [16], "first_air_date": "2022-01-01"},
                {"id": 2, "name": "棋魂", "genre_ids": [18], "first_air_date": "2020-01-01"},
            ]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            seen["tmdb_id"] = str(tmdb_id)
            seen["season_number"] = season_number
            return {"episodes": [{"episode_number": 1, "name": "真人版首集"}]}

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="browse",
        vod=VodItem(vod_id="v1", vod_name="棋魂", vod_year="2020", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="棋魂", vod_year="2020", category_name="电视剧"),
            playlist=[PlayItem(title="01.mp4", original_title="01.mp4", path="/show/01.mp4", url="http://m/1.mp4")],
        )
    )

    assert updated is not None
    assert seen == {"tmdb_id": "2", "season_number": 1}
    assert updated[0].episode_display_title == "第1集 真人版首集"


def test_app_coordinator_episode_title_enhancer_prefers_closer_year_among_same_title_matches(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    seen: dict[str, object] = {}

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            assert title == "神雕侠侣"
            assert year == "2024"
            return [
                {"id": 2014, "name": "神雕侠侣", "genre_ids": [18], "first_air_date": "2014-01-01"},
                {"id": 2024, "name": "神雕侠侣", "genre_ids": [18], "first_air_date": "2024-01-01"},
            ]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            seen["tmdb_id"] = str(tmdb_id)
            seen["season_number"] = season_number
            return {"episodes": [{"episode_number": 1, "name": "新版首集"}]}

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="browse",
        vod=VodItem(vod_id="v1", vod_name="神雕侠侣", vod_year="2024", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="神雕侠侣", vod_year="2024", category_name="电视剧"),
            playlist=[PlayItem(title="01.mp4", original_title="01.mp4", path="/show/01.mp4", url="http://m/1.mp4")],
        )
    )

    assert updated is not None
    assert seen == {"tmdb_id": "2024", "season_number": 1}
    assert updated[0].episode_display_title == "第1集 新版首集"


def test_app_coordinator_episode_title_enhancer_prefers_candidate_with_requested_season_content(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    seen: list[tuple[str, int]] = []

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            assert title == "成何体统"
            return [
                {"id": 100, "name": "成何体统", "genre_ids": [16], "first_air_date": "2024-01-01"},
                {"id": 256783, "name": "成何体统", "genre_ids": [16], "first_air_date": "2024-01-01"},
            ]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            seen.append((str(tmdb_id), season_number))
            if str(tmdb_id) == "100" and season_number == 2:
                return {"episodes": []}
            if str(tmdb_id) == "256783" and season_number == 2:
                return {"episodes": [{"episode_number": 1, "name": "第二季首集"}]}
            raise AssertionError((tmdb_id, season_number))

    class EmptyProvider:
        def __init__(self, name: str) -> None:
            self.name = name

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            return []

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "BangumiMetadataProvider", lambda *args, **kwargs: EmptyProvider("bangumi"))
    monkeypatch.setattr(app_module, "TencentMetadataProvider", lambda: EmptyProvider("tencent"))
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", lambda: EmptyProvider("iqiyi"))
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="browse",
        vod=VodItem(vod_id="v1", vod_name="成何体统第二季", vod_year="", category_name="动漫"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="成何体统第二季", vod_year="", category_name="动漫"),
            playlist=[PlayItem(title="01.mp4", original_title="01.mp4", path="/show/01.mp4", url="http://m/1.mp4")],
        )
    )

    assert updated is not None
    assert ("100", 2) in seen
    assert ("256783", 2) in seen
    assert updated[0].episode_display_title == "第1集 第二季首集"


def test_app_coordinator_episode_title_enhancer_reuses_cached_tmdb_results_across_reopens(tmp_path, monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    calls = {"search": 0, "season": 0}

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            calls["search"] += 1
            return [{"id": 42, "name": "掩耳盗邻", "first_air_date": "2025-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            calls["season"] += 1
            return {"episodes": [{"episode_number": 1, "name": "第二季首集"}]}

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())

    playlists = [
        [PlayItem(title="S02E01.mkv", url="http://m/201.mp4", original_title="S02E01.mkv", path="/playlist/a/S02E01.mkv")],
        [PlayItem(title="S02E01.mkv", url="http://m/202.mp4", original_title="S02E01.mkv", path="/playlist/b/S02E01.mkv")],
    ]
    for playlist in playlists:
        enhance = factory(
            source_kind="plugin",
            vod=VodItem(vod_id="v1", vod_name="掩耳盗邻第二季", vod_year="2026", category_name="电视剧"),
        )
        updated = enhance(
            SimpleNamespace(
                vod=VodItem(vod_id="v1", vod_name="掩耳盗邻第二季", vod_year="2026", category_name="电视剧"),
                playlist=playlist,
            )
        )
        assert updated is not None
        assert updated[0].episode_display_title == "第1集 第二季首集"

    assert calls == {"search": 1, "season": 1}


def test_app_coordinator_episode_title_enhancer_logs_tmdb_search_cache_hit_and_miss(
    tmp_path, monkeypatch, caplog
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            return [{"id": 42, "name": "掩耳盗邻", "first_air_date": "2025-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            return {"episodes": [{"episode_number": 1, "name": "第二季首集"}]}

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    caplog.set_level(logging.INFO)

    playlists = [
        [
            PlayItem(
                title="S02E01.mkv",
                url="http://m/201.mp4",
                original_title="S02E01.mkv",
                path="/playlist/a/S02E01.mkv",
            )
        ],
        [
            PlayItem(
                title="S02E01.mkv",
                url="http://m/202.mp4",
                original_title="S02E01.mkv",
                path="/playlist/b/S02E01.mkv",
            )
        ],
    ]
    for playlist in playlists:
        enhance = factory(
            source_kind="plugin",
            vod=VodItem(vod_id="v1", vod_name="掩耳盗邻第二季", vod_year="2026", category_name="电视剧"),
        )
        updated = enhance(
            SimpleNamespace(
                vod=VodItem(vod_id="v1", vod_name="掩耳盗邻第二季", vod_year="2026", category_name="电视剧"),
                playlist=playlist,
            )
        )
        assert updated is not None

    assert "Episode title enhancer TMDB search cache miss title=掩耳盗邻 year=" in caplog.text
    assert "Episode title enhancer TMDB search cache hit title=掩耳盗邻 year= results=1" in caplog.text


def test_app_coordinator_episode_title_enhancer_reuses_cached_final_titles_across_reopens(
    tmp_path, monkeypatch, caplog
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    calls = {
        "tmdb_search": 0,
        "tmdb_season": 0,
        "bangumi_search": 0,
        "bilibili_search": 0,
        "tencent_search": 0,
        "iqiyi_search": 0,
    }

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            calls["tmdb_search"] += 1
            return [{"id": 42, "name": "低智商犯罪", "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            calls["tmdb_season"] += 1
            return {
                "episodes": [
                    {"episode_number": 1, "name": "天屎驾到真案降临"},
                    {"episode_number": 2, "name": "怀疑的风吹到三江口"},
                ]
            }

    class _EmptySearchProvider:
        name = ""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def search(self, _query) -> list[MetadataMatch]:
            calls[f"{self.name}_search"] += 1
            return []

    class FakeBangumiProvider(_EmptySearchProvider):
        name = "bangumi"

    class FakeBilibiliProvider(_EmptySearchProvider):
        name = "bilibili"

    class FakeTencentProvider(_EmptySearchProvider):
        name = "tencent"

    class FakeIqiyiProvider(_EmptySearchProvider):
        name = "iqiyi"

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "BangumiClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(app_module, "BangumiMetadataProvider", FakeBangumiProvider)
    monkeypatch.setattr(app_module, "BilibiliMetadataProvider", FakeBilibiliProvider)
    monkeypatch.setattr(app_module, "TencentMetadataProvider", FakeTencentProvider)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", FakeIqiyiProvider)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    caplog.set_level(logging.INFO)

    playlists = [
        [
            PlayItem(title="01.mp4", original_title="01.mp4", path="/show/01.mp4", vod_id="session-a-01", url="http://m/1.mp4"),
            PlayItem(title="02.mp4", original_title="02.mp4", path="/show/02.mp4", vod_id="session-a-02", url="http://m/2.mp4"),
        ],
        [
            PlayItem(title="01.mp4", original_title="01.mp4", path="/show/01.mp4", vod_id="session-b-01", url="http://m/1.mp4"),
            PlayItem(title="02.mp4", original_title="02.mp4", path="/show/02.mp4", vod_id="session-b-02", url="http://m/2.mp4"),
        ],
    ]
    for playlist in playlists:
        enhance = factory(
            source_kind="plugin",
            vod=VodItem(vod_id="v1", vod_name="低智商犯罪", vod_year="2026", category_name="电视剧"),
        )
        updated = enhance(
            SimpleNamespace(
                vod=VodItem(vod_id="v1", vod_name="低智商犯罪", vod_year="2026", category_name="电视剧"),
                playlist=playlist,
            )
        )
        assert updated is not None
        assert updated[0].episode_display_title == "第1集 天屎驾到真案降临"
        assert updated[1].episode_display_title == "第2集 怀疑的风吹到三江口"

    assert calls == {
        "tmdb_search": 1,
        "tmdb_season": 1,
        "bangumi_search": 2,
        "bilibili_search": 2,
        "tencent_search": 2,
        "iqiyi_search": 2,
    }
    assert "Episode title enhancer restored cached titles mapped_count=2" in caplog.text


def test_app_coordinator_episode_title_enhancer_ignores_legacy_final_title_cache(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    calls = {"tmdb_search": 0, "tmdb_season": 0}

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            del title, year
            calls["tmdb_search"] += 1
            return [{"id": 42, "name": "低智商犯罪", "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            del tmdb_id, season_number
            calls["tmdb_season"] += 1
            return {"episodes": [{"episode_number": 1, "name": "新标题"}]}

    class EmptyProvider:
        name = ""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def search(self, _query) -> list[MetadataMatch]:
            return []

    cache_root = tmp_path / "app-cache"
    old_cache_key = repr(
        (
            "plugin",
            "低智商犯罪",
            "2026",
            "电视剧",
            (("S01E01.mkv", "/show/S01E01.mkv", "S01E01.mkv", ""),),
        )
    )
    app_module.MetadataCache(cache_root / "metadata").save_payload(
        "episode_title_playlist",
        old_cache_key,
        {"order": [0], "titles": [{"display": "第1集 旧缓存", "source": "tmdb"}]},
    )

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "BangumiClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(app_module, "BangumiMetadataProvider", EmptyProvider)
    monkeypatch.setattr(app_module, "BilibiliMetadataProvider", EmptyProvider)
    monkeypatch.setattr(app_module, "TencentMetadataProvider", EmptyProvider)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", EmptyProvider)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: cache_root)

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="低智商犯罪", vod_year="2026", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="低智商犯罪", vod_year="2026", category_name="电视剧"),
            playlist=[
                PlayItem(
                    title="S01E01.mkv",
                    original_title="S01E01.mkv",
                    path="/show/S01E01.mkv",
                    url="http://m/1.mp4",
                )
            ],
        )
    )

    assert updated is not None
    assert updated[0].episode_display_title == "第1集 新标题"
    assert calls == {"tmdb_search": 1, "tmdb_season": 1}


def test_app_coordinator_episode_title_enhancer_falls_back_to_vod_name_season_when_filename_has_no_season(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    seen: dict[str, object] = {}

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            seen["title"] = title
            seen["year"] = year
            return [{"id": 42, "name": "黑袍纠察队", "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            seen["season_number"] = season_number
            return {"episodes": [{"episode_number": 1, "name": "终局开篇"}]}

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="黑袍纠察队第五季", vod_year="2026", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="黑袍纠察队第五季", vod_year="2026", category_name="电视剧"),
            playlist=[PlayItem(title="01x.mp4", url="http://m/501.mp4", original_title="01x.mp4")],
        )
    )

    assert updated is not None
    assert seen == {"title": "黑袍纠察队", "year": "", "season_number": 5}
    assert updated[0].episode_display_title == "第1集 终局开篇"


def test_app_coordinator_episode_title_enhancer_prefers_filename_season_over_vod_name_season(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    seen: dict[str, object] = {}

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            seen["title"] = title
            seen["year"] = year
            return [{"id": 42, "name": "黑袍纠察队", "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            seen["season_number"] = season_number
            return {"episodes": [{"episode_number": 1, "name": "第二季首集"}]}

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="黑袍纠察队第五季", vod_year="2026", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="黑袍纠察队第五季", vod_year="2026", category_name="电视剧"),
            playlist=[PlayItem(title="S02E01.mkv", url="http://m/201.mp4", original_title="S02E01.mkv")],
        )
    )

    assert updated is not None
    assert seen == {"title": "黑袍纠察队", "year": "", "season_number": 2}
    assert updated[0].episode_display_title == "第1集 第二季首集"


def test_app_coordinator_episode_title_enhancer_uses_path_filename_for_mixed_playlist_episode_matching(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    seen: dict[str, object] = {}

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            seen["title"] = title
            seen["year"] = year
            return [{"id": 42, "name": "黑袍纠察队", "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            seen["season_number"] = season_number
            return {
                "episodes": [
                    {"episode_number": 1, "name": "终局开篇"},
                    {"episode_number": 6, "name": "第六集标题"},
                ]
            }

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="黑袍纠察队第五季", vod_year="2026", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="黑袍纠察队第五季", vod_year="2026", category_name="电视剧"),
            playlist=[
                PlayItem(
                    title="The.Boys.S05E06(8.5 GB)",
                    original_title="The.Boys.S05E06(8.5 GB)",
                    path="/show/Season5/S05E06.2160p.AMZN.WEB-DL.DDP5.1.Atmos.HDR10P.H.265.mkv",
                    url="http://m/6.mp4",
                ),
                PlayItem(
                    title="4K内嵌中英双语 - 1.mp4(3.46 GB)",
                    original_title="4K内嵌中英双语 - 1.mp4(3.46 GB)",
                    path="/show/Season5/4K内嵌中英双语/1.mp4",
                    url="http://m/1.mp4",
                ),
            ],
        )
    )

    assert updated is not None
    assert seen == {"title": "黑袍纠察队", "year": "", "season_number": 5}
    assert [item.episode_display_title for item in updated] == ["第1集 终局开篇", "第6集 第六集标题"]
    assert [item.original_title for item in updated] == [
        "4K内嵌中英双语 - 1.mp4(3.46 GB)",
        "The.Boys.S05E06(8.5 GB)",
    ]


def test_app_coordinator_episode_title_enhancer_ignores_complete_series_count_when_mapping_tmdb_titles(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    seen: dict[str, object] = {"season_calls": []}

    class FakeTMDBClient:
        def __init__(self, api_key: str, **_: object) -> None:
            assert api_key == "tmdb-key"

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            seen["title"] = title
            seen["year"] = year
            return [{"id": 42, "name": "The Capture", "first_air_date": "2019-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            assert tmdb_id == "42"
            cast = seen["season_calls"]
            assert isinstance(cast, list)
            cast.append(season_number)
            if season_number == 1:
                return {"episodes": [{"episode_number": 1, "name": "纠正"}]}
            if season_number == 2:
                return {"episodes": [{"episode_number": 2, "name": "惊天反转"}]}
            if season_number == 3:
                return {
                    "episodes": [
                        {"episode_number": 1, "name": "不要看镜头"},
                        {"episode_number": 6, "name": "有人守护我"},
                    ]
                }
            raise AssertionError(season_number)

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="The Capture 第三季", vod_year="", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="The Capture 第三季", vod_year="", category_name="电视剧"),
            playlist=[
                PlayItem(
                    title="第一季 1080P 6集全 - 01(3.67 GB)",
                    original_title="第一季 1080P 6集全 - 01(3.67 GB)",
                    url="http://m/s1e1.mp4",
                ),
                PlayItem(
                    title="第二季 1080P 6集全 - 02(2.76 GB)",
                    original_title="第二季 1080P 6集全 - 02(2.76 GB)",
                    url="http://m/s2e2.mp4",
                ),
                PlayItem(
                    title="S03 - 01(1).mkv(7.28 GB)",
                    original_title="S03 - 01(1).mkv(7.28 GB)",
                    path="/show/The Capture/S03E01.mkv",
                    url="http://m/s3e1.mp4",
                ),
                PlayItem(
                    title="The Capture S03E06 Someone to Watch Over Me 1080p iP WEB-DL AAC2 0 H 264-Kitsune.mkv(1.8 GB)",
                    original_title="The Capture S03E06 Someone to Watch Over Me 1080p iP WEB-DL AAC2 0 H 264-Kitsune.mkv(1.8 GB)",
                    url="http://m/s3e6.mp4",
                ),
            ],
        )
    )

    assert updated is not None
    assert seen == {"season_calls": [1, 2, 3], "title": "The Capture", "year": ""}
    assert [item.episode_display_title for item in updated] == [
        "第1季 第1集 纠正",
        "第2季 第2集 惊天反转",
        "第3季 第1集 不要看镜头",
        "第3季 第6集 有人守护我",
    ]


def test_app_coordinator_episode_title_enhancer_maps_tilde_quality_variants_and_skips_bonus_tracks(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeTMDBClient:
        def __init__(self, api_key: str, **_: object) -> None:
            assert api_key == "tmdb-key"

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            assert title == "超能路人甲"
            assert year == "2026"
            return [{"id": 42, "name": "超能路人甲", "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            assert tmdb_id == "42"
            assert season_number == 1
            return {
                "episodes": [
                    {"episode_number": 1, "name": "杀，为了活着"},
                    {"episode_number": 2, "name": "挡我者死"},
                ]
            }

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="超能路人甲", vod_year="2026", category_name="动漫"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="超能路人甲", vod_year="2026", category_name="动漫"),
            playlist=[
                PlayItem(
                    title="01 为了活着~[4K][HEVC.AAC][2025-12-28(616.48 MB)",
                    original_title="01 为了活着~[4K][HEVC.AAC][2025-12-28(616.48 MB)",
                    url="http://m/1.mp4",
                ),
                PlayItem(
                    title="02 挡我者死~[4K][HEVC.AAC][2025-12-28(549.19 MB)",
                    original_title="02 挡我者死~[4K][HEVC.AAC][2025-12-28(549.19 MB)",
                    url="http://m/2.mp4",
                ),
                PlayItem(
                    title="片头片尾 - 片头《弑神》大志Tiger [2025-12-28].mp4(64.87 MB)",
                    original_title="片头片尾 - 片头《弑神》大志Tiger [2025-12-28].mp4(64.87 MB)",
                    url="http://m/op.mp4",
                ),
                PlayItem(
                    title="高码率 - 01~4K.HQ.HEVC(3.56 GB)",
                    original_title="高码率 - 01~4K.HQ.HEVC(3.56 GB)",
                    url="http://m/1-hq.mp4",
                ),
                PlayItem(
                    title="高码率 - 02~4K.HQ.HEVC(3.32 GB)",
                    original_title="高码率 - 02~4K.HQ.HEVC(3.32 GB)",
                    url="http://m/2-hq.mp4",
                ),
                PlayItem(
                    title="片头片尾 - 片尾《归无》段奥娟 [202512-28].mp4(59.03 MB)",
                    original_title="片头片尾 - 片尾《归无》段奥娟 [202512-28].mp4(59.03 MB)",
                    url="http://m/ed.mp4",
                ),
            ],
        )
    )

    assert updated is not None
    mapped_titles = {item.original_title: item.episode_display_title for item in updated}
    assert mapped_titles["01 为了活着~[4K][HEVC.AAC][2025-12-28(616.48 MB)"] == "第1集 杀，为了活着"
    assert mapped_titles["02 挡我者死~[4K][HEVC.AAC][2025-12-28(549.19 MB)"] == "第2集 挡我者死"
    assert mapped_titles["高码率 - 01~4K.HQ.HEVC(3.56 GB)"] == "第1集 杀，为了活着"
    assert mapped_titles["高码率 - 02~4K.HQ.HEVC(3.32 GB)"] == "第2集 挡我者死"
    assert mapped_titles["片头片尾 - 片头《弑神》大志Tiger [2025-12-28].mp4(64.87 MB)"] == ""
    assert mapped_titles["片头片尾 - 片尾《归无》段奥娟 [202512-28].mp4(59.03 MB)"] == ""


def test_app_coordinator_episode_title_enhancer_uses_playlist_position_for_numeric_x_suffix_filenames(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeTMDBClient:
        def __init__(self, api_key: str, **_: object) -> None:
            assert api_key == "tmdb-key"

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            assert title == "牧神记"
            return [{"id": 42, "name": "牧神记", "first_air_date": "2024-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            assert tmdb_id == "42"
            assert season_number == 1
            return {
                "episodes": [
                    {"episode_number": 1, "name": "天黑别出门"},
                    {"episode_number": 2, "name": "我是霸体"},
                    {"episode_number": 3, "name": "小魔崽子"},
                    {"episode_number": 4, "name": "黑暗将至"},
                    {"episode_number": 5, "name": "红粉骷髅"},
                ]
            }

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="牧神记", vod_year="2024", category_name="动漫"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="牧神记", vod_year="2024", category_name="动漫"),
            playlist=[
                PlayItem(title="70x.mp4(2.52 GB)", original_title="70x.mp4(2.52 GB)", url="http://m/70.mp4"),
                PlayItem(title="71x.mp4(2.57 GB)", original_title="71x.mp4(2.57 GB)", url="http://m/71.mp4"),
                PlayItem(title="78X.mkv(2.89 GB)", original_title="78X.mkv(2.89 GB)", url="http://m/78.mkv"),
                PlayItem(title="82 x.mp4(1.67 GB)", original_title="82 x.mp4(1.67 GB)", url="http://m/82.mp4"),
                PlayItem(title="83 x.mp4(2.94 GB)", original_title="83 x.mp4(2.94 GB)", url="http://m/83.mp4"),
            ],
        )
    )

    assert updated is not None
    assert [item.episode_display_title for item in updated] == [
        "第1集 天黑别出门",
        "第2集 我是霸体",
        "第3集 小魔崽子",
        "第4集 黑暗将至",
        "第5集 红粉骷髅",
    ]


def test_app_coordinator_browse_episode_title_enhancer_falls_back_to_playlist_inferred_series_title(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    seen: list[tuple[str, str]] = []

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            seen.append((title, year))
            if title == "N 哪天第2":
                return []
            if title == "逆天邪神" and year == "2023":
                return [{"id": 42, "name": "逆天邪神", "first_air_date": "2023-01-01"}]
            raise AssertionError((title, year))

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            assert tmdb_id == "42"
            assert season_number == 1
            return {
                "episodes": [
                    {"episode_number": 1, "name": "初入神界"},
                    {"episode_number": 31, "name": "邪神归来"},
                ]
            }

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="browse",
        vod=VodItem(vod_id="v1", vod_name="N 哪天第2", vod_year="", category_name="动漫"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="N 哪天第2", vod_year="", category_name="动漫"),
            playlist=[
                PlayItem(
                    title="逆丨天邪神 (2023) - 01-4K-[H265.AAC][2023-09-23(815.88 MB)",
                    original_title="逆丨天邪神 (2023) - 01-4K-[H265.AAC][2023-09-23(815.88 MB)",
                    path="/show/逆丨天邪神 (2023)/01-4K-[H265.AAC][2023-09-23].mp4",
                    url="http://m/1.mp4",
                ),
                PlayItem(
                    title="S01E31.2026.2160p.25fps.WEB-DL.H265.10bit.DDP2.0(0.98 GB)",
                    original_title="S01E31.2026.2160p.25fps.WEB-DL.H265.10bit.DDP2.0(0.98 GB)",
                    path="/show/逆天邪神.S01E31.2026.2160p.25fps.WEB-DL.H265.10bit.DDP2.0.mp4",
                    url="http://m/31.mp4",
                ),
            ],
        )
    )

    assert updated is not None
    assert seen == [("N 哪天第2", ""), ("逆天邪神", "2023")]
    assert [item.episode_display_title for item in updated] == [
        "第1集 初入神界",
        "第31集 邪神归来",
    ]


def test_app_coordinator_episode_title_enhancer_preserves_grouped_multi_version_playlist_order(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            assert title == "超能路人甲"
            assert year == "2026"
            return [{"id": 42, "name": "超能路人甲", "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            assert tmdb_id == "42"
            assert season_number == 1
            return {
                "episodes": [
                    {"episode_number": 1, "name": "星门初启"},
                    {"episode_number": 2, "name": "星火初燃"},
                ]
            }

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="超能路人甲", vod_year="2026", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="超能路人甲", vod_year="2026", category_name="电视剧"),
            playlist=[
                PlayItem(title="1-4K.mp4", url="http://m/1-4k.mp4", original_title="1-4K.mp4"),
                PlayItem(title="2-4K.mp4", url="http://m/2-4k.mp4", original_title="2-4K.mp4"),
                PlayItem(title="1-1080P.mp4", url="http://m/1-1080.mp4", original_title="1-1080P.mp4"),
                PlayItem(title="2-1080P.mp4", url="http://m/2-1080.mp4", original_title="2-1080P.mp4"),
            ],
        )
    )

    assert updated is not None
    assert [item.original_title for item in updated] == [
        "1-4K.mp4",
        "2-4K.mp4",
        "1-1080P.mp4",
        "2-1080P.mp4",
    ]
    assert [item.episode_display_title for item in updated] == [
        "第1集 星门初启",
        "第2集 星火初燃",
        "第1集 星门初启",
        "第2集 星火初燃",
    ]
    assert [item.index for item in updated] == [0, 1, 2, 3]


def test_app_coordinator_episode_title_enhancer_sorts_each_multi_version_block_independently(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            assert title == "超能路人甲"
            assert year == "2026"
            return [{"id": 42, "name": "超能路人甲", "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            assert tmdb_id == "42"
            assert season_number == 1
            return {
                "episodes": [
                    {"episode_number": 1, "name": "星门初启"},
                    {"episode_number": 2, "name": "星火初燃"},
                ]
            }

    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="超能路人甲", vod_year="2026", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="超能路人甲", vod_year="2026", category_name="电视剧"),
            playlist=[
                PlayItem(title="2-4K.mp4", url="http://m/2-4k.mp4", original_title="2-4K.mp4"),
                PlayItem(title="1-4K.mp4", url="http://m/1-4k.mp4", original_title="1-4K.mp4"),
                PlayItem(title="2-1080P.mp4", url="http://m/2-1080.mp4", original_title="2-1080P.mp4"),
                PlayItem(title="1-1080P.mp4", url="http://m/1-1080.mp4", original_title="1-1080P.mp4"),
            ],
        )
    )

    assert updated is not None
    assert [item.original_title for item in updated] == [
        "1-4K.mp4",
        "2-4K.mp4",
        "1-1080P.mp4",
        "2-1080P.mp4",
    ]
    assert [item.episode_display_title for item in updated] == [
        "第1集 星门初启",
        "第2集 星火初燃",
        "第1集 星门初启",
        "第2集 星火初燃",
    ]
    assert [item.index for item in updated] == [0, 1, 2, 3]


def test_app_coordinator_episode_title_enhancer_prefers_high_confidence_iqiyi_over_tmdb_and_tencent(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeSearchProvider:
        def __init__(self, provider_name: str, title: str) -> None:
            self.name = provider_name
            self._title = title

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            raw = (
                {"episode_sites": [{"episodeInfoList": [{"title": self._title}]}]}
                if self.name == "tencent"
                else {"videos": [{"itemNumber": 1, "itemTitle": self._title}]}
            )
            return [
                MetadataMatch(
                    provider=self.name,
                    provider_id=f"{self.name}:1",
                    title=candidate.title,
                    year=candidate.year,
                    raw=raw,
                )
            ]

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            return [{"id": 42, "name": title, "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            return {"episodes": [{"episode_number": 1, "name": "TMDB标题"}]}

    monkeypatch.setattr(app_module, "TencentMetadataProvider", lambda: FakeSearchProvider("tencent", "第01话 金银米小圈1"))
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", lambda: FakeSearchProvider("iqiyi", "终局开篇"))
    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="米小圈上学记4", vod_year="2026", category_name="少儿"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="米小圈上学记4", vod_year="2026", category_name="少儿"),
            playlist=[PlayItem(title="01.mp4", url="http://m/1.mp4", original_title="01.mp4")],
        )
    )

    assert updated is not None
    assert updated[0].episode_title_source == "iqiyi"
    assert updated[0].episode_display_title == "第1集 终局开篇"


def test_app_coordinator_episode_title_enhancer_prefers_iqiyi_when_search_videoinfos_are_present(
    tmp_path,
    monkeypatch,
) -> None:
    from atv_player.metadata.providers.iqiyi import IqiyiMetadataProvider as RealIqiyiMetadataProvider

    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class EmptyTencentProvider:
        name = "tencent"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            del candidate
            return []

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            return [{"id": 42, "name": title, "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            return {"episodes": [{"episode_number": 1, "name": "TMDB标题"}]}

    def fake_iqiyi_get(url: str, **kwargs):
        assert url == "https://mesh.if.iqiyi.com/portal/lw/search/homePageV3"
        del kwargs
        return type(
            "Resp",
            (),
            {
                "json": lambda self: {
                    "data": {
                        "templates": [
                            {
                                "template": 101,
                                "albumInfo": {
                                    "title": "家业",
                                    "siteId": "iqiyi",
                                    "siteName": "爱奇艺",
                                    "pageUrl": "https://www.iqiyi.com/v_demo.html",
                                    "year": {"value": "2026"},
                                    "videos": [],
                                },
                                "videoinfos": [
                                    {
                                        "number": 1,
                                        "subtitle": "终局开篇",
                                        "title": "家业 第1集",
                                        "pageUrl": "https://www.iqiyi.com/v_ep1.html",
                                    }
                                ],
                            }
                        ]
                    }
                }
            },
        )()

    monkeypatch.setattr(app_module, "TencentMetadataProvider", EmptyTencentProvider)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", lambda: RealIqiyiMetadataProvider(get=fake_iqiyi_get))
    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="家业", vod_year="2026", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="家业", vod_year="2026", category_name="电视剧"),
            playlist=[PlayItem(title="01.mp4", url="http://m/1.mp4", original_title="01.mp4")],
        )
    )

    assert updated is not None
    assert updated[0].episode_title_source == "iqiyi"
    assert updated[0].episode_display_title == "第1集 终局开篇"


def test_app_coordinator_episode_title_enhancer_keeps_tmdb_when_iqiyi_title_confidence_is_low(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class EmptyTencentProvider:
        name = "tencent"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            del candidate
            return []

    class FakeIqiyiProvider:
        name = "iqiyi"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            return [
                MetadataMatch(
                    provider="iqiyi",
                    provider_id="iqiyi:1",
                    title="临江仙 特别篇",
                    year="2025",
                    raw={"videos": [{"itemNumber": 1, "itemTitle": "缘起"}]},
                )
            ]

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            return [{"id": 42, "name": title, "first_air_date": "2025-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            return {"episodes": [{"episode_number": 1, "name": "TMDB标题"}]}

    monkeypatch.setattr(app_module, "TencentMetadataProvider", EmptyTencentProvider)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", FakeIqiyiProvider)
    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="临江仙", vod_year="2025", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="临江仙", vod_year="2025", category_name="电视剧"),
            playlist=[PlayItem(title="01.mp4", url="http://m/1.mp4", original_title="01.mp4")],
        )
    )

    assert updated is not None
    assert updated[0].episode_title_source == "tmdb"
    assert updated[0].episode_display_title == "第1集 TMDB标题"


def test_app_coordinator_episode_title_enhancer_prefers_confirmed_bilibili_over_tmdb_when_mapping_count_is_equal(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class EmptyTencentProvider:
        name = "tencent"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            return []

    class EmptyIqiyiProvider:
        name = "iqiyi"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            return []

    class EmptyBangumiClient:
        def __init__(self, access_token: str = "", proxy_decider=None) -> None:
            del access_token, proxy_decider

        def search_subjects(self, title: str) -> list[dict[str, object]]:
            del title
            return []

    class FakeBilibiliProvider:
        name = "bilibili"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            return [
                MetadataMatch(
                    provider="bilibili",
                    provider_id="https://www.bilibili.com/bangumi/play/ss148433",
                    title=candidate.title,
                    year=candidate.year,
                    raw={"season_id": 148433, "season_type_name": "国创"},
                )
            ]

        def _hydrate_episode_candidate(self, candidate):
            return candidate.__class__(
                provider=candidate.provider,
                provider_id=candidate.provider_id,
                title=candidate.title,
                year=candidate.year,
                raw={
                    **candidate.raw,
                    "episodes": [
                        {"episode_number": 1, "long_title": "来世，你可以找我报仇", "episode_type": "main", "sort": 1},
                        {"episode_number": 28, "long_title": "答案", "episode_type": "main", "sort": 28},
                    ],
                },
            )

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            return [{"id": 315088, "name": title, "first_air_date": "2025-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            return {
                "episodes": [
                    {"episode_number": 1, "name": "来世，你可以找我报仇"},
                    {"episode_number": 28, "name": "第 28 集"},
                ]
            }

    monkeypatch.setattr(app_module, "BangumiClient", EmptyBangumiClient)
    monkeypatch.setattr(app_module, "TencentMetadataProvider", EmptyTencentProvider)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", EmptyIqiyiProvider)
    monkeypatch.setattr(app_module, "BilibiliMetadataProvider", FakeBilibiliProvider)
    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="凸变英雄X", vod_year="2025", category_name="动漫"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="凸变英雄X", vod_year="2025", category_name="动漫"),
            playlist=[
                PlayItem(title="S01E01.mkv", url="http://m/1.mp4", original_title="S01E01.mkv"),
                PlayItem(title="S01E28.mkv", url="http://m/28.mp4", original_title="S01E28.mkv"),
            ],
        )
    )

    assert updated is not None
    assert updated[0].episode_title_source == "bilibili"
    assert updated[0].episode_display_title == "第1集 来世，你可以找我报仇"
    assert updated[1].episode_title_source == "bilibili"
    assert updated[1].episode_display_title == "第28集 答案"


def test_app_coordinator_episode_title_enhancer_prefers_bound_bangumi_over_tmdb_after_reopen(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        database_path = tmp_path / "app.db"

        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    MetadataBindingRepository(tmp_path / "app.db").save(
        "牧神记",
        "2024",
        provider="bangumi",
        provider_id="subject:1",
        matched_title="牧神记",
        matched_year="2024",
    )

    class FakeBangumiClient:
        def __init__(self, access_token: str = "", proxy_decider=None) -> None:
            del access_token, proxy_decider

        def get_episodes(self, subject_id: str) -> list[dict[str, object]]:
            assert subject_id == "1"
            return [
                {"sort": 1, "type": 0, "name_cn": "天黑别出门"},
                {"sort": 2, "type": 0, "name_cn": "我是霸体"},
            ]

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            return [{"id": 42, "name": title, "first_air_date": "2024-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            return {
                "episodes": [
                    {"episode_number": 1, "name": "TMDB标题1"},
                    {"episode_number": 2, "name": "TMDB标题2"},
                ]
            }

    monkeypatch.setattr(app_module, "BangumiClient", FakeBangumiClient)
    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="牧神记", vod_year="2024", category_name="动漫"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="牧神记", vod_year="2024", category_name="动漫"),
            playlist=[
                PlayItem(title="01.mp4", url="http://m/1.mp4", original_title="01.mp4"),
                PlayItem(title="02.mp4", url="http://m/2.mp4", original_title="02.mp4"),
            ],
        )
    )

    assert updated is not None
    assert updated[0].episode_title_source == "bangumi"
    assert updated[0].episode_display_title == "第1集 天黑别出门"
    assert updated[1].episode_title_source == "bangumi"
    assert updated[1].episode_display_title == "第2集 我是霸体"


def test_app_coordinator_episode_title_enhancer_loads_bound_bangumi_using_current_media_title_query(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        database_path = tmp_path / "app.db"

        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    MetadataBindingRepository(tmp_path / "app.db").save(
        "仙剑奇侠传三",
        "2025",
        provider="bangumi",
        provider_id="subject:526975",
        matched_title="仙剑奇侠传三",
        matched_year="2025",
    )

    class FakeBangumiClient:
        def __init__(self, access_token: str = "", proxy_decider=None) -> None:
            del access_token, proxy_decider

        def get_episodes(self, subject_id: str) -> list[dict[str, object]]:
            assert subject_id == "526975"
            return [{"sort": 1, "type": 0, "name_cn": "问心"}]

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            return [{"id": 42, "name": title, "first_air_date": "2025-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            return {"episodes": [{"episode_number": 1, "name": "TMDB标题"}]}

    monkeypatch.setattr(app_module, "BangumiClient", FakeBangumiClient)
    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="自动刮削后的标题", vod_year="2025", category_name="动漫"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="自动刮削后的标题", vod_year="2025", category_name="动漫"),
            start_index=0,
            playlist=[
                PlayItem(
                    title="01.mp4",
                    url="http://m/1.mp4",
                    original_title="01.mp4",
                    media_title="仙剑奇侠传三",
                )
            ],
        )
    )

    assert updated is not None
    assert updated[0].episode_title_source == "bangumi"
    assert updated[0].episode_display_title == "第1集 问心"


def test_app_coordinator_episode_title_enhancer_hydrates_bangumi_search_candidate_to_override_tmdb(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeBangumiClient:
        def __init__(self, access_token: str = "", proxy_decider=None) -> None:
            del access_token, proxy_decider

        def search_subjects(self, title: str) -> list[dict[str, object]]:
            assert title == "仙剑奇侠传三"
            return [{"id": 395223, "type": 2, "name_cn": "仙剑奇侠传三", "date": "2025-01-01"}]

        def get_episodes(self, subject_id: str) -> list[dict[str, object]]:
            assert subject_id == "395223"
            return [
                {"sort": 1, "type": 0, "name_cn": "问心"},
                {"sort": 2, "type": 0, "name_cn": "蜀山风云"},
            ]

    class EmptyTencentProvider:
        name = "tencent"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            del candidate
            return []

    class EmptyIqiyiProvider:
        name = "iqiyi"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            del candidate
            return []

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            return [{"id": 233295, "name": title, "first_air_date": "2025-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            return {
                "episodes": [
                    {"episode_number": 1, "name": "TMDB标题1"},
                    {"episode_number": 2, "name": "TMDB标题2"},
                ]
            }

    monkeypatch.setattr(app_module, "BangumiClient", FakeBangumiClient)
    monkeypatch.setattr(app_module, "TencentMetadataProvider", EmptyTencentProvider)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", EmptyIqiyiProvider)
    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="仙剑奇侠传三", vod_year="2025", category_name="动漫"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="仙剑奇侠传三", vod_year="2025", category_name="动漫"),
            start_index=0,
            playlist=[
                PlayItem(title="S01E01.mkv", url="http://m/1.mp4", original_title="S01E01.mkv"),
                PlayItem(title="S01E02.mkv", url="http://m/2.mp4", original_title="S01E02.mkv"),
            ],
        )
    )

    assert updated is not None
    assert updated[0].episode_title_source == "bangumi"
    assert updated[0].episode_display_title == "第1集 问心"
    assert updated[1].episode_title_source == "bangumi"
    assert updated[1].episode_display_title == "第2集 蜀山风云"


def test_app_coordinator_episode_title_enhancer_infers_anime_category_from_title_suffix_and_prefers_bangumi(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeBangumiClient:
        def __init__(self, access_token: str = "", proxy_decider=None) -> None:
            del access_token, proxy_decider

        def search_subjects(self, title: str) -> list[dict[str, object]]:
            assert title == "仙剑奇侠传叁"
            return [{"id": 395223, "type": 2, "name_cn": "仙剑奇侠传三", "date": "2025-01-01"}]

        def get_episodes(self, subject_id: str) -> list[dict[str, object]]:
            assert subject_id == "395223"
            return [
                {"sort": 1, "type": 0, "name_cn": "问心"},
                {"sort": 2, "type": 0, "name_cn": "蜀山风云"},
            ]

    class EmptyTencentProvider:
        name = "tencent"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            del candidate
            return []

    class EmptyIqiyiProvider:
        name = "iqiyi"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            del candidate
            return []

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            return [{"id": 233295, "name": "仙剑奇侠传三", "genre_ids": [16], "first_air_date": "2025-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            return {
                "episodes": [
                    {"episode_number": 1, "name": "TMDB标题1"},
                    {"episode_number": 2, "name": "TMDB标题2"},
                ]
            }

    monkeypatch.setattr(app_module, "BangumiClient", FakeBangumiClient)
    monkeypatch.setattr(app_module, "TencentMetadataProvider", EmptyTencentProvider)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", EmptyIqiyiProvider)
    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="仙剑奇侠传叁动漫", vod_year="2025"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="仙剑奇侠传叁动漫", vod_year="2025"),
            start_index=0,
            playlist=[
                PlayItem(title="S01E01.mkv", url="http://m/1.mp4", original_title="S01E01.mkv"),
                PlayItem(title="S01E02.mkv", url="http://m/2.mp4", original_title="S01E02.mkv"),
            ],
        )
    )

    assert updated is not None
    assert updated[0].episode_title_source == "bangumi"
    assert updated[0].episode_display_title == "第1集 问心"
    assert updated[1].episode_title_source == "bangumi"
    assert updated[1].episode_display_title == "第2集 蜀山风云"


def test_app_coordinator_episode_title_enhancer_falls_back_from_tmdb_to_iqiyi(tmp_path, monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class EmptyTencentProvider:
        name = "tencent"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            return [
                MetadataMatch(
                    provider="tencent",
                    provider_id="tencent:1",
                    title=candidate.title,
                    year=candidate.year,
                    raw={},
                )
            ]

    class FakeIqiyiProvider:
        name = "iqiyi"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            return [
                MetadataMatch(
                    provider="iqiyi",
                    provider_id="iqiyi:1",
                    title=candidate.title,
                    year=candidate.year,
                    raw={"videos": [{"itemNumber": 1, "itemTitle": "终局开篇"}]},
                )
            ]

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            return [{"id": 42, "name": title, "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            return {"episodes": []}

    monkeypatch.setattr(app_module, "TencentMetadataProvider", EmptyTencentProvider)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", FakeIqiyiProvider)
    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="黑袍纠察队第五季", vod_year="2026", category_name="电视剧"),
    )

    updated = enhance(
        SimpleNamespace(
            vod=VodItem(vod_id="v1", vod_name="黑袍纠察队第五季", vod_year="2026", category_name="电视剧"),
            playlist=[PlayItem(title="S05E01.mkv", url="http://m/1.mp4", original_title="S05E01.mkv")],
        )
    )

    assert updated is not None
    assert updated[0].episode_title_source == "iqiyi"
    assert updated[0].episode_display_title == "第1集 终局开篇"


def test_app_coordinator_episode_title_enhancer_falls_back_from_tmdb_404_to_tencent(tmp_path, monkeypatch, caplog) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_tmdb_api_key="tmdb-key",
                episode_title_enhancement_enabled=True,
            )

    class FakeTencentProvider:
        name = "tencent"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            return [
                MetadataMatch(
                    provider="tencent",
                    provider_id="tencent:1",
                    title=candidate.title,
                    year=candidate.year,
                    raw={"episode_sites": [{"episodeInfoList": [{"title": "第01话 金银米小圈1"}]}]},
                )
            ]

    class EmptyIqiyiProvider:
        name = "iqiyi"

        def search(self, candidate: MetadataQuery) -> list[MetadataMatch]:
            return []

    class FakeTMDBClient:
        def __init__(self, api_key: str, proxy_decider=None) -> None:
            assert api_key == "tmdb-key"
            del proxy_decider

        def search_tv(self, title: str, year: str = "") -> list[dict[str, object]]:
            return [{"id": 233295, "name": title, "first_air_date": "2026-01-01"}]

        def get_tv_season_detail(self, tmdb_id: str | int, season_number: int) -> dict[str, object]:
            request = httpx.Request("GET", f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("404 Not Found", request=request, response=response)

    monkeypatch.setattr(app_module, "TencentMetadataProvider", FakeTencentProvider)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", EmptyIqiyiProvider)
    monkeypatch.setattr(app_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_episode_title_enhancer_factory(object())
    enhance = factory(
        source_kind="plugin",
        vod=VodItem(vod_id="v1", vod_name="米小圈上学记4", vod_year="2026", category_name="少儿"),
    )

    with caplog.at_level(logging.DEBUG, logger=app_module.__name__):
        updated = enhance(
            SimpleNamespace(
                vod=VodItem(vod_id="v1", vod_name="米小圈上学记4", vod_year="2026", category_name="少儿"),
                playlist=[PlayItem(title="01.mp4", url="http://m/1.mp4", original_title="01.mp4")],
            )
        )

    assert updated is not None
    assert updated[0].episode_title_source == "tencent"
    assert updated[0].episode_display_title == "第1集 第01话 金银米小圈1"
    assert "Skip TMDB season episode title enhancement fallback to provider metadata" in caplog.text


def test_app_coordinator_build_plugin_metadata_payload_uses_metadata_block_and_raw_fallbacks() -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig()

    coordinator = AppCoordinator(FakeRepo())

    payload = coordinator._build_plugin_metadata_payload(
        {
            "metadata": {
                "id": "plugin-meta-1",
                "title": "自定义标题",
                "overview": "自定义简介",
                "rating": "8.8",
            },
            "vod_id": "detail-1",
            "vod_name": "原始标题",
            "vod_content": "原始简介",
            "vod_remarks": "9.9",
            "vod_year": "2026",
            "vod_pic": "https://img.example/poster.jpg",
            "vod_actor": "梁达伟,唐雅菁",
            "vod_area": "中国大陆",
            "vod_lang": "汉语普通话",
            "vod_director": "周琛",
            "type_name": "动画 / 科幻",
            "ext": [{"label": "别名", "value": "深空彼岸"}],
        }
    )

    assert payload == {
        "id": "plugin-meta-1",
        "title": "自定义标题",
        "overview": "自定义简介",
        "rating": "8.8",
        "year": "2026",
        "poster": "https://img.example/poster.jpg",
        "actors": "梁达伟,唐雅菁",
        "country": "中国大陆",
        "language": "汉语普通话",
        "directors": "周琛",
        "genre": "动画 / 科幻",
        "detail_fields": [{"label": "别名", "value": "深空彼岸"}],
    }


def test_app_coordinator_builds_local_douban_client_from_latest_config(monkeypatch, tmp_path) -> None:
    class FakeRepo(_FakeRepoBase):
        def __init__(self) -> None:
            self.config = AppConfig(
                metadata_douban_cookie="bid=first;",
                metadata_tmdb_api_key="tmdb-key",
                metadata_tmdb_proxy_base_url="https://tmdb.example.com",
            )

        def load_config(self) -> AppConfig:
            return self.config

    class RecordingLocalDoubanClient:
        def __init__(self, cookie: str = "", transport=None, proxy_decider=None) -> None:
            del transport, proxy_decider
            seen_cookies.append(cookie)

    class RecordingLocalDoubanProvider:
        name = "local_douban"

        def __init__(self, local_client) -> None:
            captured["local_client"] = local_client

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    class RecordingRemoteDoubanProvider:
        name = "remote_douban"

        def __init__(self, api_client) -> None:
            captured["api_client"] = api_client

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    class RecordingTMDBClient:
        def __init__(self, api_key: str, proxy_base_url: str = "", transport=None, proxy_decider=None) -> None:
            del transport, proxy_decider
            captured["tmdb_api_key"] = api_key
            captured["tmdb_proxy_base_url"] = proxy_base_url

    class RecordingTMDBProvider:
        name = "tmdb"

        def __init__(self, client) -> None:
            captured["tmdb_client"] = client

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    seen_cookies: list[str] = []
    captured: dict[str, object] = {}
    repo = FakeRepo()
    coordinator = AppCoordinator(repo)
    api_client = object()

    monkeypatch.setattr(app_module, "LocalDoubanClient", RecordingLocalDoubanClient)
    monkeypatch.setattr(app_module, "OfficialDoubanProvider", RecordingLocalDoubanProvider, raising=False)
    monkeypatch.setattr(app_module, "LocalDoubanProvider", RecordingRemoteDoubanProvider, raising=False)
    monkeypatch.setattr(app_module, "TMDBClient", RecordingTMDBClient, raising=False)
    monkeypatch.setattr(app_module, "TMDBProvider", RecordingTMDBProvider, raising=False)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    factory = coordinator._build_metadata_hydrator_factory(api_client)
    hydrate = factory(source_kind="browse", vod=VodItem(vod_id="v1", vod_name="深空彼岸"))
    assert callable(hydrate)

    repo.config.metadata_douban_cookie = "bid=second;"
    hydrate = factory(source_kind="browse", vod=VodItem(vod_id="v2", vod_name="牧神记"))
    assert callable(hydrate)

    assert seen_cookies == ["bid=first;", "bid=second;"]
    assert captured["api_client"] is api_client
    assert captured["tmdb_api_key"] == "tmdb-key"
    assert captured["tmdb_proxy_base_url"] == "https://tmdb.example.com"
    assert "tmdb_client" in captured


def test_app_coordinator_enables_tmdb_provider_with_proxy_only(monkeypatch) -> None:
    config = AppConfig(
        metadata_tmdb_api_key="",
        metadata_tmdb_proxy_base_url="https://tmdb.example.com",
        disabled_metadata_provider_ids=[
            "plugin",
            "bangumi",
            "bilibili",
            "iqiyi",
            "tencent",
            "youku",
            "official_douban",
            "sohu",
            "local_douban",
            "remote_douban",
        ],
    )

    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return config

    class RecordingTMDBClient:
        def __init__(self, api_key: str, proxy_base_url: str = "", proxy_decider=None) -> None:
            del proxy_decider
            captured["api_key"] = api_key
            captured["proxy_base_url"] = proxy_base_url

    class RecordingTMDBProvider:
        name = "tmdb"

        def __init__(self, client) -> None:
            captured["provider_client"] = client

    captured: dict[str, object] = {}
    coordinator = AppCoordinator(FakeRepo())
    monkeypatch.setattr(app_module, "TMDBClient", RecordingTMDBClient, raising=False)
    monkeypatch.setattr(app_module, "TMDBProvider", RecordingTMDBProvider, raising=False)

    providers = coordinator._build_metadata_providers(
        api_client=object(),
        config=config,
        source_kind="browse",
        raw_detail=None,
    )

    assert [provider.name for provider in providers] == ["tmdb"]
    assert captured["api_key"] == ""
    assert captured["proxy_base_url"] == "https://tmdb.example.com"
    assert "provider_client" in captured


def test_app_coordinator_disables_metadata_hydrator_when_enhancement_off(tmp_path) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=False,
                metadata_douban_cookie="bid=demo;",
                metadata_tmdb_api_key="tmdb-key",
            )

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_metadata_hydrator_factory(object())

    hydrate = factory(source_kind="browse", vod=VodItem(vod_id="v1", vod_name="深空彼岸"))

    assert hydrate is None


def test_app_coordinator_metadata_factories_do_not_support_youtube_source(tmp_path) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(metadata_enhancement_enabled=True)

    coordinator = AppCoordinator(FakeRepo())
    hydrator_factory = coordinator._build_metadata_hydrator_factory(object())
    scrape_factory = coordinator._build_metadata_scrape_service_factory(object())

    hydrate = hydrator_factory(source_kind="youtube", vod=VodItem(vod_id="yt:video:abc123", vod_name="YouTube"))
    scrape_service = scrape_factory(source_kind="youtube", vod=VodItem(vod_id="yt:video:abc123", vod_name="YouTube"))

    assert hydrate is None
    assert scrape_service is None


@pytest.mark.parametrize("vod_id", ["BV1xx411c7mD", "av170001", ""])
def test_app_coordinator_metadata_factories_skip_regular_bilibili_video_ids(tmp_path, monkeypatch, vod_id: str) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(metadata_enhancement_enabled=True)

    coordinator = AppCoordinator(FakeRepo())
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    hydrator_factory = coordinator._build_metadata_hydrator_factory(object())
    scrape_factory = coordinator._build_metadata_scrape_service_factory(object())
    vod = VodItem(vod_id=vod_id, vod_name="B站普通视频")

    hydrate = hydrator_factory(source_kind="bilibili", vod=vod)
    scrape_service = scrape_factory(source_kind="bilibili", vod=vod)

    assert hydrate is None
    assert scrape_service is None


@pytest.mark.parametrize("vod_id", ["ss45969", "ep2401902", "season$45969"])
def test_app_coordinator_metadata_factories_support_bilibili_pgc_ids(tmp_path, monkeypatch, vod_id: str) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(metadata_enhancement_enabled=True)

    coordinator = AppCoordinator(FakeRepo())
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    hydrator_factory = coordinator._build_metadata_hydrator_factory(object())
    scrape_factory = coordinator._build_metadata_scrape_service_factory(object())
    vod = VodItem(vod_id=vod_id, vod_name="B站长视频")

    hydrate = hydrator_factory(source_kind="bilibili", vod=vod)
    scrape_service = scrape_factory(source_kind="bilibili", vod=vod)

    assert callable(hydrate)
    assert scrape_service is not None


def test_app_coordinator_injects_ai_enrichment_into_metadata_scrape_factory(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                ai_enabled=True,
                ai_base_url="https://api.example.com",
                ai_api_key="key",
                ai_chat_model="model",
            )

    coordinator = AppCoordinator(FakeRepo())
    ai_service = object()
    monkeypatch.setattr(coordinator, "_build_ai_enrichment_service", lambda config: ai_service)
    monkeypatch.setattr(coordinator, "_build_metadata_providers", lambda **kwargs: [])
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    factory = coordinator._build_metadata_scrape_service_factory(object())
    service = factory(source_kind="browse", vod=VodItem(vod_id="1", vod_name="x"))

    assert service._ai_enrichment_service is ai_service


def test_app_coordinator_disables_metadata_ai_workflows_independently(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                ai_enabled=True,
                ai_base_url="https://api.example.com",
                ai_api_key="key",
                ai_chat_model="model",
                ai_metadata_enrichment_enabled=False,
                ai_episode_title_rewrite_enabled=True,
            )

    coordinator = AppCoordinator(FakeRepo())
    workflow_calls: list[str] = []

    def build_ai(config, *, workflow: str = ""):
        del config
        workflow_calls.append(workflow)
        return object()

    monkeypatch.setattr(coordinator, "_build_ai_enrichment_service", build_ai)
    monkeypatch.setattr(coordinator, "_build_metadata_providers", lambda **kwargs: [])
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    factory = coordinator._build_metadata_scrape_service_factory(object())
    service = factory(source_kind="browse", vod=VodItem(vod_id="1", vod_name="x"))

    assert workflow_calls == ["episode_titles"]
    assert service._ai_enrichment_service is not None
    assert service._ai_query_refinement_enabled is False
    assert service._ai_episode_title_rewrite_enabled is True


def test_app_coordinator_metadata_factories_support_bilibili_season_id_detail_field(tmp_path, monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(metadata_enhancement_enabled=True)

    coordinator = AppCoordinator(FakeRepo())
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    hydrator_factory = coordinator._build_metadata_hydrator_factory(object())
    scrape_factory = coordinator._build_metadata_scrape_service_factory(object())
    vod = VodItem(
        vod_id="113367389900623-26520061025-1112251",
        vod_name="牧神记",
        detail_fields=[
            PlaybackDetailField(
                label="Season ID",
                value_parts=[
                    PlaybackDetailValuePart(
                        label="45969",
                        action=PlaybackDetailFieldAction(type="link", value="season$45969", target="bilibili"),
                    )
                ],
            )
        ],
    )

    hydrate = hydrator_factory(source_kind="bilibili", vod=vod)
    scrape_service = scrape_factory(source_kind="bilibili", vod=vod)

    assert callable(hydrate)
    assert scrape_service is not None


def test_app_coordinator_metadata_factories_support_telegram_source(monkeypatch, tmp_path) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_douban_cookie="",
                metadata_tmdb_api_key="",
            )

    class RecordingBangumiProvider:
        name = "bangumi"

        def __init__(self, client) -> None:
            self.client = client

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    class RecordingRemoteDoubanProvider:
        name = "local_douban"

        def __init__(self, api_client) -> None:
            self.api_client = api_client

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    coordinator = AppCoordinator(FakeRepo())
    api_client = object()

    monkeypatch.setattr(app_module, "BangumiClient", lambda access_token="", proxy_decider=None: object(), raising=False)
    monkeypatch.setattr(app_module, "BangumiMetadataProvider", RecordingBangumiProvider, raising=False)
    monkeypatch.setattr(app_module, "BilibiliMetadataProvider", lambda: type("P", (), {"name": "bilibili", "can_enrich": lambda self, _: False, "search": lambda self, _: [], "get_detail": lambda self, _: None})(), raising=False)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", lambda: type("P", (), {"name": "iqiyi", "can_enrich": lambda self, _: False, "search": lambda self, _: [], "get_detail": lambda self, _: None})(), raising=False)
    monkeypatch.setattr(app_module, "TencentMetadataProvider", lambda: type("P", (), {"name": "tencent", "can_enrich": lambda self, _: False, "search": lambda self, _: [], "get_detail": lambda self, _: None})(), raising=False)
    monkeypatch.setattr(app_module, "LocalDoubanProvider", RecordingRemoteDoubanProvider, raising=False)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    hydrator_factory = coordinator._build_metadata_hydrator_factory(api_client)
    scrape_factory = coordinator._build_metadata_scrape_service_factory(api_client)

    for source_kind in ("telegram", "telegram_channel"):
        assert callable(hydrator_factory(source_kind=source_kind, vod=VodItem(vod_id="v1", vod_name="成何体统")))
        assert scrape_factory(source_kind=source_kind, vod=VodItem(vod_id="v1", vod_name="成何体统")) is not None


def test_app_coordinator_scrape_service_skips_local_douban_and_tmdb_without_required_config(monkeypatch, tmp_path) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_douban_cookie="",
                metadata_tmdb_api_key="",
            )

    class RecordingLocalDoubanClient:
        def __init__(self, cookie: str = "", transport=None, proxy_decider=None) -> None:
            del cookie, transport, proxy_decider
            raise AssertionError("local douban client should not be created without cookie")

    class RecordingLocalDoubanProvider:
        name = "local_douban"

        def __init__(self, local_client) -> None:
            raise AssertionError(f"unexpected local douban provider: {local_client!r}")

    class RecordingTMDBClient:
        def __init__(self, api_key: str, transport=None, proxy_decider=None) -> None:
            del api_key, transport, proxy_decider
            raise AssertionError("tmdb client should not be created without api key")

    class RecordingTMDBProvider:
        name = "tmdb"

        def __init__(self, client) -> None:
            raise AssertionError(f"unexpected tmdb provider: {client!r}")

    bangumi_tokens: list[str] = []

    class RecordingBangumiClient:
        def __init__(self, access_token: str = "", transport=None, proxy_decider=None) -> None:
            del transport, proxy_decider
            bangumi_tokens.append(access_token)

    class RecordingBangumiProvider:
        name = "bangumi"

        def __init__(self, client) -> None:
            self.client = client

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    class RecordingRemoteDoubanProvider:
        name = "remote_douban"

        def __init__(self, api_client) -> None:
            self.api_client = api_client

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    coordinator = AppCoordinator(FakeRepo())
    api_client = object()

    monkeypatch.setattr(app_module, "BilibiliMetadataProvider", lambda: type("P", (), {"name": "bilibili"})(), raising=False)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", lambda: type("P", (), {"name": "iqiyi"})(), raising=False)
    monkeypatch.setattr(app_module, "TencentMetadataProvider", lambda: type("P", (), {"name": "tencent"})(), raising=False)
    monkeypatch.setattr(app_module, "YoukuMetadataProvider", lambda: type("P", (), {"name": "youku"})(), raising=False)
    monkeypatch.setattr(app_module, "SohuMetadataProvider", lambda: type("P", (), {"name": "sohu"})(), raising=False)
    monkeypatch.setattr(app_module, "LocalDoubanClient", RecordingLocalDoubanClient)
    monkeypatch.setattr(app_module, "OfficialDoubanProvider", RecordingLocalDoubanProvider, raising=False)
    monkeypatch.setattr(app_module, "TMDBClient", RecordingTMDBClient, raising=False)
    monkeypatch.setattr(app_module, "TMDBProvider", RecordingTMDBProvider, raising=False)
    monkeypatch.setattr(app_module, "BangumiClient", RecordingBangumiClient, raising=False)
    monkeypatch.setattr(app_module, "BangumiMetadataProvider", RecordingBangumiProvider, raising=False)
    monkeypatch.setattr(app_module, "LocalDoubanProvider", RecordingRemoteDoubanProvider, raising=False)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    factory = coordinator._build_metadata_scrape_service_factory(api_client)
    service = factory(source_kind="browse", vod=VodItem(vod_id="v1", vod_name="深空彼岸"))

    assert service is not None
    assert [provider.name for provider in service._providers] == [
        "bangumi",
        "bilibili",
        "iqiyi",
        "tencent",
        "youku",
        "sohu",
        "remote_douban",
    ]
    assert bangumi_tokens == [""]


def test_app_coordinator_scrape_service_excludes_disabled_metadata_sources(monkeypatch, tmp_path) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_douban_cookie="bid=demo;",
                metadata_tmdb_api_key="tmdb-key",
                disabled_metadata_provider_ids=["iqiyi", "tmdb", "official_douban", "migu"],
            )

    class RecordingProvider:
        def __init__(self, name: str) -> None:
            self.name = name

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    monkeypatch.setattr(app_module, "BangumiClient", lambda access_token="", proxy_decider=None: object(), raising=False)
    monkeypatch.setattr(app_module, "BangumiMetadataProvider", lambda client: RecordingProvider("bangumi"), raising=False)
    monkeypatch.setattr(app_module, "BilibiliMetadataProvider", lambda: RecordingProvider("bilibili"), raising=False)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", lambda: RecordingProvider("iqiyi"), raising=False)
    monkeypatch.setattr(app_module, "TencentMetadataProvider", lambda: RecordingProvider("tencent"), raising=False)
    monkeypatch.setattr(app_module, "YoukuMetadataProvider", lambda: RecordingProvider("youku"), raising=False)
    monkeypatch.setattr(app_module, "SohuMetadataProvider", lambda: RecordingProvider("sohu"), raising=False)
    monkeypatch.setattr(app_module, "LocalDoubanClient", lambda cookie="", proxy_decider=None: object(), raising=False)
    monkeypatch.setattr(app_module, "OfficialDoubanProvider", lambda client: RecordingProvider("official_douban"), raising=False)
    monkeypatch.setattr(app_module, "TMDBClient", lambda api_key="", proxy_decider=None: object(), raising=False)
    monkeypatch.setattr(app_module, "TMDBProvider", lambda client: RecordingProvider("tmdb"), raising=False)
    monkeypatch.setattr(app_module, "LocalDoubanProvider", lambda api_client: RecordingProvider("local_douban"), raising=False)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    coordinator = AppCoordinator(FakeRepo())
    factory = coordinator._build_metadata_scrape_service_factory(object())
    service = factory(source_kind="browse", vod=VodItem(vod_id="v1", vod_name="深空彼岸"))

    assert service is not None
    assert [provider.name for provider in service._providers] == [
        "bangumi",
        "bilibili",
        "tencent",
        "youku",
        "sohu",
        "local_douban",
    ]


def test_metadata_context_to_query_includes_original_base_match_fields() -> None:
    vod = VodItem(
        vod_id="vod-1",
        vod_name="深空彼岸",
        vod_year="2026",
        vod_area="中国大陆",
        vod_lang="汉语普通话",
        vod_director="周琛,赵禹晴",
        vod_actor="梁达伟,唐雅菁",
        category_name="动漫",
    )

    query = MetadataContext(vod=vod, source_kind="spider").to_query()

    assert query.vod_area == "中国大陆"
    assert query.vod_lang == "汉语普通话"
    assert query.vod_director == "周琛,赵禹晴"
    assert query.vod_actor == "梁达伟,唐雅菁"


def test_app_coordinator_builds_iqiyi_metadata_provider(monkeypatch, tmp_path) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig(
                metadata_enhancement_enabled=True,
                metadata_douban_cookie="",
                metadata_tmdb_api_key="",
            )

    class RecordingIqiyiProvider:
        name = "iqiyi"

        def __init__(self) -> None:
            created.append("iqiyi")

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    class RecordingTencentProvider:
        name = "tencent"

        def __init__(self) -> None:
            created.append("tencent")

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    class RecordingRemoteDoubanProvider:
        name = "local_douban"

        def __init__(self, api_client) -> None:
            del api_client
            created.append("local_douban")

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    class RecordingBilibiliProvider:
        name = "bilibili"

        def __init__(self) -> None:
            created.append("bilibili")

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    class RecordingSohuProvider:
        name = "sohu"

        def __init__(self) -> None:
            created.append("sohu")

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    class RecordingYoukuProvider:
        name = "youku"

        def __init__(self) -> None:
            created.append("youku")

        def can_enrich(self, _context) -> bool:
            return False

        def search(self, _candidate):
            return []

        def get_detail(self, _match):
            raise AssertionError("not used")

    created: list[str] = []
    coordinator = AppCoordinator(FakeRepo())
    monkeypatch.setattr(app_module, "BilibiliMetadataProvider", RecordingBilibiliProvider, raising=False)
    monkeypatch.setattr(app_module, "IqiyiMetadataProvider", RecordingIqiyiProvider, raising=False)
    monkeypatch.setattr(app_module, "TencentMetadataProvider", RecordingTencentProvider, raising=False)
    monkeypatch.setattr(app_module, "YoukuMetadataProvider", RecordingYoukuProvider, raising=False)
    monkeypatch.setattr(app_module, "SohuMetadataProvider", RecordingSohuProvider, raising=False)
    monkeypatch.setattr(app_module, "LocalDoubanProvider", RecordingRemoteDoubanProvider, raising=False)
    monkeypatch.setattr(app_module, "app_cache_dir", lambda: tmp_path / "app-cache")

    factory = coordinator._build_metadata_scrape_service_factory(object())
    service = factory(source_kind="browse", vod=VodItem(vod_id="v1", vod_name="剑来 第二季"))

    assert service is not None
    assert [provider.name for provider in service._providers] == ["bangumi", "bilibili", "iqiyi", "tencent", "youku", "sohu", "local_douban"]
    assert created == ["bilibili", "iqiyi", "tencent", "youku", "sohu", "local_douban"]


def test_main_window_restore_last_player_routes_bilibili_detail_to_bilibili_controller(qtbot, monkeypatch) -> None:
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

    controller = FakeBilibiliController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_source="bilibili",
        last_playback_mode="detail",
        last_playback_vod_id="BV1xx411c7mD",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        bilibili_controller=controller,
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
        show_bilibili_tab=True,
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    assert window.player_window.opened[0][0]["vod"].vod_name == "B站视频"
    assert window.player_window.opened[0][1] is True


def test_main_window_restore_last_player_routes_youtube_detail_to_youtube_controller(qtbot, monkeypatch) -> None:
    class RestoreBrowseController:
        def build_request_from_detail(self, vod_id: str):
            raise AssertionError(f"browse restore should not be used for {vod_id}")

    class RecordingYoutubeController(FakeYoutubeController):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[str] = []

        def build_request(self, vod_id: str):
            self.calls.append(vod_id)
            request = super().build_request(vod_id)
            request.source_kind = "youtube"
            request.use_local_history = False
            return request

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

    controller = RecordingYoutubeController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_source="youtube",
        last_playback_mode="detail",
        last_playback_vod_id="yt:video:abc123",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        youtube_controller=controller,
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
        show_youtube_tab=True,
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    assert controller.calls == ["yt:video:abc123"]
    assert window.player_window.opened[0][0]["vod"].vod_name == "YouTube视频"
    assert window.player_window.opened[0][0]["use_local_history"] is False
    assert window.player_window.opened[0][1] is True


def test_app_coordinator_starts_epg_and_remote_live_refresh_in_background(monkeypatch, tmp_path) -> None:
    class FakeRepo(_FakeRepoBase):
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
    monkeypatch.setattr(
        app_module,
        "SpiderPluginManager",
        lambda repository, loader, playback_history_repository, **_kwargs: FakePluginManager(),
    )
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


def test_start_live_background_refresh_skips_recent_epg_and_sources(monkeypatch) -> None:
    class ImmediateThread:
        def __init__(self, target, daemon=None):
            del daemon
            self._target = target

        def start(self) -> None:
            self._target()

    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig()

    class FakeEpgService:
        def __init__(self) -> None:
            self.refresh_calls = 0

        def load_config(self):
            return type(
                "Config",
                (),
                {"epg_url": "https://example.com/epg.xml", "last_refreshed_at": 1_713_168_000},
            )()

        def refresh(self) -> None:
            self.refresh_calls += 1

    class FakeLiveSourceManager:
        def __init__(self) -> None:
            self.refresh_calls: list[int] = []

        def list_sources(self):
            return [
                type("Source", (), {"id": 1, "source_type": "remote", "last_refreshed_at": 1_713_168_000})(),
                type("Source", (), {"id": 2, "source_type": "local", "last_refreshed_at": 1_713_168_000})(),
                type("Source", (), {"id": 3, "source_type": "manual", "last_refreshed_at": 1_713_168_000})(),
            ]

        def refresh_source(self, source_id: int) -> None:
            self.refresh_calls.append(source_id)

    monkeypatch.setattr(app_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr("atv_player.app.time.time", lambda: 1_713_171_000)
    coordinator = AppCoordinator(FakeRepo())
    epg_service = FakeEpgService()
    live_source_manager = FakeLiveSourceManager()

    coordinator._start_live_background_refresh(live_source_manager, epg_service)

    assert epg_service.refresh_calls == 0
    assert live_source_manager.refresh_calls == []


def test_start_live_background_refresh_refreshes_stale_epg_and_non_manual_sources(monkeypatch) -> None:
    class ImmediateThread:
        def __init__(self, target, daemon=None):
            del daemon
            self._target = target

        def start(self) -> None:
            self._target()

    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig()

    class FakeEpgService:
        def __init__(self) -> None:
            self.refresh_calls = 0

        def load_config(self):
            return type(
                "Config",
                (),
                {"epg_url": "https://example.com/epg.xml", "last_refreshed_at": 12},
            )()

        def refresh(self) -> None:
            self.refresh_calls += 1

    class FakeLiveSourceManager:
        def __init__(self) -> None:
            self.refresh_calls: list[int] = []

        def list_sources(self):
            return [
                type("Source", (), {"id": 1, "source_type": "remote", "last_refreshed_at": 12})(),
                type("Source", (), {"id": 2, "source_type": "local", "last_refreshed_at": 1_713_150_000})(),
                type("Source", (), {"id": 3, "source_type": "manual", "last_refreshed_at": 0})(),
            ]

        def refresh_source(self, source_id: int) -> None:
            self.refresh_calls.append(source_id)

    monkeypatch.setattr(app_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr("atv_player.app.time.time", lambda: 1_713_171_000)
    coordinator = AppCoordinator(FakeRepo())
    epg_service = FakeEpgService()
    live_source_manager = FakeLiveSourceManager()

    coordinator._start_live_background_refresh(live_source_manager, epg_service)

    assert epg_service.refresh_calls == 1
    assert live_source_manager.refresh_calls == [1, 2]


def test_start_live_background_refresh_logs_failures_with_live_category(monkeypatch, caplog) -> None:
    class ImmediateThread:
        def __init__(self, target, daemon=None):
            del daemon
            self._target = target

        def start(self) -> None:
            self._target()

    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig()

    class FailingEpgService:
        def load_config(self):
            return type("Config", (), {"epg_url": "https://example.com/epg.xml", "last_refreshed_at": 12})()

        def refresh(self) -> None:
            raise RuntimeError("epg boom")

    class FakeLiveSourceManager:
        def list_sources(self):
            return []

    monkeypatch.setattr(app_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr("atv_player.app.time.time", lambda: 1_713_171_000)
    coordinator = AppCoordinator(FakeRepo())

    with caplog.at_level(logging.ERROR):
        coordinator._start_live_background_refresh(FakeLiveSourceManager(), FailingEpgService())

    record = caplog.records[-1]
    assert record.message == "Background refresh failed target=epg"
    assert getattr(record, "log_category", "") == "live"


def test_metadata_hydrator_logs_search_failures_with_metadata_category(tmp_path, caplog) -> None:
    class FailingProvider:
        name = "tmdb"

        def search(self, query):
            del query
            raise RuntimeError("search boom")

    hydrator = MetadataHydrator(cache=MetadataCache(tmp_path), providers=[FailingProvider()])

    with caplog.at_level(logging.WARNING):
        matches = hydrator._load_provider_matches(FailingProvider(), MetadataQuery(title="测试剧"))

    assert matches == []
    record = caplog.records[-1]
    assert record.message == "Metadata provider search failed provider=tmdb"
    assert getattr(record, "log_category", "") == "metadata"


def test_app_coordinator_show_main_keeps_window_open_when_initial_browse_times_out(
    qtbot,
    monkeypatch,
) -> None:
    class FakeRepo(_FakeRepoBase):
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
    class FakeRepo(_FakeRepoBase):
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


def test_app_coordinator_show_login_closes_active_api_client(monkeypatch) -> None:
    class FakeRepo(_FakeRepoBase):
        def load_config(self) -> AppConfig:
            return AppConfig()

    class FakeLoginWindow:
        login_succeeded = type("SignalStub", (), {"connect": lambda self, cb: None})()

        def __init__(self, controller) -> None:
            self.controller = controller

    class ClosableClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    coordinator = AppCoordinator(FakeRepo())
    client = ClosableClient()
    coordinator._api_client = client
    monkeypatch.setattr(app_module, "LoginWindow", FakeLoginWindow)

    coordinator._show_login()

    assert client.closed is True
    assert coordinator._api_client is None


def test_fit_rect_within_available_screens_keeps_window_on_matching_secondary_screen() -> None:
    fitted = main_window_module._fit_rect_within_available_screens(
        QRect(2200, 540, 1100, 900),
        [QRect(0, 0, 1920, 1080), QRect(1920, 0, 1280, 720)],
    )

    assert fitted == QRect(2100, 0, 1100, 720)


def test_fit_rect_within_available_screens_uses_nearest_screen_when_saved_rect_no_longer_intersects() -> None:
    fitted = main_window_module._fit_rect_within_available_screens(
        QRect(2200, 900, 1000, 400),
        [QRect(0, 0, 1920, 1080), QRect(1920, 0, 1280, 720)],
    )

    assert fitted == QRect(2200, 320, 1000, 400)


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


def test_main_window_show_or_restore_player_hides_global_search_popup(qtbot) -> None:
    class ExistingPlayerWindow:
        def __init__(self) -> None:
            self.session = object()

        def show(self) -> None:
            return None

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

    window = MainWindow(
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(last_active_window="main", global_search_history=["庆余年"]),
        save_config=lambda: None,
        global_search_hotkey_loader=lambda hot_type: [{"title": "热搜一", "query": "热搜一"}],
    )
    qtbot.addWidget(window)
    window.player_window = ExistingPlayerWindow()
    window.show()

    qtbot.mouseClick(window.global_search_popup_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window._global_search_popup.isVisible() is True)

    window.show_or_restore_player()

    assert window._global_search_popup.isVisible() is False
    assert window.isHidden() is True


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


def test_main_window_return_from_player_close_keeps_existing_player_window(qtbot) -> None:
    class ExistingPlayerWindow:
        def __init__(self) -> None:
            self.session = object()
            self._close_event_returns_to_main = True

    config = AppConfig(last_active_window="player")
    window = MainWindow(
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    player_window = ExistingPlayerWindow()
    window.player_window = player_window

    window._show_main_again()

    assert window.player_window is player_window
    assert config.last_active_window == "main"


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


def test_main_window_restore_last_player_routes_telegram_detail_with_playback_history_loader(
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

    class RestoreTelegramController:
        def build_request(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="电报影视剧"),
                playlist=[PlayItem(title="第2集", url="https://media.example/2.m3u8")],
                clicked_index=0,
                source_kind="telegram",
                source_mode="detail",
                source_vod_id=vod_id,
                use_local_history=False,
                playback_history_loader=lambda: HistoryRecord(
                    id=0,
                    key=vod_id,
                    vod_name="电报影视剧",
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
        last_playback_source="telegram",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        telegram_controller=RestoreTelegramController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    session = window.player_window.opened[0][0]
    assert session["vod"].vod_name == "电报影视剧"
    assert session["use_local_history"] is False
    assert session["playback_history_loader"] is not None
    assert session["playback_history_loader"]().position == 45000
    assert window.player_window.opened[0][1] is True


def test_main_window_restore_last_player_routes_telegram_channel_detail_to_channel_controller(
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

    controller = FakeTelegramChannelController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_source="telegram_channel",
        last_playback_mode="detail",
        last_playback_vod_id="tg-channel-vod-1",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        telegram_channel_controller=controller,
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    assert controller.build_calls == ["tg-channel-vod-1"]
    assert window.player_window.opened[0][0]["vod"].vod_name == "Telegram Channel Movie"
    assert window.player_window.opened[0][0]["source_kind"] == "telegram_channel"
    assert window.player_window.opened[0][1] is True


def test_main_window_restore_last_player_routes_jellyfin_detail_to_jellyfin_controller(qtbot, monkeypatch) -> None:
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

    controller = FakeJellyfinController()
    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig(
        last_active_window="player",
        last_playback_source="jellyfin",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
        last_player_paused=True,
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        jellyfin_controller=controller,
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    restored = window.restore_last_player()

    assert restored is window.player_window
    assert window.player_window.opened[0][0]["vod"].vod_name == "Jellyfin Movie"
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

        def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
            del filters
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
