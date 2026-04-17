import threading
import time

import pytest

from atv_player.models import AppConfig, OpenPlayerRequest, PlayItem, VodItem
import atv_player.ui.main_window as main_window_module
from atv_player.ui.main_window import MainWindow


class FakeStaticController:
    def load_categories(self):
        return []

    def load_items(self, category_id: str, page: int):
        return [], 0


class FakeSpiderController:
    def __init__(self, name: str) -> None:
        self.name = name
        self.open_calls: list[str] = []

    def load_categories(self):
        return []

    def load_items(self, category_id: str, page: int):
        return [], 0

    def build_request(self, vod_id: str):
        self.open_calls.append(vod_id)
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name=self.name),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.m3u8")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )


class FakePluginManager:
    def __init__(self) -> None:
        self.dialog_opened = 0


class FakePlayerController:
    def create_session(
        self,
        vod,
        playlist,
        clicked_index: int,
        detail_resolver=None,
        resolved_vod_by_id=None,
        use_local_history=True,
        restore_history=False,
        playback_loader=None,
        playback_progress_reporter=None,
        playback_stopper=None,
    ):
        return {
            "vod": vod,
            "playlist": playlist,
            "clicked_index": clicked_index,
            "restore_history": restore_history,
        }


class AsyncOpenController(FakeStaticController):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._event = threading.Event()

    def build_request(self, vod_id: str):
        self.calls.append(vod_id)
        assert self._event.wait(timeout=5), "open request was never released"
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="Movie"),
            playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )

    def release(self) -> None:
        self._event.set()


class AsyncMediaController(FakeStaticController):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._event = threading.Event()

    def load_folder_items(self, vod_id: str):
        self.calls.append(vod_id)
        assert self._event.wait(timeout=5), "media load was never released"
        return [VodItem(vod_id="m1", vod_name="Movie")], 1

    def release(self) -> None:
        self._event.set()


class AsyncRestoreController(FakeStaticController):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._event = threading.Event()

    def build_request_from_detail(self, vod_id: str):
        self.calls.append(vod_id)
        assert self._event.wait(timeout=5), "restore request was never released"
        return OpenPlayerRequest(
            vod=VodItem(vod_id=vod_id, vod_name="Movie"),
            playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
            clicked_index=0,
            source_mode="detail",
            source_vod_id=vod_id,
        )

    def release(self) -> None:
        self._event.set()


def test_main_window_inserts_dynamic_spider_tabs_before_browse(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        spider_plugins=[
            {"title": "红果短剧", "controller": FakeSpiderController("红果短剧"), "search_enabled": True},
            {"title": "短剧二号", "controller": FakeSpiderController("短剧二号"), "search_enabled": False},
        ],
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    assert [window.nav_tabs.tabText(i) for i in range(window.nav_tabs.count())] == [
        "豆瓣电影",
        "电报影视",
        "网络直播",
        "Emby",
        "Jellyfin",
        "红果短剧",
        "短剧二号",
        "文件浏览",
        "播放记录",
    ]
    assert window.plugin_manager_button.text() == "插件管理"


def test_main_window_shows_live_source_manager_button_after_plugin_manager(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeStaticController(),
        telegram_controller=FakeStaticController(),
        live_controller=FakeStaticController(),
        emby_controller=FakeStaticController(),
        jellyfin_controller=FakeStaticController(),
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
        live_source_manager=object(),
        plugin_manager=FakePluginManager(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.plugin_manager_button.text() == "插件管理"
    assert window.live_source_manager_button.text() == "直播源管理"


def test_main_window_open_player_creates_session_without_blocking_ui(qtbot, monkeypatch) -> None:
    class FakeSignal:
        def connect(self, _callback) -> None:
            return None

    class SlowPlayerController(FakePlayerController):
        def create_session(
            self,
            vod,
            playlist,
            clicked_index: int,
            detail_resolver=None,
            resolved_vod_by_id=None,
            use_local_history=True,
            restore_history=False,
            playback_loader=None,
            playback_progress_reporter=None,
            playback_stopper=None,
        ):
            time.sleep(0.15)
            return super().create_session(
                vod,
                playlist,
                clicked_index,
                detail_resolver=detail_resolver,
                resolved_vod_by_id=resolved_vod_by_id,
                use_local_history=use_local_history,
                restore_history=restore_history,
                playback_loader=playback_loader,
                playback_progress_reporter=playback_progress_reporter,
                playback_stopper=playback_stopper,
            )

    class RecordingPlayerWindow:
        def __init__(self, controller, config, save_config) -> None:
            self.opened: list[tuple[object, bool]] = []
            self.show_calls = 0
            self.raise_calls = 0
            self.activate_calls = 0
            self.closed_to_main = FakeSignal()

        def open_session(self, session, start_paused: bool = False) -> None:
            self.opened.append((session, start_paused))

        def show(self) -> None:
            self.show_calls += 1

        def raise_(self) -> None:
            self.raise_calls += 1

        def activateWindow(self) -> None:
            self.activate_calls += 1

    monkeypatch.setattr(main_window_module, "PlayerWindow", RecordingPlayerWindow)
    config = AppConfig()
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=SlowPlayerController(),
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

    started_at = time.perf_counter()
    window.open_player(request)
    elapsed_seconds = time.perf_counter() - started_at

    assert elapsed_seconds < 0.1
    assert window.isHidden() is False
    assert window.player_window is None

    qtbot.waitUntil(lambda: window.player_window is not None and len(window.player_window.opened) == 1)
    assert window.isHidden() is True
    assert config.last_active_window == "player"
    assert config.last_playback_mode == "detail"
    assert config.last_playback_vod_id == "vod-1"
    assert config.last_player_paused is False


def test_main_window_async_restore_failure_resets_last_active_window(qtbot) -> None:
    class FailingBrowseController(FakeStaticController):
        def build_request_from_detail(self, vod_id: str):
            raise RuntimeError(f"failed to restore {vod_id}")

    saved = {"count": 0}
    config = AppConfig(
        last_active_window="player",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
    )
    window = MainWindow(
        browse_controller=FailingBrowseController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)

    window._start_restore_last_player()

    qtbot.waitUntil(lambda: config.last_active_window == "main")
    assert saved["count"] >= 1


def test_main_window_async_restore_without_saved_request_resets_last_active_window(qtbot) -> None:
    saved = {"count": 0}
    config = AppConfig(last_active_window="player")
    window = MainWindow(
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)

    window._start_restore_last_player()

    qtbot.waitUntil(lambda: config.last_active_window == "main")
    assert saved["count"] >= 1


def test_main_window_async_restore_session_creation_failure_resets_last_active_window(qtbot) -> None:
    class RestoreBrowseController(FakeStaticController):
        def build_request_from_detail(self, vod_id: str):
            return OpenPlayerRequest(
                vod=VodItem(vod_id=vod_id, vod_name="Movie"),
                playlist=[PlayItem(title="Episode 1", url="1.m3u8")],
                clicked_index=0,
                source_mode="detail",
                source_vod_id=vod_id,
            )

    class FailingPlayerController(FakePlayerController):
        def create_session(
            self,
            vod,
            playlist,
            clicked_index: int,
            detail_resolver=None,
            resolved_vod_by_id=None,
            use_local_history=True,
            restore_history=False,
            playback_loader=None,
            playback_progress_reporter=None,
            playback_stopper=None,
        ):
            raise RuntimeError("session failed")

    saved = {"count": 0}
    errors: list[str] = []
    config = AppConfig(
        last_active_window="player",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
    )
    window = MainWindow(
        browse_controller=RestoreBrowseController(),
        history_controller=FakeStaticController(),
        player_controller=FailingPlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)
    window.show_error = errors.append

    window._start_restore_last_player()

    qtbot.waitUntil(lambda: config.last_active_window == "main")
    assert errors == ["session failed"]
    assert saved["count"] >= 1


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_main_window_ignores_async_open_request_after_window_deletion(qtbot) -> None:
    controller = AsyncOpenController()
    window = MainWindow(
        telegram_controller=controller,
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    destroyed = {"count": 0}
    window.destroyed.connect(lambda *_args: destroyed.__setitem__("count", destroyed["count"] + 1))

    window._handle_telegram_open_requested("vod-1")
    qtbot.waitUntil(lambda: controller.calls == ["vod-1"], timeout=1000)

    window.deleteLater()
    qtbot.waitUntil(lambda: destroyed["count"] == 1, timeout=1000)

    controller.release()
    qtbot.wait(100)

    assert destroyed["count"] == 1


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_main_window_ignores_async_media_load_after_window_deletion(qtbot) -> None:
    controller = AsyncMediaController()
    window = MainWindow(
        live_controller=controller,
        browse_controller=FakeStaticController(),
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    destroyed = {"count": 0}
    window.destroyed.connect(lambda *_args: destroyed.__setitem__("count", destroyed["count"] + 1))

    item = type("Item", (), {"vod_id": "folder-1", "vod_name": "Folder", "vod_tag": "folder"})()
    window._open_media_folder(window.live_page, controller, item)
    qtbot.waitUntil(lambda: controller.calls == ["folder-1"], timeout=1000)

    window.deleteLater()
    qtbot.waitUntil(lambda: destroyed["count"] == 1, timeout=1000)

    controller.release()
    qtbot.wait(100)

    assert destroyed["count"] == 1


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_main_window_ignores_async_restore_after_window_deletion(qtbot) -> None:
    controller = AsyncRestoreController()
    config = AppConfig(
        last_active_window="player",
        last_playback_mode="detail",
        last_playback_vod_id="vod-1",
    )
    window = MainWindow(
        browse_controller=controller,
        history_controller=FakeStaticController(),
        player_controller=FakePlayerController(),
        config=config,
    )
    destroyed = {"count": 0}
    window.destroyed.connect(lambda *_args: destroyed.__setitem__("count", destroyed["count"] + 1))

    window._start_restore_last_player()
    qtbot.waitUntil(lambda: controller.calls == ["vod-1"], timeout=1000)

    window.deleteLater()
    qtbot.waitUntil(lambda: destroyed["count"] == 1, timeout=1000)

    controller.release()
    qtbot.wait(100)

    assert destroyed["count"] == 1
