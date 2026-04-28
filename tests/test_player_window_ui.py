import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QEvent, QObject, QRect, Qt, Signal
from PySide6.QtGui import QAction, QColor, QContextMenuEvent, QCursor, QIcon, QImage, QKeyEvent, QMouseEvent, QPixmap, QWindow
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QMenu, QTableWidget, QWidget
from PySide6.QtWidgets import QSplitter, QToolTip
from atv_player.controllers.player_controller import PlayerSession
from atv_player.danmaku.models import DanmakuSourceGroup, DanmakuSourceOption
from atv_player.models import AppConfig, PlayItem, PlaybackLoadResult, VodItem
from atv_player.player.mpv_widget import AudioTrack, SubtitleTrack

import atv_player.ui.poster_loader as poster_loader_module
import atv_player.ui.player_window as player_window_module
from atv_player.ui.player_window import PlayerWindow


class FakePlayerController:
    def report_progress(
        self,
        session,
        current_index: int,
        position_seconds: int,
        speed: float,
        opening_seconds: int,
        ending_seconds: int,
        paused: bool,
        force_remote_report: bool = False,
    ) -> None:
        return None

    def resolve_play_item_detail(self, session, play_item):
        return None

    def stop_playback(self, session, current_index: int) -> None:
        return None


class RecordingPlayerController(FakePlayerController):
    def __init__(self) -> None:
        self.progress_calls: list[tuple[int, int, float, int, int, bool]] = []
        self.force_remote_report_calls: list[bool] = []
        self.stop_calls: list[int] = []

    def report_progress(
        self,
        session,
        current_index: int,
        position_seconds: int,
        speed: float,
        opening_seconds: int,
        ending_seconds: int,
        paused: bool,
        force_remote_report: bool = False,
    ) -> None:
        self.progress_calls.append((current_index, position_seconds, speed, opening_seconds, ending_seconds, paused))
        self.force_remote_report_calls.append(force_remote_report)

    def resolve_play_item_detail(self, session, play_item):
        if not play_item.vod_id or session.detail_resolver is None:
            return None
        if play_item.vod_id in session.resolved_vod_by_id:
            resolved_vod = session.resolved_vod_by_id[play_item.vod_id]
        else:
            resolved_vod = session.detail_resolver(play_item)
            session.resolved_vod_by_id[play_item.vod_id] = resolved_vod
        play_item.url = resolved_vod.items[0].url if resolved_vod.items else resolved_vod.vod_play_url
        return resolved_vod

    def stop_playback(self, session, current_index: int) -> None:
        self.stop_calls.append(current_index)


class RecordingVideo:
    def __init__(self) -> None:
        self.load_calls: list[tuple[str, int]] = []
        self.pause_calls = 0
        self.resume_calls = 0
        self.toggle_mute_calls = 0
        self.seek_relative_calls: list[int] = []
        self.set_speed_calls: list[float] = []
        self.set_volume_calls: list[int] = []

    def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
        self.load_calls.append((url, start_seconds))

    def set_speed(self, value: float) -> None:
        self.set_speed_calls.append(value)

    def set_volume(self, value: int) -> None:
        self.set_volume_calls.append(value)

    def pause(self) -> None:
        self.pause_calls += 1

    def resume(self) -> None:
        self.resume_calls += 1

    def toggle_mute(self) -> None:
        self.toggle_mute_calls += 1

    def seek_relative(self, seconds: int) -> None:
        self.seek_relative_calls.append(seconds)

    def position_seconds(self) -> int:
        return 30

    def duration_seconds(self) -> int:
        return 120


def make_player_session(start_index: int = 1, speed: float = 1.0) -> PlayerSession:
    return PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(title="Episode 1", url="http://m/1.m3u8"),
            PlayItem(title="Episode 2", url="http://m/2.m3u8"),
            PlayItem(title="Episode 3", url="http://m/3.m3u8"),
        ],
        start_index=start_index,
        start_position_seconds=0,
        speed=speed,
        opening_seconds=0,
        ending_seconds=0,
    )


def send_key(window: PlayerWindow, key: int, modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier, text: str = "") -> None:
    QApplication.sendEvent(window, QKeyEvent(QEvent.Type.KeyPress, key, modifiers, text))
    QApplication.sendEvent(window, QKeyEvent(QEvent.Type.KeyRelease, key, modifiers, text))


def release_event_after(delay_seconds: float, event: threading.Event) -> None:
    def run() -> None:
        time.sleep(delay_seconds)
        event.set()

    threading.Thread(target=run, daemon=True).start()


def _submenu_actions(menu: QMenu, title: str) -> list[QAction]:
    submenu = next(action.menu() for action in menu.actions() if action.text() == title)
    assert submenu is not None
    return submenu.actions()


def test_player_window_has_reasonable_default_size_and_horizontal_progress(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())

    qtbot.addWidget(window)
    window.show()

    assert window.width() >= 1000
    assert window.height() >= 700
    assert window.progress.orientation() == Qt.Orientation.Horizontal
    assert window.current_time_label.text() == "00:00"
    assert window.duration_label.text() == "00:00"
    assert window.volume_layout.indexOf(window.mute_button) == 0
    assert window.volume_layout.indexOf(window.volume_slider) == 1
    assert window.volume_slider.maximumWidth() == 180
    assert window.bottom_area.maximumHeight() == 72
    assert window.bottom_layout.spacing() == 4
    assert window.opening_spin.prefix() == "片头 "
    assert window.ending_spin.prefix() == "片尾 "


def test_player_window_icon_updates_use_cached_icon_loader(qtbot, monkeypatch) -> None:
    calls: list[str] = []

    def fake_load_icon(path) -> QIcon:
        calls.append(str(path))
        return QIcon()

    monkeypatch.setattr(player_window_module, "load_icon", fake_load_icon)

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    calls.clear()

    window.is_playing = True
    window._update_play_button_icon()
    window.is_playing = False
    window._update_play_button_icon()
    window._is_muted = True
    window._update_mute_button_icon()
    window._is_muted = False
    window._update_mute_button_icon()

    assert calls == [
        str(window._icons_dir / "pause.svg"),
        str(window._icons_dir / "play.svg"),
        str(window._icons_dir / "volume-off.svg"),
        str(window._icons_dir / "volume-on.svg"),
    ]


def test_player_window_shows_danmaku_source_button_with_custom_icon(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.danmaku_source_button.toolTip() == "弹幕源"
    assert window.danmaku_source_button.isEnabled() is False


def test_player_window_video_context_menu_contains_danmaku_source_action_when_candidates_exist(qtbot) -> None:
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
            )
        ],
    )
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    menu = window._build_video_context_menu()

    assert any(action.text() == "弹幕源" for action in menu.actions())


def test_player_window_opens_danmaku_source_dialog_for_current_item(qtbot) -> None:
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
            )
        ],
        selected_danmaku_provider="tencent",
        selected_danmaku_url="https://v.qq.com/demo",
        selected_danmaku_title="红果短剧 第1集",
        danmaku_search_query="红果短剧 1集",
    )
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    window._open_danmaku_source_dialog()

    assert window._danmaku_source_dialog is not None
    assert window._danmaku_source_query_edit.text() == "红果短剧 1集"
    assert window._danmaku_source_provider_list.count() == 1


def test_player_window_reset_danmaku_source_query_restores_default(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def refresh_danmaku_sources(self, item: PlayItem, query_override: str | None = None) -> None:
            self.calls.append(query_override)
            item.danmaku_search_query = "红果短剧 1集" if query_override is None else query_override
            item.danmaku_search_query_overridden = query_override is not None

    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_search_query="红果短剧 腾讯版",
        danmaku_search_query_overridden=True,
    )
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=FakeDanmakuController(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    window._open_danmaku_source_dialog()
    window._reset_current_item_danmaku_search_query()

    assert window._danmaku_source_query_edit.text() == "红果短剧 1集"
    assert item.danmaku_search_query_overridden is False


def test_player_window_manual_danmaku_source_switch_reconfigures_current_item(qtbot, monkeypatch) -> None:
    class FakeDanmakuController:
        def switch_danmaku_source(self, item: PlayItem, page_url: str) -> str:
            item.selected_danmaku_url = page_url
            item.selected_danmaku_provider = "tencent"
            item.selected_danmaku_title = "红果短剧 第1集"
            item.danmaku_xml = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>'
            return item.danmaku_xml

    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
            )
        ],
        selected_danmaku_provider="tencent",
        selected_danmaku_url="https://v.qq.com/demo",
        selected_danmaku_title="红果短剧 第1集",
        danmaku_search_query="红果短剧 1集",
    )
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=FakeDanmakuController(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_configure_danmaku_for_current_item", lambda: None)

    window.open_session(session)
    window._open_danmaku_source_dialog()
    window._switch_current_item_danmaku_source()

    assert item.selected_danmaku_url == "https://v.qq.com/demo"
    assert "ok" in item.danmaku_xml


def test_player_window_uses_splitters_for_resizable_panels(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert isinstance(window.main_splitter, QSplitter)
    assert window.main_splitter.orientation() == Qt.Orientation.Horizontal
    assert isinstance(window.sidebar_splitter, QSplitter)
    assert window.sidebar_splitter.orientation() == Qt.Orientation.Vertical


def test_player_window_shows_route_selector_for_single_group(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘剧集"),
        playlist=[PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="网盘线(夸克)")],
        playlists=[
            [PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="网盘线(夸克)")]
        ],
        playlist_index=0,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    assert isinstance(window.playlist_group_combo, QComboBox)
    assert window.playlist_group_combo.isHidden() is False
    assert window.playlist_group_combo.count() == 1
    assert window.playlist_group_combo.itemText(0) == "网盘线(夸克)"


def test_player_window_rewrites_remote_m3u8_to_local_proxy_url(qtbot) -> None:
    class FakeM3U8AdFilter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            self.calls.append((url, dict(headers or {})))
            return "http://127.0.0.1:2323/m3u?v=proxy-1"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://media.example/path/index.m3u8",
                headers={"Referer": "https://site.example"},
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    filter_service = FakeM3U8AdFilter()
    video = RecordingVideo()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=filter_service)
    qtbot.addWidget(window)
    window.video = video

    window.open_session(session)
    qtbot.waitUntil(lambda: video.load_calls == [("http://127.0.0.1:2323/m3u?v=proxy-1", 0)])

    assert filter_service.calls == [
        (
            "https://media.example/path/index.m3u8",
            {"Referer": "https://site.example"},
        )
    ]


def test_player_window_logs_proxy_prepare_failure_and_plays_original_url(qtbot) -> None:
    class FailingM3U8AdFilter:
        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            raise RuntimeError("port 2323 busy")

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="正片", url="https://media.example/path/index.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    video = RecordingVideo()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=FailingM3U8AdFilter())
    qtbot.addWidget(window)
    window.video = video

    window.open_session(session)
    qtbot.waitUntil(lambda: video.load_calls == [("https://media.example/path/index.m3u8", 0)])

    assert "port 2323 busy" in window.log_view.toPlainText()


def test_player_window_rewrites_resolved_m3u8_after_detail_lookup(qtbot) -> None:
    class ResolvingPlayerController(FakePlayerController):
        def resolve_play_item_detail(self, session, play_item):
            play_item.url = "https://media.example/path/resolved.m3u8"
            return VodItem(
                vod_id="movie-1",
                vod_name="Resolved Movie",
                items=[PlayItem(title="正片", url=play_item.url)],
            )

    class FakeM3U8AdFilter:
        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            return "http://127.0.0.1:2323/m3u?v=resolved-1"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="正片", url="", vod_id="detail-1")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        detail_resolver=lambda item: VodItem(vod_id=item.vod_id, vod_name="Resolved Movie"),
    )
    video = RecordingVideo()
    window = PlayerWindow(ResolvingPlayerController(), m3u8_ad_filter=FakeM3U8AdFilter())
    qtbot.addWidget(window)
    window.video = video

    window.open_session(session)
    qtbot.waitUntil(lambda: video.load_calls == [("http://127.0.0.1:2323/m3u?v=resolved-1", 0)])


def test_player_window_uses_detail_container_with_metadata_and_log_views(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.details is not None
    assert window.metadata_view.isReadOnly() is True
    assert window.log_view.isReadOnly() is True
    assert window.details.layout().indexOf(window.metadata_view) != -1
    assert window.details.layout().indexOf(window.log_view) != -1


def test_player_window_renders_route_selector_and_switches_active_group(qtbot) -> None:
    controller = FakePlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    video = RecordingVideo()
    window.video = video
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="红果短剧"),
        playlist=[
            PlayItem(title="第1集", url="http://a/1.m3u8", play_source="备用线"),
            PlayItem(title="第2集", url="http://a/2.m3u8", play_source="备用线"),
        ],
        playlists=[
            [
                PlayItem(title="第1集", url="http://a/1.m3u8", play_source="备用线"),
                PlayItem(title="第2集", url="http://a/2.m3u8", play_source="备用线"),
            ],
            [
                PlayItem(title="第1集", url="http://b/1.m3u8", play_source="极速线"),
                PlayItem(title="第2集", url="http://b/2.m3u8", play_source="极速线"),
            ],
        ],
        playlist_index=0,
        start_index=1,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    assert window.playlist_group_combo.isHidden() is False
    assert [window.playlist_group_combo.itemText(i) for i in range(window.playlist_group_combo.count())] == ["备用线", "极速线"]
    assert [window.playlist.item(i).text() for i in range(window.playlist.count())] == ["第1集", "第2集"]
    assert window.playlist.currentRow() == 1

    window.playlist_group_combo.setCurrentIndex(1)

    assert window.session is not None
    assert window.session.playlist_index == 1
    assert [item.title for item in window.session.playlist] == ["第1集", "第2集"]
    assert window.playlist.currentRow() == 1
    assert video.load_calls[-1][0] == "http://b/2.m3u8"


def test_player_window_next_and_previous_stay_within_active_group(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    video = RecordingVideo()
    window.video = video
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="红果短剧"),
        playlist=[
            PlayItem(title="第1集", url="http://b/1.m3u8", play_source="极速线"),
            PlayItem(title="第2集", url="http://b/2.m3u8", play_source="极速线"),
        ],
        playlists=[
            [PlayItem(title="第1集", url="http://a/1.m3u8", play_source="备用线")],
            [
                PlayItem(title="第1集", url="http://b/1.m3u8", play_source="极速线"),
                PlayItem(title="第2集", url="http://b/2.m3u8", play_source="极速线"),
            ],
        ],
        playlist_index=1,
        start_index=1,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)
    window.play_next()
    assert window.current_index == 1

    window.play_previous()
    assert window.current_index == 0
    assert video.load_calls[-1][0] == "http://b/1.m3u8"


def test_player_window_places_poster_widget_above_metadata_and_log_views(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    details_layout = window.details.layout()

    assert window.poster_label is not None
    assert details_layout.indexOf(window.poster_label) != -1
    assert details_layout.indexOf(window.poster_label) < details_layout.indexOf(window.metadata_view)
    assert details_layout.indexOf(window.poster_label) < details_layout.indexOf(window.log_view)
    assert window.poster_label.alignment() == Qt.AlignmentFlag.AlignCenter
    assert window.poster_label.minimumHeight() > 0


def test_player_window_renders_poster_when_session_has_vod_pic(qtbot, tmp_path) -> None:
    poster_path = tmp_path / "poster.png"
    pixmap = QPixmap(20, 30)
    pixmap.fill(QColor("red"))
    assert pixmap.save(str(poster_path)) is True

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="九寨沟", vod_pic=str(poster_path)),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    rendered = window.poster_label.pixmap()
    assert rendered is not None
    assert rendered.isNull() is False
    assert rendered.size().width() <= window.poster_label.maximumWidth()
    assert rendered.size().height() <= window.poster_label.maximumHeight()


def test_player_window_keeps_empty_reserved_poster_area_without_placeholder_text(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="九寨沟", vod_pic=""),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    rendered = window.poster_label.pixmap()
    assert rendered is None or rendered.isNull() is True
    assert window.poster_label.text() == ""
    assert window.poster_label.minimumHeight() > 0


def test_player_window_starts_remote_poster_load_without_blocking_open_session(qtbot, monkeypatch) -> None:
    started: list[str] = []

    def fake_start(self, source: str, request_id: int) -> None:
        started.append(source)

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="九寨沟", vod_pic="https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123.jpg"),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    rendered = window.poster_label.pixmap()
    assert started == ["https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123.jpg"]
    assert rendered is None or rendered.isNull() is True
    assert window.video.load_calls == [("http://m/1.m3u8", False, 0)]


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_player_window_ignores_async_poster_result_after_window_deletion(qtbot, monkeypatch) -> None:
    release_poster = threading.Event()
    destroyed = {"count": 0}

    def fake_load_remote_poster_image(*args, **kwargs):
        assert release_poster.wait(timeout=5), "poster load was never released"
        image = QImage(20, 30, QImage.Format.Format_RGB32)
        image.fill(QColor("green"))
        return image

    monkeypatch.setattr(player_window_module, "load_remote_poster_image", fake_load_remote_poster_image)

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    window.destroyed.connect(lambda *_args: destroyed.__setitem__("count", destroyed["count"] + 1))
    window.video = FakeVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123.jpg"),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )

    window.deleteLater()
    qtbot.waitUntil(lambda: destroyed["count"] == 1, timeout=1000)

    release_poster.set()
    qtbot.wait(100)

    assert destroyed["count"] == 1


def test_player_window_ignores_stale_async_poster_results(qtbot, monkeypatch) -> None:
    started_request_ids: list[int] = []

    def fake_start(self, source: str, request_id: int) -> None:
        started_request_ids.append(request_id)

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    first_image = QImage(20, 30, QImage.Format.Format_RGB32)
    first_image.fill(QColor("red"))
    second_image = QImage(20, 30, QImage.Format.Format_RGB32)
    second_image.fill(QColor("blue"))

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-1", vod_name="First", vod_pic="https://img3.doubanio.com/view/photo/s_ratio_poster/public/first.jpg"),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )
    first_request_id = started_request_ids[-1]

    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-2", vod_name="Second", vod_pic="https://img3.doubanio.com/view/photo/s_ratio_poster/public/second.jpg"),
            playlist=[PlayItem(title="正片", url="http://m/2.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )
    second_request_id = started_request_ids[-1]

    window._handle_poster_load_finished(first_request_id, first_image)
    stale_rendered = window.poster_label.pixmap()
    assert stale_rendered is None or stale_rendered.isNull() is True

    window._handle_poster_load_finished(second_request_id, second_image)

    rendered = window.poster_label.pixmap()
    assert rendered is not None
    assert rendered.isNull() is False
    assert rendered.toImage().pixelColor(0, 0) == QColor("blue")


def test_player_window_shows_loaded_poster_over_video_until_playback_progress_appears(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self._duration = 0
            self._position = 0

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def duration_seconds(self) -> int:
            return self._duration

        def position_seconds(self) -> int:
            return self._position

    image = QImage(20, 30, QImage.Format.Format_RGB32)
    image.fill(QColor("green"))

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123.jpg"),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )

    window._handle_poster_load_finished(window._poster_request_id, image)

    assert window.video_poster_overlay.isHidden() is False
    assert window.video_poster_overlay.pixmap() is not None
    assert window.video_poster_overlay.pixmap().isNull() is False

    window.video._duration = 120
    window._sync_progress_slider()

    assert window.video_poster_overlay.isHidden() is True


def test_player_window_keeps_video_poster_overlay_hidden_when_no_poster_is_loaded(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def duration_seconds(self) -> int:
            return 0

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-1", vod_name="Movie", vod_pic=""),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )

    assert window.video_poster_overlay.isHidden() is True


def test_player_window_renders_remote_poster_via_direct_request_headers(qtbot, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: tmp_path / "poster-cache")
    poster_path = tmp_path / "poster.png"
    pixmap = QPixmap(20, 30)
    pixmap.fill(QColor("blue"))
    assert pixmap.save(str(poster_path)) is True
    poster_bytes = poster_path.read_bytes()
    requests: list[tuple[str, dict[str, str], float]] = []

    class FakeResponse:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    def fake_get(
        url: str,
        headers: dict[str, str],
        timeout: float,
        follow_redirects: bool = False,
    ) -> FakeResponse:
        requests.append((url, headers, timeout))
        return FakeResponse(poster_bytes)

    monkeypatch.setattr(
        player_window_module,
        "httpx",
        type("FakeHttpx", (), {"get": staticmethod(fake_get)}),
        raising=False,
    )

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="九寨沟",
            vod_pic="https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123.jpg",
        ),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: len(requests) == 1)
    qtbot.waitUntil(lambda: (window.poster_label.pixmap() is not None and not window.poster_label.pixmap().isNull()))

    rendered = window.poster_label.pixmap()
    assert rendered is not None
    assert rendered.isNull() is False
    assert requests == [
        (
            "https://img3.doubanio.com/view/photo/m/public/p123.jpg",
            {
                "Referer": "https://movie.douban.com/",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            },
            10.0,
        )
    ]


def test_player_window_uses_short_timeout_for_remote_poster_requests(qtbot, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: tmp_path / "poster-cache")
    requested_timeouts: list[float] = []

    class FakeResponse:
        content = b""

        def raise_for_status(self) -> None:
            return None

    def fake_get(
        url: str,
        headers: dict[str, str],
        timeout: float,
        follow_redirects: bool = False,
    ) -> FakeResponse:
        requested_timeouts.append(timeout)
        return FakeResponse()

    monkeypatch.setattr(
        player_window_module,
        "httpx",
        type("FakeHttpx", (), {"get": staticmethod(fake_get)}),
        raising=False,
    )

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="九寨沟",
            vod_pic="https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123.jpg",
        ),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: requested_timeouts == [10.0])

    assert requested_timeouts == [10.0]


def test_player_window_uses_youtube_referer_for_ytimg_posters(qtbot, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: tmp_path / "poster-cache")
    requested_headers: list[dict[str, str]] = []

    class FakeResponse:
        content = b""

        def raise_for_status(self) -> None:
            return None

    def fake_get(
        url: str,
        headers: dict[str, str],
        timeout: float,
        follow_redirects: bool = False,
    ) -> FakeResponse:
        requested_headers.append(headers)
        return FakeResponse()

    monkeypatch.setattr(
        player_window_module,
        "httpx",
        type("FakeHttpx", (), {"get": staticmethod(fake_get)}),
        raising=False,
    )

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Trailer", vod_pic="https://i.ytimg.com/vi/demo/maxresdefault.jpg"),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: len(requested_headers) == 1)

    assert requested_headers == [
        {
            "Referer": "https://www.youtube.com/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }
    ]


def test_player_window_uses_netease_referer_for_netease_posters(qtbot, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: tmp_path / "poster-cache")
    requested_headers: list[dict[str, str]] = []

    class FakeResponse:
        content = b""

        def raise_for_status(self) -> None:
            return None

    def fake_get(
        url: str,
        headers: dict[str, str],
        timeout: float,
        follow_redirects: bool = False,
    ) -> FakeResponse:
        requested_headers.append(headers)
        return FakeResponse()

    monkeypatch.setattr(
        player_window_module,
        "httpx",
        type("FakeHttpx", (), {"get": staticmethod(fake_get)}),
        raising=False,
    )

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Live", vod_pic="https://p1.cc.163.com/demo/poster.jpg"),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: len(requested_headers) == 1)

    assert requested_headers == [
        {
            "Referer": "https://cc.163.com/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        }
    ]


def test_player_window_uses_vertical_shell_with_bottom_controls(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    root_layout = window.layout()

    assert root_layout is not None
    assert root_layout.count() == 2
    assert root_layout.itemAt(0).widget() is window.main_splitter
    assert root_layout.itemAt(1).widget() is window.bottom_area
    assert window.main_splitter.orientation() == Qt.Orientation.Horizontal
    assert window.sidebar_splitter.orientation() == Qt.Orientation.Vertical


def test_player_window_bottom_controls_are_not_nested_inside_video_pane(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    main_container = window.main_splitter.widget(0)

    assert main_container is not None
    assert main_container.layout().indexOf(window.bottom_area) == -1


def test_player_window_falls_back_when_saved_splitter_state_is_invalid(qtbot) -> None:
    config = AppConfig(player_main_splitter_state=b"not-a-real-splitter-state")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.show()

    sizes = window.main_splitter.sizes()

    assert len(sizes) == 2
    assert all(size > 0 for size in sizes)


def test_player_window_falls_back_when_saved_splitter_state_collapses_sidebar(qtbot) -> None:
    config = AppConfig()
    source = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(source)
    source.show()
    source.main_splitter.setSizes([1, 0])
    config.player_main_splitter_state = bytes(source.main_splitter.saveState())

    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.show()

    sizes = window.main_splitter.sizes()

    assert len(sizes) == 2
    assert all(size > 0 for size in sizes)


def test_player_window_retries_resume_seek_when_player_is_not_ready(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.seek_calls = 0
            self.can_seek_calls = 0

        def can_seek(self) -> bool:
            self.can_seek_calls += 1
            return self.can_seek_calls > 1

        def seek(self, seconds: int) -> None:
            self.seek_calls += 1

    scheduled_delays: list[int] = []

    def immediate_single_shot(delay: int, callback) -> None:
        scheduled_delays.append(delay)
        callback()

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    monkeypatch.setattr(player_window_module.QTimer, "singleShot", immediate_single_shot)

    window._attempt_resume_seek(42, retries_remaining=2)

    assert window.video.seek_calls == 1
    assert scheduled_delays == [300]


def test_player_window_reports_failure_after_seek_retries_are_exhausted(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def can_seek(self) -> bool:
            return False

        def seek(self, seconds: int) -> None:
            raise AssertionError("seek should not be called when player is not seekable")

    scheduled_delays: list[int] = []

    def immediate_single_shot(delay: int, callback) -> None:
        scheduled_delays.append(delay)
        callback()

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    monkeypatch.setattr(player_window_module.QTimer, "singleShot", immediate_single_shot)

    window._attempt_resume_seek(42, retries_remaining=1)

    assert scheduled_delays == [300]
    assert "恢复播放失败" in window.log_view.toPlainText()


def test_player_window_passes_resume_offset_into_video_load(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=42,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.video.load_calls == [("http://m/1.m3u8", False, 42)]


def test_player_window_starts_from_opening_skip_when_resume_position_is_earlier(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=3,
        speed=1.0,
        opening_seconds=12,
        ending_seconds=0,
    )

    window.open_session(session)

    assert window.video.load_calls == [("http://m/1.m3u8", 12)]


def test_player_window_can_open_session_paused(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=1), start_paused=True)

    assert window.is_playing is False
    assert window.video.load_calls == [("http://m/2.m3u8", True, 0)]
    assert window.play_button.icon().pixmap(24, 24).toImage() == player_window_module.QIcon(
        str(window._icons_dir / "play.svg")
    ).pixmap(24, 24).toImage()


def test_player_window_shows_video_title_while_playing(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 2", url="http://m/2.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.windowTitle() == "Movie - Episode 2"


def test_player_window_renders_title_metadata_in_expected_order(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="九寨沟",
            type_name="纪录片",
            vod_year="2006",
            vod_area="中国大陆",
            vod_lang="无对白",
            vod_remarks="6.2",
            vod_director="Masa Nishimura",
            vod_actor="未知",
            vod_content="九寨沟风景名胜区位于四川省阿坝藏族羌族自治州南坪县境内。",
            dbid=19971621,
        ),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.metadata_view.toPlainText() == (
        "名称: 九寨沟\n"
        "类型: 纪录片\n"
        "年代: 2006\n"
        "地区: 中国大陆\n"
        "语言: 无对白\n"
        "评分: 6.2\n"
        "导演: Masa Nishimura\n"
        "演员: 未知\n"
        "豆瓣ID: 19971621\n"
        "\n"
        "简介:\n"
        "九寨沟风景名胜区位于四川省阿坝藏族羌族自治州南坪县境内。"
    )


def test_player_window_renders_live_metadata_with_five_live_fields(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    session = PlayerSession(
        vod=VodItem(
            vod_id="bili$1785607569",
            vod_name="主播直播间",
            type_name="游戏",
            vod_remarks="10万",
            vod_director="哔哩哔哩",
            vod_actor="测试主播",
            detail_style="live",
        ),
        playlist=[PlayItem(title="线路 1", url="https://stream.example/live.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.metadata_view.toPlainText() == (
        "标题: 主播直播间\n"
        "平台: 哔哩哔哩\n"
        "类型: 游戏\n"
        "主播: 测试主播\n"
        "人气: 10万"
    )


def test_player_window_renders_epg_rows_for_live_metadata(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    session = PlayerSession(
        vod=VodItem(
            vod_id="custom-live-1",
            vod_name="CCTV-1",
            detail_style="live",
            epg_current="09:00-10:00 朝闻天下",
            epg_schedule="10:00-11:00 新闻30分\n11:00-12:00 今日说法",
        ),
        playlist=[PlayItem(title="线路 1", url="https://live.example/cctv1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.metadata_view.toPlainText() == (
        "当前节目:\n"
        "09:00-10:00 朝闻天下\n"
        "\n"
        "今日节目单:\n"
        "10:00-11:00 新闻30分\n"
        "11:00-12:00 今日说法"
    )


def test_player_window_appends_runtime_failures_to_log_view_without_overwriting_metadata(qtbot) -> None:
    class FailingVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            raise RuntimeError("boom")

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="九寨沟", type_name="纪录片", vod_content="简介文本"),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FailingVideo()

    window.open_session(session)

    assert "名称: 九寨沟" in window.metadata_view.toPlainText()
    assert "播放失败: boom" in window.log_view.toPlainText()
    assert "播放失败: boom" not in window.metadata_view.toPlainText()


def test_player_window_appends_mpv_failure_messages_to_log_view_without_overwriting_metadata(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie", type_name="剧情", vod_content="简介"),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.video_widget.playback_failed.emit("播放失败: HTTP 403 Forbidden")

    assert "名称: Movie" in window.metadata_view.toPlainText()
    assert "播放失败: HTTP 403 Forbidden" in window.log_view.toPlainText()
    assert "播放失败: HTTP 403 Forbidden" not in window.metadata_view.toPlainText()


def test_player_window_opening_new_session_refreshes_metadata_and_clears_old_logs(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    first_session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="九寨沟", type_name="纪录片", vod_content="第一条简介"),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    second_session = PlayerSession(
        vod=VodItem(vod_id="movie-2", vod_name="黄龙", type_name="纪录片", vod_content="第二条简介"),
        playlist=[PlayItem(title="正片", url="http://m/2.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(first_session)
    window._append_log("播放失败: boom")
    window.open_session(second_session)

    assert "名称: 黄龙" in window.metadata_view.toPlainText()
    assert "第一条简介" not in window.metadata_view.toPlainText()
    assert "播放失败: boom" not in window.log_view.toPlainText()


def test_player_window_can_hide_playlist_and_details(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    window.toggle_playlist_button.click()
    window.toggle_details_button.click()

    assert window.playlist.isHidden() is True
    assert window.details.isHidden() is True

    window.toggle_playlist_button.click()
    window.toggle_details_button.click()

    assert window.playlist.isHidden() is False
    assert window.details.isHidden() is False


def test_player_window_toggle_fullscreen_changes_window_state(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    window.toggle_details_button.click()

    window.toggle_fullscreen()
    assert window.isFullScreen() is True
    assert window.bottom_area.isHidden() is True
    assert window.sidebar_actions_widget.isHidden() is True
    assert window.playlist.isHidden() is True
    assert window.details.isHidden() is True

    window.toggle_fullscreen()
    assert window.isFullScreen() is False
    assert window.bottom_area.isHidden() is False
    assert window.sidebar_actions_widget.isHidden() is False
    assert window.playlist.isHidden() is False
    assert window.details.isHidden() is True


def test_player_window_escape_shortcut_exits_fullscreen_instead_of_returning_to_main(qtbot) -> None:
    emitted = {"count": 0}
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))
    window.show()

    window.toggle_fullscreen()
    assert window.isFullScreen() is True

    window.escape_shortcut.activated.emit()

    assert window.isFullScreen() is False
    assert window.isHidden() is False
    assert emitted["count"] == 0


def test_player_window_syncs_progress_slider_and_seeks_from_it(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.seek_calls: list[int] = []

        def duration_seconds(self) -> int:
            return 120

        def position_seconds(self) -> int:
            return 30

        def seek(self, seconds: int) -> None:
            self.seek_calls.append(seconds)

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window._sync_progress_slider()

    assert window.progress.maximum() == 120
    assert window.progress.value() == 30
    assert window.current_time_label.text() == "00:30"
    assert window.duration_label.text() == "02:00"

    window.progress.setValue(75)
    window._seek_from_slider()

    assert window.video.seek_calls == [75]


def test_player_window_clicking_progress_track_seeks_immediately(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.seek_calls: list[int] = []

        def seek(self, seconds: int) -> None:
            self.seek_calls.append(seconds)

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.progress.clicked_value.emit(48)

    assert window.video.seek_calls == [48]


def test_player_window_progress_slider_hover_formats_time(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def duration_seconds(self) -> int:
            return 120

        def position_seconds(self) -> int:
            return 30

    shown: list[str] = []
    monkeypatch.setattr(
        QToolTip,
        "showText",
        staticmethod(lambda _pos, text, *_args, **_kwargs: shown.append(text)),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()
    window.video = FakeVideo()
    window._sync_progress_slider()

    local_pos = window.progress.rect().center()
    global_pos = window.progress.mapToGlobal(local_pos)
    QApplication.sendEvent(
        window.progress,
        QMouseEvent(
            QEvent.Type.MouseMove,
            local_pos,
            global_pos,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )

    assert shown == [window._format_time(window.progress._pixel_pos_to_value(local_pos.x()))]


def test_player_window_volume_slider_hover_formats_percent(qtbot, monkeypatch) -> None:
    shown: list[str] = []
    monkeypatch.setattr(
        QToolTip,
        "showText",
        staticmethod(lambda _pos, text, *_args, **_kwargs: shown.append(text)),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()

    local_pos = window.volume_slider.rect().center()
    global_pos = window.volume_slider.mapToGlobal(local_pos)
    QApplication.sendEvent(
        window.volume_slider,
        QMouseEvent(
            QEvent.Type.MouseMove,
            local_pos,
            global_pos,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )

    assert shown == [f"{window.volume_slider._pixel_pos_to_value(local_pos.x())}%"]


def test_player_window_exposes_extended_playback_controls(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.play_button.text() == ""
    assert window.prev_button.text() == ""
    assert window.next_button.text() == ""
    assert window.backward_button.text() == ""
    assert window.forward_button.text() == ""
    assert window.refresh_button.text() == ""
    assert window.mute_button.text() == ""
    assert window.wide_button.text() == ""
    assert window.fullscreen_button.text() == ""
    assert window.toggle_playlist_button.text() == ""
    assert window.toggle_details_button.text() == ""
    assert window.play_button.toolTip() == "播放/暂停 (Space)"
    assert window.mute_button.toolTip() == "静音 (M)"
    assert window.fullscreen_button.toolTip() == "全屏 (Enter)"
    assert isinstance(window.speed_combo, QComboBox)
    assert window.volume_slider.maximum() == 100


def test_player_window_exposes_subtitle_combo_with_default_auto_entry(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert isinstance(window.subtitle_combo, QComboBox)
    assert window.subtitle_combo.count() == 1
    assert window.subtitle_combo.itemText(0) == "自动选择"
    assert window.subtitle_combo.isEnabled() is False


def test_player_window_exposes_danmaku_combo_after_subtitle_combo(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    control_layout = window.subtitle_combo.parentWidget().layout()

    assert isinstance(window.danmaku_combo, QComboBox)
    assert [window.danmaku_combo.itemText(index) for index in range(window.danmaku_combo.count())] == [
        "弹幕",
        "关闭",
        "1行",
        "2行",
        "3行",
        "4行",
        "5行",
    ]
    assert window.danmaku_combo.isEnabled() is False
    assert control_layout.indexOf(window.danmaku_combo) == control_layout.indexOf(window.subtitle_combo) + 1


def test_player_window_exposes_audio_combo_with_default_auto_entry(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert isinstance(window.audio_combo, QComboBox)
    assert window.audio_combo.count() == 1
    assert window.audio_combo.itemText(0) == "自动选择"
    assert window.audio_combo.isEnabled() is False


def test_player_window_exposes_parse_combo_with_builtin_entries(qtbot) -> None:
    class FakeParserService:
        def parsers(self):
            return [
                type("Parser", (), {"key": "fish", "label": "fish"})(),
                type("Parser", (), {"key": "jx1", "label": "jx1"})(),
                type("Parser", (), {"key": "jx2", "label": "jx2"})(),
                type("Parser", (), {"key": "mg1", "label": "mg1"})(),
                type("Parser", (), {"key": "tx1", "label": "tx1"})(),
            ]

    window = PlayerWindow(FakePlayerController(), config=AppConfig(), playback_parser_service=FakeParserService())
    qtbot.addWidget(window)

    assert window.parse_combo.count() == 6
    assert window.parse_combo.itemText(0) == "解析"
    assert [window.parse_combo.itemText(index) for index in range(1, window.parse_combo.count())] == [
        "fish",
        "jx1",
        "jx2",
        "mg1",
        "tx1",
    ]


def test_player_window_saves_preferred_parse_key_when_user_selects_parser(qtbot) -> None:
    saved = {"called": 0}
    config = AppConfig()

    class FakeParserService:
        def parsers(self):
            return [
                type("Parser", (), {"key": "fish", "label": "fish"})(),
                type("Parser", (), {"key": "jx1", "label": "jx1"})(),
            ]

    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("called", saved["called"] + 1),
        playback_parser_service=FakeParserService(),
    )
    qtbot.addWidget(window)

    window.parse_combo.setCurrentIndex(2)

    assert config.preferred_parse_key == "jx1"
    assert saved["called"] == 1


def test_player_window_populates_embedded_audio_options_after_open_session(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []
            self.audio_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return [
                AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
            ]

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((mode, track_id))
            return 31 if mode == "auto" else track_id

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    assert [window.audio_combo.itemText(index) for index in range(window.audio_combo.count())] == [
        "音轨",
        "国语 (默认)",
        "English Dub",
    ]
    assert window.audio_combo.isEnabled() is True
    assert window.video.audio_apply_calls[0] == ("auto", None)


def test_player_window_disables_audio_selector_when_current_item_has_no_embedded_audio_options(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    assert window.audio_combo.count() == 1
    assert window.audio_combo.itemText(0) == "音轨"
    assert window.audio_combo.isEnabled() is False


def test_player_window_user_selection_applies_selected_audio_track(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.audio_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return [
                AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
            ]

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((mode, track_id))
            return track_id

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.video.audio_apply_calls.clear()

    window.audio_combo.setCurrentIndex(2)

    assert window.video.audio_apply_calls == [("track", 32)]


def test_player_window_reuses_audio_track_preference_for_next_episode(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.current_url = ""
            self.audio_apply_calls: list[tuple[str, str, int | None]] = []
            self.tracks_by_url = {
                "http://m/1.m3u8": [
                    AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                    AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
                ],
                "http://m/2.m3u8": [
                    AudioTrack(id=41, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                    AudioTrack(id=42, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
                ],
            }

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.current_url = url

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return self.tracks_by_url[self.current_url]

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((self.current_url, mode, track_id))
            return track_id if mode == "track" else 41

        def position_seconds(self) -> int:
            return 30

        def duration_seconds(self) -> int:
            return 120

    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.audio_combo.setCurrentIndex(2)
    window.video.audio_apply_calls.clear()

    window.play_next()

    assert ("http://m/2.m3u8", "track", 42) in window.video.audio_apply_calls
    assert window.audio_combo.currentText() == "English Dub"


def test_player_window_falls_back_to_auto_when_previous_audio_track_cannot_be_matched(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.current_url = ""
            self.audio_apply_calls: list[tuple[str, str, int | None]] = []
            self.tracks_by_url = {
                "http://m/1.m3u8": [
                    AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                    AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
                ],
                "http://m/2.m3u8": [
                    AudioTrack(id=41, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                ],
            }

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.current_url = url

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return self.tracks_by_url[self.current_url]

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((self.current_url, mode, track_id))
            return 41 if mode == "auto" else track_id

        def position_seconds(self) -> int:
            return 30

        def duration_seconds(self) -> int:
            return 120

    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.audio_combo.setCurrentIndex(2)
    window.video.audio_apply_calls.clear()

    window.play_next()

    assert ("http://m/2.m3u8", "auto", None) in window.video.audio_apply_calls
    assert window.audio_combo.currentText() == "音轨"
    assert window.audio_combo.isEnabled() is False


def test_player_window_logs_and_resets_when_audio_refresh_fails(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            raise RuntimeError("boom")

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    assert "音轨加载失败: boom" in window.log_view.toPlainText()
    assert window.audio_combo.count() == 1
    assert window.audio_combo.itemText(0) == "音轨"
    assert window.audio_combo.isEnabled() is False


def test_player_window_refreshes_audio_options_when_mpv_reports_tracks_after_load(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    load_calls: list[tuple[str, bool, int]] = []
    audio_apply_calls: list[tuple[str, int | None]] = []
    tracks_call_count = {"count": 0}

    def fake_audio_tracks() -> list[AudioTrack]:
        tracks_call_count["count"] += 1
        if tracks_call_count["count"] == 1:
            return []
        return [
            AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
            AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
        ]

    window.video_widget.load = lambda url, pause=False, start_seconds=0: load_calls.append((url, pause, start_seconds))
    window.video_widget.set_speed = lambda speed: None
    window.video_widget.set_volume = lambda value: None
    window.video_widget.subtitle_tracks = lambda: []
    window.video_widget.apply_subtitle_mode = lambda mode, track_id=None: None
    window.video_widget.audio_tracks = fake_audio_tracks
    window.video_widget.apply_audio_mode = (
        lambda mode, track_id=None: audio_apply_calls.append((mode, track_id)) or (31 if mode == "auto" else track_id)
    )
    window.video_widget.position_seconds = lambda: 0

    window.open_session(make_player_session(start_index=0))

    assert load_calls == [("http://m/1.m3u8", False, 0)]
    assert window.audio_combo.count() == 1
    assert window.audio_combo.isEnabled() is False

    window.video_widget.audio_tracks_changed.emit()

    assert [window.audio_combo.itemText(index) for index in range(window.audio_combo.count())] == [
        "音轨",
        "国语 (默认)",
        "English Dub",
    ]
    assert window.audio_combo.isEnabled() is True
    assert audio_apply_calls == [("auto", None)]


def test_player_window_builds_video_context_menu_with_track_submenus(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [
                SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
            ]

        def audio_tracks(self) -> list[AudioTrack]:
            return [
                AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
            ]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 11 if mode == "auto" else track_id

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None if mode == "off" else track_id

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 31 if mode == "auto" else track_id

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def supports_subtitle_scale(self) -> bool:
            return True

        def subtitle_scale(self) -> int:
            return 100

        def set_subtitle_scale(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_scale(self) -> bool:
            return True

        def secondary_subtitle_scale(self) -> int:
            return 100

        def set_secondary_subtitle_scale(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()

    assert [action.text() for action in menu.actions()] == [
        "主字幕",
        "次字幕",
        "主字幕位置",
        "次字幕位置",
        "主字幕大小",
        "次字幕大小",
        "音轨",
        "弹幕源",
        "视频信息",
    ]
    assert [action.text() for action in _submenu_actions(menu, "主字幕")] == ["自动选择", "关闭字幕", "中文 (默认)", "English"]
    assert [action.text() for action in _submenu_actions(menu, "次字幕")] == ["关闭次字幕", "中文 (默认)", "English"]
    assert [action.text() for action in _submenu_actions(menu, "主字幕大小")] == [
        "很小",
        "小",
        "默认",
        "大",
        "很大",
        "",
        "缩小 5%",
        "放大 5%",
        "重置",
    ]
    assert [action.text() for action in _submenu_actions(menu, "次字幕大小")] == [
        "很小",
        "小",
        "默认",
        "大",
        "很大",
        "",
        "缩小 5%",
        "放大 5%",
        "重置",
    ]
    assert [action.text() for action in _submenu_actions(menu, "音轨")] == ["自动选择", "国语 (默认)", "English Dub"]


def test_player_window_context_menu_video_info_action_calls_video_layer(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.video_info_toggles = 0

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def supports_subtitle_scale(self) -> bool:
            return True

        def subtitle_scale(self) -> int:
            return 100

        def set_subtitle_scale(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_scale(self) -> bool:
            return True

        def secondary_subtitle_scale(self) -> int:
            return 100

        def set_secondary_subtitle_scale(self, value: int) -> None:
            return None

        def toggle_video_info(self) -> None:
            self.video_info_toggles += 1

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    video_info_action = next(action for action in menu.actions() if action.text() == "视频信息")
    video_info_action.trigger()

    assert window.video.video_info_toggles == 1


def test_player_window_context_menu_video_info_action_logs_failures(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def supports_subtitle_scale(self) -> bool:
            return True

        def subtitle_scale(self) -> int:
            return 100

        def set_subtitle_scale(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_scale(self) -> bool:
            return True

        def secondary_subtitle_scale(self) -> int:
            return 100

        def set_secondary_subtitle_scale(self, value: int) -> None:
            return None

        def toggle_video_info(self) -> None:
            raise RuntimeError("info boom")

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in menu.actions() if action.text() == "视频信息").trigger()

    assert "视频信息显示失败: info boom" in window.log_view.toPlainText()


def test_player_window_context_menu_primary_subtitle_action_syncs_bottom_combo(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.audio_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [
                SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
            ]

        def audio_tracks(self) -> list[AudioTrack]:
            return [AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)")]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((mode, track_id))
            return 31

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.video.subtitle_apply_calls.clear()

    menu = window._build_video_context_menu()
    english_action = next(action for action in _submenu_actions(menu, "主字幕") if action.text() == "English")
    english_action.trigger()

    assert window.video.subtitle_apply_calls == [("track", 12)]
    assert window.subtitle_combo.currentText() == "English"


def test_player_window_context_menu_secondary_subtitle_and_audio_actions_call_video_layer(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.secondary_subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.audio_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)")]

        def audio_tracks(self) -> list[AudioTrack]:
            return [
                AudioTrack(id=31, title="", lang="cmn", is_default=True, is_forced=False, label="国语 (默认)"),
                AudioTrack(id=32, title="English Dub", lang="eng", is_default=False, is_forced=False, label="English Dub"),
            ]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 11

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.secondary_subtitle_apply_calls.append((mode, track_id))
            return track_id

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((mode, track_id))
            return track_id if mode == "track" else 31

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.video.secondary_subtitle_apply_calls.clear()
    window.video.audio_apply_calls.clear()

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "次字幕") if action.text() == "中文 (默认)").trigger()
    next(action for action in _submenu_actions(menu, "音轨") if action.text() == "English Dub").trigger()

    assert window.video.secondary_subtitle_apply_calls == [("track", 11)]
    assert window.video.audio_apply_calls == [("track", 32)]
    assert window.audio_combo.currentText() == "English Dub"


def test_player_window_context_menu_position_actions_update_video_layer(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.subtitle_position_value = 50
            self.secondary_subtitle_position_value = 50

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return self.subtitle_position_value

        def set_subtitle_position(self, value: int) -> None:
            self.subtitle_position_value = value

        def secondary_subtitle_position(self) -> int:
            return self.secondary_subtitle_position_value

        def set_secondary_subtitle_position(self, value: int) -> None:
            self.secondary_subtitle_position_value = value

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "主字幕位置") if action.text() == "偏下").trigger()
    next(action for action in _submenu_actions(menu, "次字幕位置") if action.text() == "上移 5%").trigger()

    assert window.video.subtitle_position_value == 70
    assert window.video.secondary_subtitle_position_value == 45


def test_player_window_context_menu_includes_primary_and_secondary_subtitle_size_submenus(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def supports_subtitle_scale(self) -> bool:
            return True

        def subtitle_scale(self) -> int:
            return 100

        def set_subtitle_scale(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_scale(self) -> bool:
            return True

        def secondary_subtitle_scale(self) -> int:
            return 100

        def set_secondary_subtitle_scale(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()

    assert [action.text() for action in menu.actions()] == [
        "主字幕",
        "次字幕",
        "主字幕位置",
        "次字幕位置",
        "主字幕大小",
        "次字幕大小",
        "音轨",
        "弹幕源",
        "视频信息",
    ]
    assert [action.text() for action in _submenu_actions(menu, "主字幕大小")] == [
        "很小",
        "小",
        "默认",
        "大",
        "很大",
        "",
        "缩小 5%",
        "放大 5%",
        "重置",
    ]


def test_player_window_context_menu_size_actions_update_video_layer(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.subtitle_scale_value = 100
            self.secondary_subtitle_scale_value = 100

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def supports_subtitle_scale(self) -> bool:
            return True

        def subtitle_scale(self) -> int:
            return self.subtitle_scale_value

        def set_subtitle_scale(self, value: int) -> None:
            self.subtitle_scale_value = value

        def supports_secondary_subtitle_scale(self) -> bool:
            return True

        def secondary_subtitle_scale(self) -> int:
            return self.secondary_subtitle_scale_value

        def set_secondary_subtitle_scale(self, value: int) -> None:
            self.secondary_subtitle_scale_value = value

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "主字幕大小") if action.text() == "大").trigger()
    next(action for action in _submenu_actions(menu, "次字幕大小") if action.text() == "放大 5%").trigger()

    assert window.video.subtitle_scale_value == 115
    assert window.video.secondary_subtitle_scale_value == 105


def test_player_window_reuses_primary_and_secondary_subtitle_scale_for_next_episode(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.current_url = ""
            self.subtitle_scale_value = 100
            self.secondary_subtitle_scale_value = 100
            self.tracks_by_url = {
                "http://m/1.m3u8": [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="简体中文 (默认)")],
                "http://m/2.m3u8": [SubtitleTrack(id=21, title="", lang="zh", is_default=True, is_forced=False, label="简体中文 (默认)")],
            }

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.current_url = url
            self.subtitle_scale_value = 100
            self.secondary_subtitle_scale_value = 100

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return self.tracks_by_url[self.current_url]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 21 if mode == "auto" else track_id

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def supports_subtitle_scale(self) -> bool:
            return True

        def subtitle_scale(self) -> int:
            return self.subtitle_scale_value

        def set_subtitle_scale(self, value: int) -> None:
            self.subtitle_scale_value = value

        def supports_secondary_subtitle_scale(self) -> bool:
            return True

        def secondary_subtitle_scale(self) -> int:
            return self.secondary_subtitle_scale_value

        def set_secondary_subtitle_scale(self, value: int) -> None:
            self.secondary_subtitle_scale_value = value

        def position_seconds(self) -> int:
            return 30

        def duration_seconds(self) -> int:
            return 120

    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "主字幕大小") if action.text() == "大").trigger()
    next(action for action in _submenu_actions(menu, "次字幕大小") if action.text() == "很大").trigger()

    window.play_next()

    assert window.video.subtitle_scale_value == 115
    assert window.video.secondary_subtitle_scale_value == 130


def test_player_window_disables_unsupported_subtitle_size_menus(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def supports_subtitle_scale(self) -> bool:
            return False

        def supports_secondary_subtitle_scale(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    primary_menu = next(action.menu() for action in menu.actions() if action.text() == "主字幕大小")
    secondary_menu = next(action.menu() for action in menu.actions() if action.text() == "次字幕大小")

    assert primary_menu is not None
    assert secondary_menu is not None
    assert primary_menu.isEnabled() is False
    assert secondary_menu.isEnabled() is False


def test_player_window_logs_when_supported_subtitle_scale_write_fails(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def supports_subtitle_scale(self) -> bool:
            return True

        def subtitle_scale(self) -> int:
            return 100

        def set_subtitle_scale(self, value: int) -> None:
            raise RuntimeError("scale boom")

        def supports_secondary_subtitle_scale(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "主字幕大小") if action.text() == "大").trigger()

    assert "主字幕大小设置失败: scale boom" in window.log_view.toPlainText()


def test_player_window_reuses_secondary_subtitle_and_position_preferences_for_next_episode(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.current_url = ""
            self.secondary_subtitle_apply_calls: list[tuple[str, str, int | None]] = []
            self.subtitle_position_value = 50
            self.secondary_subtitle_position_value = 50
            self.tracks_by_url = {
                "http://m/1.m3u8": [
                    SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                    SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
                ],
                "http://m/2.m3u8": [
                    SubtitleTrack(id=21, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                    SubtitleTrack(id=22, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
                ],
            }

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.current_url = url

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return self.tracks_by_url[self.current_url]

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 21 if mode == "auto" else track_id

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.secondary_subtitle_apply_calls.append((self.current_url, mode, track_id))
            return track_id if mode == "track" else None

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return self.subtitle_position_value

        def set_subtitle_position(self, value: int) -> None:
            self.subtitle_position_value = value

        def secondary_subtitle_position(self) -> int:
            return self.secondary_subtitle_position_value

        def set_secondary_subtitle_position(self, value: int) -> None:
            self.secondary_subtitle_position_value = value

        def position_seconds(self) -> int:
            return 30

        def duration_seconds(self) -> int:
            return 120

    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "次字幕") if action.text() == "English").trigger()
    next(action for action in _submenu_actions(menu, "主字幕位置") if action.text() == "偏下").trigger()
    next(action for action in _submenu_actions(menu, "次字幕位置") if action.text() == "偏上").trigger()
    window.video.secondary_subtitle_apply_calls.clear()

    window.play_next()

    assert ("http://m/2.m3u8", "track", 22) in window.video.secondary_subtitle_apply_calls
    assert window.video.subtitle_position_value == 70
    assert window.video.secondary_subtitle_position_value == 30


def test_player_window_logs_and_recovers_when_secondary_subtitle_or_position_apply_fails(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)")]

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 11

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            raise RuntimeError("secondary boom")

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            raise RuntimeError("position boom")

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "次字幕") if action.text() == "中文 (默认)").trigger()
    next(action for action in _submenu_actions(menu, "主字幕位置") if action.text() == "偏下").trigger()

    assert "次字幕切换失败: secondary boom" in window.log_view.toPlainText()
    assert "主字幕位置设置失败: position boom" in window.log_view.toPlainText()


def test_player_window_disables_secondary_subtitle_position_menu_when_video_layer_lacks_support(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="简体中文 (默认)")]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 11

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    menu = window._build_video_context_menu()
    secondary_position_menu = next(action.menu() for action in menu.actions() if action.text() == "次字幕位置")

    assert secondary_position_menu is not None
    assert secondary_position_menu.isEnabled() is False
    assert "次字幕位置设置失败" not in window.log_view.toPlainText()


def test_player_window_right_click_on_video_surface_opens_context_menu(qtbot, monkeypatch) -> None:
    shown: list[tuple[int, int]] = []

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 0

    monkeypatch.setattr(
        PlayerWindow,
        "_show_video_context_menu",
        lambda self, pos: shown.append((pos.x(), pos.y())),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.show()

    local_pos = window.video_widget.rect().center()
    global_pos = window.video_widget.mapToGlobal(local_pos)
    QApplication.sendEvent(
        window.video_widget,
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            local_pos,
            global_pos,
            Qt.MouseButton.RightButton,
            Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )

    assert shown == [(local_pos.x(), local_pos.y())]


def test_player_window_right_click_on_video_child_maps_position_to_video_widget(qtbot, monkeypatch) -> None:
    shown: list[tuple[int, int]] = []

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 0

    monkeypatch.setattr(
        PlayerWindow,
        "_show_video_context_menu",
        lambda self, pos: shown.append((pos.x(), pos.y())),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.show()

    child = QWidget(window.video_widget)
    child.setGeometry(40, 30, 120, 80)
    child.show()
    window._configure_video_surface_widgets()

    local_pos = child.rect().center()
    global_pos = child.mapToGlobal(local_pos)
    QApplication.sendEvent(
        child,
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            local_pos,
            global_pos,
            Qt.MouseButton.RightButton,
            Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )

    expected = window.video_widget.mapFromGlobal(global_pos)
    assert shown == [(expected.x(), expected.y())]


def test_player_window_right_click_on_video_child_added_after_load_opens_context_menu(qtbot, monkeypatch) -> None:
    shown: list[tuple[int, int]] = []

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 1

        def duration_seconds(self) -> int:
            return 120

    monkeypatch.setattr(
        PlayerWindow,
        "_show_video_context_menu",
        lambda self, pos: shown.append((pos.x(), pos.y())),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.show()

    window.open_session(make_player_session(start_index=0))

    child = QWidget(window.video_widget)
    child.setGeometry(40, 30, 120, 80)
    child.show()

    local_pos = child.rect().center()
    global_pos = child.mapToGlobal(local_pos)
    QApplication.sendEvent(
        child,
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            local_pos,
            global_pos,
            Qt.MouseButton.RightButton,
            Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )

    expected = window.video_widget.mapFromGlobal(global_pos)
    assert shown == [(expected.x(), expected.y())]


def test_player_window_right_click_on_native_video_window_opens_context_menu(qtbot, monkeypatch) -> None:
    shown: list[tuple[int, int]] = []

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 1

        def duration_seconds(self) -> int:
            return 120

    monkeypatch.setattr(
        PlayerWindow,
        "_show_video_context_menu",
        lambda self, pos: shown.append((pos.x(), pos.y())),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.show()
    window.open_session(make_player_session(start_index=0))

    native_surface = QWindow()
    local_pos = window.video_widget.rect().center()
    global_pos = window.video_widget.mapToGlobal(local_pos)
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        local_pos,
        global_pos,
        Qt.MouseButton.RightButton,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
    )

    handled = window.eventFilter(native_surface, event)

    assert handled is True
    assert shown == [(local_pos.x(), local_pos.y())]


def test_player_window_context_menu_event_on_native_video_window_opens_context_menu(qtbot, monkeypatch) -> None:
    shown: list[tuple[int, int]] = []

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 1

        def duration_seconds(self) -> int:
            return 120

    monkeypatch.setattr(
        PlayerWindow,
        "_show_video_context_menu",
        lambda self, pos: shown.append((pos.x(), pos.y())),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.show()
    window.open_session(make_player_session(start_index=0))

    native_surface = QWindow()
    local_pos = window.video_widget.rect().center()
    global_pos = window.video_widget.mapToGlobal(local_pos)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, local_pos, global_pos)

    handled = window.eventFilter(native_surface, event)

    assert handled is True
    assert shown == [(local_pos.x(), local_pos.y())]


def test_player_window_left_click_on_native_video_window_closes_open_context_menu(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 1

        def duration_seconds(self) -> int:
            return 120

    class FakeMenu(QObject):
        aboutToHide = Signal()

        def __init__(self) -> None:
            super().__init__()
            self.visible = True
            self.hide_calls = 0

        def isVisible(self) -> bool:
            return self.visible

        def hide(self) -> None:
            self.hide_calls += 1
            self.visible = False
            self.aboutToHide.emit()

        def deleteLater(self) -> None:
            return None

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.show()
    window.open_session(make_player_session(start_index=0))

    fake_menu = FakeMenu()
    window._video_context_menu = fake_menu

    native_surface = QWindow()
    local_pos = window.video_widget.rect().center()
    global_pos = window.video_widget.mapToGlobal(local_pos)
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        local_pos,
        global_pos,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    handled = window.eventFilter(native_surface, event)

    assert handled is False
    assert fake_menu.hide_calls == 1
    assert window._video_context_menu is None


def test_player_window_opening_video_context_menu_closes_previous_menu(qtbot, monkeypatch) -> None:
    class FakeMenu(QObject):
        aboutToHide = Signal()

        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name
            self.visible = True
            self.exec_calls: list[tuple[int, int]] = []
            self.hide_calls = 0

        def exec(self, pos) -> None:
            self.visible = True
            self.exec_calls.append((pos.x(), pos.y()))

        def isVisible(self) -> bool:
            return self.visible

        def hide(self) -> None:
            self.hide_calls += 1
            self.visible = False
            self.aboutToHide.emit()

        def deleteLater(self) -> None:
            return None

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    menus = [FakeMenu("first"), FakeMenu("second")]
    monkeypatch.setattr(window, "_build_video_context_menu", lambda: menus.pop(0))

    first_pos = window.video_widget.rect().center()
    second_pos = first_pos + first_pos

    window._show_video_context_menu(first_pos)
    first_menu = window._video_context_menu
    assert first_menu is not None

    window._show_video_context_menu(second_pos)

    assert first_menu.hide_calls == 1
    assert window._video_context_menu is not None
    assert window._video_context_menu is not first_menu
    second_global_pos = window.video_widget.mapToGlobal(second_pos)
    assert window._video_context_menu.exec_calls == [(second_global_pos.x(), second_global_pos.y())]


def test_player_window_left_click_inside_open_menu_does_not_close_it(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 1

        def duration_seconds(self) -> int:
            return 120

    class FakeMenu(QObject):
        aboutToHide = Signal()

        def __init__(self, geometry: QRect) -> None:
            super().__init__()
            self.visible = True
            self.hide_calls = 0
            self._geometry = geometry

        def isVisible(self) -> bool:
            return self.visible

        def hide(self) -> None:
            self.hide_calls += 1
            self.visible = False
            self.aboutToHide.emit()

        def geometry(self) -> QRect:
            return self._geometry

        def deleteLater(self) -> None:
            return None

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.show()
    window.open_session(make_player_session(start_index=0))

    menu_rect = QRect(window.video_widget.mapToGlobal(window.video_widget.rect().center()), window.video_widget.rect().center())
    fake_menu = FakeMenu(menu_rect)
    window._video_context_menu = fake_menu

    native_surface = QWindow()
    global_pos = menu_rect.center()
    local_pos = window.video_widget.mapFromGlobal(global_pos)
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        local_pos,
        global_pos,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    handled = window.eventFilter(native_surface, event)

    assert handled is False
    assert fake_menu.hide_calls == 0
    assert window._video_context_menu is fake_menu


def test_player_window_app_level_left_click_outside_menu_closes_it(qtbot) -> None:
    class FakeMenu(QObject):
        aboutToHide = Signal()

        def __init__(self, geometry: QRect) -> None:
            super().__init__()
            self.visible = True
            self.hide_calls = 0
            self._geometry = geometry

        def isVisible(self) -> bool:
            return self.visible

        def hide(self) -> None:
            self.hide_calls += 1
            self.visible = False
            self.aboutToHide.emit()

        def geometry(self) -> QRect:
            return self._geometry

        def deleteLater(self) -> None:
            return None

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    menu_rect = QRect(window.video_widget.mapToGlobal(window.video_widget.rect().center()), window.video_widget.rect().center())
    fake_menu = FakeMenu(menu_rect)
    window._video_context_menu = fake_menu

    other_widget = QWidget(window)
    other_widget.setGeometry(10, 10, 40, 40)
    other_widget.show()
    local_pos = other_widget.rect().center()
    global_pos = other_widget.mapToGlobal(local_pos)
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        local_pos,
        global_pos,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    handled = window.eventFilter(other_widget, event)

    assert handled is False
    assert fake_menu.hide_calls == 1
    assert window._video_context_menu is None


def test_player_window_mpv_right_click_signal_opens_context_menu_at_cursor(qtbot, monkeypatch) -> None:
    shown: list[tuple[int, int]] = []

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 1

        def duration_seconds(self) -> int:
            return 120

    monkeypatch.setattr(
        PlayerWindow,
        "_show_video_context_menu",
        lambda self, pos: shown.append((pos.x(), pos.y())),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.show()
    window.open_session(make_player_session(start_index=0))

    local_pos = window.video_widget.rect().center()
    global_pos = window.video_widget.mapToGlobal(local_pos)
    monkeypatch.setattr(player_window_module.QCursor, "pos", staticmethod(lambda: global_pos))

    window.video_widget.context_menu_requested.emit()

    assert shown == [(local_pos.x(), local_pos.y())]


def test_player_window_mpv_left_click_signal_closes_open_menu_at_cursor(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 1

        def duration_seconds(self) -> int:
            return 120

    class FakeMenu(QObject):
        aboutToHide = Signal()

        def __init__(self, geometry: QRect) -> None:
            super().__init__()
            self.visible = True
            self.hide_calls = 0
            self._geometry = geometry

        def isVisible(self) -> bool:
            return self.visible

        def hide(self) -> None:
            self.hide_calls += 1
            self.visible = False
            self.aboutToHide.emit()

        def geometry(self) -> QRect:
            return self._geometry

        def deleteLater(self) -> None:
            return None

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.show()
    window.open_session(make_player_session(start_index=0))

    menu_rect = QRect(window.video_widget.mapToGlobal(window.video_widget.rect().center()), window.video_widget.rect().center())
    fake_menu = FakeMenu(menu_rect)
    window._video_context_menu = fake_menu

    outside_widget = QWidget(window)
    outside_widget.setGeometry(10, 10, 40, 40)
    outside_widget.show()
    monkeypatch.setattr(player_window_module.QCursor, "pos", staticmethod(lambda: outside_widget.mapToGlobal(outside_widget.rect().center())))

    window.video_widget.context_menu_dismiss_requested.emit()

    assert fake_menu.hide_calls == 1
    assert window._video_context_menu is None


def test_player_window_mpv_duplicate_open_request_does_not_reopen_visible_menu(qtbot, monkeypatch) -> None:
    class FakeMenu(QObject):
        aboutToHide = Signal()

        def __init__(self, geometry: QRect) -> None:
            super().__init__()
            self.visible = True
            self.hide_calls = 0
            self._geometry = geometry

        def isVisible(self) -> bool:
            return self.visible

        def hide(self) -> None:
            self.hide_calls += 1
            self.visible = False
            self.aboutToHide.emit()

        def geometry(self) -> QRect:
            return self._geometry

        def deleteLater(self) -> None:
            return None

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    menu_rect = QRect(window.video_widget.mapToGlobal(window.video_widget.rect().center()), window.video_widget.rect().center())
    fake_menu = FakeMenu(menu_rect)
    window._video_context_menu = fake_menu

    rebuilt = {"count": 0}
    monkeypatch.setattr(window, "_build_video_context_menu", lambda: rebuilt.__setitem__("count", rebuilt["count"] + 1))
    monkeypatch.setattr(player_window_module.QCursor, "pos", staticmethod(lambda: menu_rect.center()))

    window._show_video_context_menu_at_cursor()

    assert fake_menu.hide_calls == 0
    assert rebuilt["count"] == 0
    assert window._video_context_menu is fake_menu


def test_player_window_recent_duplicate_open_request_ignores_same_click_before_menu_is_visible(qtbot, monkeypatch) -> None:
    class FakeMenu(QObject):
        aboutToHide = Signal()

        def __init__(self) -> None:
            super().__init__()
            self.visible = False
            self.exec_calls: list[tuple[int, int]] = []
            self.hide_calls = 0

        def exec(self, pos) -> None:
            self.exec_calls.append((pos.x(), pos.y()))

        def isVisible(self) -> bool:
            return self.visible

        def hide(self) -> None:
            self.hide_calls += 1
            self.visible = False
            self.aboutToHide.emit()

        def deleteLater(self) -> None:
            return None

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    menus = [FakeMenu()]
    monkeypatch.setattr(window, "_build_video_context_menu", lambda: menus.pop(0))
    first_pos = window.video_widget.rect().center()
    global_pos = window.video_widget.mapToGlobal(first_pos)
    monkeypatch.setattr(player_window_module.QCursor, "pos", staticmethod(lambda: global_pos))

    window._show_video_context_menu(first_pos)
    first_menu = window._video_context_menu
    assert first_menu is not None

    window._show_video_context_menu_at_cursor()

    assert first_menu.exec_calls == [(global_pos.x(), global_pos.y())]
    assert first_menu.hide_calls == 0
    assert window._video_context_menu is first_menu


def test_player_window_populates_embedded_subtitle_options_after_open_session(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [
                SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
            ]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return 11 if mode == "auto" else track_id

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    assert [window.subtitle_combo.itemText(index) for index in range(window.subtitle_combo.count())] == [
        "字幕",
        "关闭字幕",
        "中文 (默认)",
        "English",
    ]
    assert window.subtitle_combo.isEnabled() is True
    assert window.video.subtitle_apply_calls[0] == ("auto", None)


def test_player_window_disables_subtitle_selector_when_current_item_has_no_embedded_subtitles(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    assert window.subtitle_combo.count() == 1
    assert window.subtitle_combo.itemText(0) == "字幕"
    assert window.subtitle_combo.isEnabled() is False


def test_player_window_user_selection_applies_selected_subtitle_track(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [
                SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
            ]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.video.subtitle_apply_calls.clear()

    window.subtitle_combo.setCurrentIndex(3)

    assert window.video.subtitle_apply_calls == [("track", 12)]


def test_player_window_reuses_subtitle_track_preference_for_next_episode(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.current_url = ""
            self.subtitle_apply_calls: list[tuple[str, str, int | None]] = []
            self.tracks_by_url = {
                "http://m/1.m3u8": [
                    SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                    SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
                ],
                "http://m/2.m3u8": [
                    SubtitleTrack(id=21, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                    SubtitleTrack(id=22, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
                ],
            }

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.current_url = url

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return self.tracks_by_url[self.current_url]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((self.current_url, mode, track_id))
            return track_id if mode == "track" else None

        def position_seconds(self) -> int:
            return 30

        def duration_seconds(self) -> int:
            return 120

    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.subtitle_combo.setCurrentIndex(3)
    window.video.subtitle_apply_calls.clear()

    window.play_next()

    assert ("http://m/2.m3u8", "track", 22) in window.video.subtitle_apply_calls
    assert window.subtitle_combo.currentText() == "English"


def test_player_window_falls_back_to_auto_when_previous_track_cannot_be_matched(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.current_url = ""
            self.subtitle_apply_calls: list[tuple[str, str, int | None]] = []
            self.tracks_by_url = {
                "http://m/1.m3u8": [
                    SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                    SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
                ],
                "http://m/2.m3u8": [
                    SubtitleTrack(id=21, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                ],
            }

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.current_url = url

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return self.tracks_by_url[self.current_url]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((self.current_url, mode, track_id))
            return 21 if mode == "auto" else track_id

        def position_seconds(self) -> int:
            return 30

        def duration_seconds(self) -> int:
            return 120

    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(make_player_session(start_index=0))
    window.subtitle_combo.setCurrentIndex(3)
    window.video.subtitle_apply_calls.clear()

    window.play_next()

    assert ("http://m/2.m3u8", "auto", None) in window.video.subtitle_apply_calls
    assert window.subtitle_combo.currentText() == "字幕"


def test_player_window_logs_and_resets_when_subtitle_refresh_fails(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            raise RuntimeError("boom")

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))

    assert "字幕加载失败: boom" in window.log_view.toPlainText()
    assert window.subtitle_combo.count() == 1
    assert window.subtitle_combo.itemText(0) == "字幕"
    assert window.subtitle_combo.isEnabled() is False


def test_player_window_refreshes_subtitle_options_when_mpv_reports_tracks_after_load(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    load_calls: list[tuple[str, bool, int]] = []
    subtitle_apply_calls: list[tuple[str, int | None]] = []
    tracks_call_count = {"count": 0}

    def fake_subtitle_tracks() -> list[SubtitleTrack]:
        tracks_call_count["count"] += 1
        if tracks_call_count["count"] == 1:
            return []
        return [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)")]

    window.video_widget.load = lambda url, pause=False, start_seconds=0: load_calls.append((url, pause, start_seconds))
    window.video_widget.set_speed = lambda speed: None
    window.video_widget.set_volume = lambda value: None
    window.video_widget.subtitle_tracks = fake_subtitle_tracks
    window.video_widget.apply_subtitle_mode = (
        lambda mode, track_id=None: subtitle_apply_calls.append((mode, track_id)) or (11 if mode == "auto" else track_id)
    )
    window.video_widget.position_seconds = lambda: 0

    window.open_session(make_player_session(start_index=0))

    assert load_calls == [("http://m/1.m3u8", False, 0)]
    assert window.subtitle_combo.count() == 1
    assert window.subtitle_combo.isEnabled() is False

    window.video_widget.subtitle_tracks_changed.emit()

    assert [window.subtitle_combo.itemText(index) for index in range(window.subtitle_combo.count())] == [
        "字幕",
        "关闭字幕",
        "中文 (默认)",
    ]
    assert window.subtitle_combo.isEnabled() is True
    assert subtitle_apply_calls == [("auto", None)]


def test_player_window_does_not_reapply_track_side_effects_when_track_list_updates_after_manual_subtitle_switch(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.secondary_subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.audio_apply_calls: list[tuple[str, int | None]] = []
            self.set_subtitle_position_calls: list[int] = []
            self.set_secondary_subtitle_position_calls: list[int] = []
            self.set_subtitle_scale_calls: list[int] = []
            self.set_secondary_subtitle_scale_calls: list[int] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [
                SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)"),
                SubtitleTrack(id=12, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
            ]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id if mode == "track" else 11

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.secondary_subtitle_apply_calls.append((mode, track_id))
            return track_id

        def subtitle_position(self) -> int:
            return 50

        def set_subtitle_position(self, value: int) -> None:
            self.set_subtitle_position_calls.append(value)

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            self.set_secondary_subtitle_position_calls.append(value)

        def supports_subtitle_scale(self) -> bool:
            return True

        def subtitle_scale(self) -> int:
            return 100

        def set_subtitle_scale(self, value: int) -> None:
            self.set_subtitle_scale_calls.append(value)

        def supports_secondary_subtitle_scale(self) -> bool:
            return True

        def secondary_subtitle_scale(self) -> int:
            return 100

        def set_secondary_subtitle_scale(self, value: int) -> None:
            self.set_secondary_subtitle_scale_calls.append(value)

        def audio_tracks(self) -> list[AudioTrack]:
            return [
                AudioTrack(id=1, title="中文", lang="zh", is_default=True, is_forced=False, label="中文"),
                AudioTrack(id=2, title="English", lang="eng", is_default=False, is_forced=False, label="English"),
            ]

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((mode, track_id))
            return track_id if mode == "track" else 1

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(make_player_session(start_index=0))
    window.video.subtitle_apply_calls.clear()
    window.video.secondary_subtitle_apply_calls.clear()
    window.video.audio_apply_calls.clear()
    window.video.set_subtitle_position_calls.clear()
    window.video.set_secondary_subtitle_position_calls.clear()
    window.video.set_subtitle_scale_calls.clear()
    window.video.set_secondary_subtitle_scale_calls.clear()

    window.subtitle_combo.setCurrentIndex(3)
    window.video_widget.subtitle_tracks_changed.emit()
    window.video_widget.audio_tracks_changed.emit()

    assert window.video.subtitle_apply_calls == [("track", 12)]
    assert window.video.secondary_subtitle_apply_calls == []
    assert window.video.audio_apply_calls == []
    assert window.video.set_subtitle_position_calls == []
    assert window.video.set_secondary_subtitle_position_calls == []
    assert window.video.set_subtitle_scale_calls == []
    assert window.video.set_secondary_subtitle_scale_calls == []
    assert window.subtitle_combo.currentText() == "English"


def test_player_window_enables_danmaku_by_default_when_current_item_has_danmaku(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self.removed_danmaku_track_ids: list[int] = []
            self.set_secondary_subtitle_position_calls: list[int] = []
            self.set_secondary_subtitle_ass_override_calls: list[str] = []
            self._next_track_id = 40

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            assert select_for_secondary is True
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            if track_id is not None:
                self.removed_danmaku_track_ids.append(track_id)

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def set_secondary_subtitle_position(self, value: int) -> None:
            self.set_secondary_subtitle_position_calls.append(value)

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return True

        def secondary_subtitle_ass_override(self) -> str:
            return "strip"

        def set_secondary_subtitle_ass_override(self, value: str) -> None:
            self.set_secondary_subtitle_ass_override_calls.append(value)

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml=(
                    '<?xml version="1.0" encoding="UTF-8"?><i>'
                    '<d p="0.0,1,25,16777215">第一条</d>'
                    '<d p="0.5,1,25,16777215">第二条</d>'
                    "</i>"
                ),
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.danmaku_combo.isEnabled() is True
    assert window.danmaku_combo.currentText() == "弹幕"
    assert len(window.video.loaded_danmaku_paths) == 1
    assert window.video.set_secondary_subtitle_position_calls == []
    assert window.video.set_secondary_subtitle_ass_override_calls[-1] == "no"
    assert Path(window.video.loaded_danmaku_paths[0]).read_text(encoding="utf-8").startswith("[Script Info]")


def test_player_window_uses_saved_off_danmaku_preference_on_open_session(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            return 70

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), config=AppConfig(preferred_danmaku_enabled=False))
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.video.loaded_danmaku_paths == []
    assert window.danmaku_combo.currentText() == "关闭"


def test_player_window_uses_saved_danmaku_line_count_on_open_session(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 70

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(preferred_danmaku_enabled=True, preferred_danmaku_line_count=4),
    )
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert len(window.video.loaded_danmaku_paths) == 1
    assert window.danmaku_combo.currentText() == "4行"


def test_player_window_changes_danmaku_mode_without_affecting_playback(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self.removed_danmaku_track_ids: list[int] = []
            self.set_secondary_subtitle_position_calls: list[int] = []
            self.secondary_subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._next_track_id = 70

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            if track_id is not None:
                self.removed_danmaku_track_ids.append(track_id)

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def set_secondary_subtitle_position(self, value: int) -> None:
            self.set_secondary_subtitle_position_calls.append(value)

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.secondary_subtitle_apply_calls.append((mode, track_id))
            return track_id

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    initial_loaded_count = len(window.video.loaded_danmaku_paths)

    window.danmaku_combo.setCurrentIndex(1)
    window.danmaku_combo.setCurrentIndex(4)

    assert len(window.video.loaded_danmaku_paths) == initial_loaded_count + 1
    assert window.video.removed_danmaku_track_ids == [70]
    assert window.video.secondary_subtitle_apply_calls == []
    assert window.danmaku_combo.currentText() == "3行"


def test_player_window_saves_preferred_danmaku_selection_when_user_changes_combo(qtbot) -> None:
    saved = {"called": 0}

    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self.removed_danmaku_track_ids: list[int] = []
            self._next_track_id = 70

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            if track_id is not None:
                self.removed_danmaku_track_ids.append(track_id)

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    config = AppConfig()
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("called", saved["called"] + 1),
    )
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    saved["called"] = 0
    window.danmaku_combo.setCurrentIndex(5)

    assert config.preferred_danmaku_enabled is True
    assert config.preferred_danmaku_line_count == 4
    assert saved["called"] == 1


def test_player_window_keeps_danmaku_temp_file_until_player_loads_it(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 70

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            assert Path(path).exists()
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert len(window.video.loaded_danmaku_paths) == 1
    assert window.video.loaded_danmaku_paths[0].endswith(".ass")
    assert Path(window.video.loaded_danmaku_paths[0]).read_text(encoding="utf-8").startswith("[Script Info]")


def test_player_window_falls_back_when_secondary_danmaku_track_is_unsupported(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[tuple[str, bool]] = []
            self.set_secondary_subtitle_ass_override_calls: list[str] = []
            self.set_subtitle_ass_override_calls: list[str] = []
            self._next_track_id = 70

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return True

        def secondary_subtitle_ass_override(self) -> str:
            return "strip"

        def set_secondary_subtitle_ass_override(self, value: str) -> None:
            self.set_secondary_subtitle_ass_override_calls.append(value)

        def supports_subtitle_ass_override(self) -> bool:
            return True

        def subtitle_ass_override(self) -> str:
            return "scale"

        def set_subtitle_ass_override(self, value: str) -> None:
            self.set_subtitle_ass_override_calls.append(value)

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append((path, select_for_secondary))
            if select_for_secondary:
                raise RuntimeError("secondary unsupported")
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.danmaku_combo.isEnabled() is True
    assert window.danmaku_combo.currentText() == "弹幕"
    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_danmaku_paths] == [True, False]
    assert window.video.set_secondary_subtitle_ass_override_calls[-1] == "no"
    assert window.video.set_subtitle_ass_override_calls[-1] == "no"
    assert Path(window.video.loaded_danmaku_paths[-1][0]).read_text(encoding="utf-8").startswith("[Script Info]")
    assert "弹幕加载失败" not in window.log_view.toPlainText()


def test_player_window_uses_primary_slot_when_secondary_ass_override_is_unsupported(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[tuple[str, bool]] = []
            self.set_subtitle_ass_override_calls: list[str] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._next_track_id = 90

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return False

        def supports_subtitle_ass_override(self) -> bool:
            return True

        def subtitle_ass_override(self) -> str:
            return "scale"

        def set_subtitle_ass_override(self, value: str) -> None:
            self.set_subtitle_ass_override_calls.append(value)

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append((path, select_for_secondary))
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_danmaku_paths] == [False]
    assert window.video.set_subtitle_ass_override_calls[-1] == "no"
    assert window.video.subtitle_apply_calls[-1] == ("track", 90)


def test_player_window_retries_danmaku_load_after_initial_mpv_command_failure(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[tuple[str, bool]] = []
            self.set_secondary_subtitle_ass_override_calls: list[str] = []
            self._next_track_id = 110
            self._fail_first_load = True

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return True

        def secondary_subtitle_ass_override(self) -> str:
            return "strip"

        def set_secondary_subtitle_ass_override(self, value: str) -> None:
            self.set_secondary_subtitle_ass_override_calls.append(value)

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append((path, select_for_secondary))
            if self._fail_first_load:
                self._fail_first_load = False
                raise RuntimeError("Error running mpv command")
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_danmaku_paths) >= 2)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_danmaku_paths] == [True, True]
    assert "弹幕加载失败" not in window.log_view.toPlainText()


def test_player_window_does_not_disable_danmaku_when_track_list_refresh_arrives_during_load(qtbot) -> None:
    class FakeVideo:
        def __init__(self, window: PlayerWindow) -> None:
            self.window = window
            self.loaded_danmaku_paths: list[tuple[str, bool]] = []
            self.secondary_subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.set_secondary_subtitle_ass_override_calls: list[str] = []
            self._next_track_id = 120
            self._loaded_track_id: int | None = None

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            if self._loaded_track_id is None:
                return []
            return [
                SubtitleTrack(
                    id=self._loaded_track_id,
                    title="danmaku",
                    lang="",
                    is_default=False,
                    is_forced=False,
                    label="danmaku",
                )
            ]

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return True

        def secondary_subtitle_ass_override(self) -> str:
            return "strip"

        def set_secondary_subtitle_ass_override(self, value: str) -> None:
            self.set_secondary_subtitle_ass_override_calls.append(value)

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append((path, select_for_secondary))
            track_id = self._next_track_id
            self._next_track_id += 1
            self._loaded_track_id = track_id
            self.window.video_widget.subtitle_tracks_changed.emit()
            return track_id

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.secondary_subtitle_apply_calls.append((mode, track_id))
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def set_secondary_subtitle_position(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo(window)

    window.open_session(session)

    assert window.video.secondary_subtitle_apply_calls == []
    assert window.danmaku_combo.currentText() == "弹幕"


def test_player_window_does_not_disable_primary_fallback_danmaku_when_track_list_refresh_arrives_during_load(qtbot) -> None:
    class FakeVideo:
        def __init__(self, window: PlayerWindow) -> None:
            self.window = window
            self.loaded_danmaku_paths: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.set_subtitle_ass_override_calls: list[str] = []
            self._next_track_id = 130
            self._loaded_track_id: int | None = None

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            if self._loaded_track_id is None:
                return []
            return [
                SubtitleTrack(
                    id=self._loaded_track_id,
                    title="danmaku",
                    lang="",
                    is_default=False,
                    is_forced=False,
                    label="danmaku",
                )
            ]

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return False

        def supports_subtitle_ass_override(self) -> bool:
            return True

        def subtitle_ass_override(self) -> str:
            return "scale"

        def set_subtitle_ass_override(self, value: str) -> None:
            self.set_subtitle_ass_override_calls.append(value)

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append((path, select_for_secondary))
            track_id = self._next_track_id
            self._next_track_id += 1
            self._loaded_track_id = track_id
            self.window.video_widget.subtitle_tracks_changed.emit()
            return track_id

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo(window)

    window.open_session(session)

    assert window.video.loaded_danmaku_paths == [(window.video.loaded_danmaku_paths[0][0], False)]
    assert window.video.subtitle_apply_calls == [("track", 130)]
    assert window.danmaku_combo.currentText() == "弹幕"


def test_player_window_keeps_secondary_subtitle_preference_out_of_danmaku_slot(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.secondary_subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.loaded_danmaku_paths: list[str] = []
            self.set_secondary_subtitle_position_calls: list[int] = []
            self._next_track_id = 99

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [
                SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文"),
            ]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 11

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.secondary_subtitle_apply_calls.append((mode, track_id))
            return track_id

        def subtitle_position(self) -> int:
            return 50

        def supports_secondary_subtitle_position(self) -> bool:
            return True

        def secondary_subtitle_position(self) -> int:
            return 50

        def set_secondary_subtitle_position(self, value: int) -> None:
            self.set_secondary_subtitle_position_calls.append(value)

        def supports_subtitle_scale(self) -> bool:
            return False

        def supports_secondary_subtitle_scale(self) -> bool:
            return False

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            return self._next_track_id

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.video.secondary_subtitle_apply_calls.clear()

    window.video_widget.subtitle_tracks_changed.emit()

    assert window.video.secondary_subtitle_apply_calls == []


def test_player_window_loads_danmaku_after_async_resolution_completes(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return False

        def supports_subtitle_ass_override(self) -> bool:
            return False

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                danmaku_pending=True,
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.video.loaded_danmaku_paths == []

    session.playlist[0].danmaku_xml = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'
    session.playlist[0].danmaku_pending = False

    qtbot.waitUntil(lambda: len(window.video.loaded_danmaku_paths) == 1)
    assert window.danmaku_combo.currentText() == "弹幕"


def test_player_window_applies_saved_danmaku_line_count_after_async_resolution(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def supports_secondary_subtitle_ass_override(self) -> bool:
            return False

        def supports_subtitle_ass_override(self) -> bool:
            return False

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="第1集", url="http://m/1.m3u8", danmaku_pending=True)],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(preferred_danmaku_enabled=True, preferred_danmaku_line_count=3),
    )
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    session.playlist[0].danmaku_xml = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'
    session.playlist[0].danmaku_pending = False

    qtbot.waitUntil(lambda: len(window.video.loaded_danmaku_paths) == 1)
    assert window.danmaku_combo.currentText() == "3行"


def test_player_window_uses_distinct_seek_icons(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.prev_button.icon().pixmap(24, 24).toImage() != window.backward_button.icon().pixmap(24, 24).toImage()
    assert window.next_button.icon().pixmap(24, 24).toImage() != window.forward_button.icon().pixmap(24, 24).toImage()


def test_player_window_mute_button_icon_tracks_mute_state(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.toggle_mute_calls = 0

        def toggle_mute(self) -> None:
            self.toggle_mute_calls += 1

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    unmuted_icon = window.mute_button.icon().pixmap(24, 24).toImage()

    window.mute_button.click()
    muted_icon = window.mute_button.icon().pixmap(24, 24).toImage()

    window.mute_button.click()

    assert window.video.toggle_mute_calls == 2
    assert muted_icon != unmuted_icon
    assert window.mute_button.icon().pixmap(24, 24).toImage() == unmuted_icon


def test_player_window_refresh_button_replays_current_item(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.set_speed_calls: list[float] = []
            self.set_volume_calls: list[int] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))

        def set_speed(self, value: float) -> None:
            self.set_speed_calls.append(value)

        def set_volume(self, value: int) -> None:
            self.set_volume_calls.append(value)

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.volume_slider.setValue(35)
    window.open_session(make_player_session(start_index=1, speed=1.5))
    window.video.load_calls.clear()
    window.video.set_speed_calls.clear()
    window.video.set_volume_calls.clear()

    window.refresh_button.click()

    assert window.current_index == 1
    assert window.playlist.currentRow() == 1
    assert window.video.load_calls == [("http://m/2.m3u8", 0)]
    assert window.video.set_speed_calls == [1.5]
    assert window.video.set_volume_calls == [35]


def test_player_window_refresh_button_restores_active_playback_title(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1))

    window.toggle_playback()

    assert window.windowTitle() == "alist-tvbox 播放器"

    window.refresh_button.click()

    assert window.windowTitle() == "Movie - Episode 2"


def test_player_window_restores_saved_volume_for_new_session(qtbot) -> None:
    config = AppConfig(player_volume=35)
    window = PlayerWindow(FakePlayerController(), config=config)
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    assert window.volume_slider.value() == 35

    window.open_session(make_player_session(start_index=0))

    assert window.video.set_volume_calls[-1] == 35


def test_player_window_restores_saved_mute_for_new_session(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.set_speed_calls: list[float] = []
            self.set_volume_calls: list[int] = []
            self.set_muted_calls: list[bool] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))

        def set_speed(self, value: float) -> None:
            self.set_speed_calls.append(value)

        def set_volume(self, value: int) -> None:
            self.set_volume_calls.append(value)

        def set_muted(self, muted: bool) -> None:
            self.set_muted_calls.append(muted)

    config = AppConfig(player_muted=True)
    window = PlayerWindow(FakePlayerController(), config=config)
    qtbot.addWidget(window)
    window.video = FakeVideo()
    expected_muted_icon = window._create_icon_button("volume-off.svg", "静音", "M").icon().pixmap(24, 24).toImage()

    window.open_session(make_player_session(start_index=0))

    assert window.video.set_muted_calls == [True]
    assert window.mute_button.icon().pixmap(24, 24).toImage() == expected_muted_icon


def test_player_window_volume_changes_persist_to_config(qtbot) -> None:
    config = AppConfig(player_volume=35)
    saved = {"count": 0}
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.volume_slider.setValue(60)

    assert config.player_volume == 60
    assert window.video.set_volume_calls == [60]
    assert saved["count"] >= 1


def test_player_window_mute_changes_persist_to_config(qtbot) -> None:
    config = AppConfig(player_muted=False)
    saved = {"count": 0}

    class FakeVideo:
        def __init__(self) -> None:
            self.toggle_mute_calls = 0

        def toggle_mute(self) -> None:
            self.toggle_mute_calls += 1

    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.mute_button.click()

    assert config.player_muted is True
    assert window.video.toggle_mute_calls == 1
    assert saved["count"] >= 1


def test_player_window_advances_to_next_item_when_playback_finishes(qtbot) -> None:
    controller = RecordingPlayerController()
    video = RecordingVideo()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))

    video.load_calls.clear()

    window.video_widget.playback_finished.emit()

    assert window.current_index == 1
    assert window.playlist.currentRow() == 1
    assert video.load_calls == [("http://m/2.m3u8", 0)]
    qtbot.waitUntil(lambda: controller.progress_calls == [(0, 30, 1.0, 0, 0, False)])
    assert controller.progress_calls == [(0, 30, 1.0, 0, 0, False)]


def test_player_window_play_next_resolves_target_episode_before_loading(qtbot) -> None:
    controller = RecordingPlayerController()
    resolved_vod = VodItem(
        vod_id="ep-2",
        vod_name="Resolved Episode 2",
        vod_content="resolved episode content",
        items=[PlayItem(title="Episode 2", url="http://resolved/2.m3u8", vod_id="ep-2")],
    )

    class FakeVideo(RecordingVideo):
        pass

    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = FakeVideo()
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(title="Episode 1", url="http://m/1.m3u8"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]
    session.detail_resolver = lambda item: resolved_vod
    window.open_session(session)
    window.video.load_calls.clear()

    window.play_next()

    qtbot.waitUntil(lambda: window.video.load_calls == [("http://resolved/2.m3u8", 0)])
    assert window.current_index == 1
    assert "resolved episode content" in window.metadata_view.toPlainText()


def test_player_window_reuses_cached_detail_when_returning_to_same_episode(qtbot) -> None:
    controller = RecordingPlayerController()
    detail_calls: list[str] = []

    def detail_resolver(item: PlayItem) -> VodItem:
        detail_calls.append(item.vod_id)
        return VodItem(
            vod_id=item.vod_id,
            vod_name=f"Resolved {item.title}",
            items=[PlayItem(title=item.title, url=f"http://resolved/{item.vod_id}.m3u8", vod_id=item.vod_id)],
        )

    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(title="Episode 1", url="http://m/1.m3u8"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]
    session.detail_resolver = detail_resolver
    window.open_session(session)
    detail_calls.clear()
    window.video.load_calls.clear()

    window.play_next()
    qtbot.waitUntil(lambda: ("http://resolved/ep-2.m3u8", 0) in window.video.load_calls)
    window.play_previous()
    window.play_next()

    assert detail_calls == ["ep-2"]
    assert ("http://resolved/ep-2.m3u8", 0) in window.video.load_calls


def test_player_window_keeps_current_index_when_next_episode_detail_resolution_fails(qtbot) -> None:
    controller = RecordingPlayerController()

    def detail_resolver(item: PlayItem) -> VodItem:
        if item.vod_id == "ep-2":
            raise RuntimeError("detail failed")
        return VodItem(
            vod_id=item.vod_id,
            vod_name=item.title,
            items=[PlayItem(title=item.title, url=f"http://resolved/{item.vod_id}.m3u8", vod_id=item.vod_id)],
        )

    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(title="Episode 1", url="http://m/1.m3u8"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]
    session.detail_resolver = detail_resolver
    window.open_session(session)
    window.video.load_calls.clear()

    window.play_next()

    qtbot.waitUntil(lambda: "播放失败: detail failed" in window.log_view.toPlainText())
    assert window.current_index == 0
    assert window.video.load_calls == []
    assert "播放失败: detail failed" in window.log_view.toPlainText()


def test_player_window_play_next_resolves_target_episode_without_blocking_ui(qtbot) -> None:
    controller = RecordingPlayerController()
    release_resolution = threading.Event()
    resolved_vod = VodItem(
        vod_id="ep-2",
        vod_name="Resolved Episode 2",
        vod_content="resolved episode content",
        items=[PlayItem(title="Episode 2", url="http://resolved/2.m3u8", vod_id="ep-2")],
    )
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(title="Episode 1", url="http://m/1.m3u8"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]

    def detail_resolver(item: PlayItem) -> VodItem:
        release_resolution.wait(timeout=1.0)
        return resolved_vod

    session.detail_resolver = detail_resolver
    window.open_session(session)
    window.video.load_calls.clear()
    release_event_after(0.2, release_resolution)

    started_at = time.perf_counter()
    window.play_next()
    elapsed_seconds = time.perf_counter() - started_at

    assert elapsed_seconds < 0.1
    assert window.current_index == 1
    assert window.video.load_calls == []

    qtbot.waitUntil(lambda: window.video.load_calls == [("http://resolved/2.m3u8", 0)])
    assert "resolved episode content" in window.metadata_view.toPlainText()


def test_player_window_reverts_index_after_async_detail_resolution_failure(qtbot) -> None:
    controller = RecordingPlayerController()
    release_resolution = threading.Event()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(title="Episode 1", url="http://m/1.m3u8"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]

    def detail_resolver(item: PlayItem) -> VodItem:
        release_resolution.wait(timeout=1.0)
        raise RuntimeError("detail failed")

    session.detail_resolver = detail_resolver
    window.open_session(session)
    window.video.load_calls.clear()
    release_event_after(0.2, release_resolution)

    started_at = time.perf_counter()
    window.play_next()
    elapsed_seconds = time.perf_counter() - started_at

    assert elapsed_seconds < 0.1
    assert window.current_index == 1

    qtbot.waitUntil(lambda: window.current_index == 0)
    assert window.playlist.currentRow() == 0
    assert window.video.load_calls == []
    assert "播放失败: detail failed" in window.log_view.toPlainText()


def test_player_window_loads_play_item_via_session_loader_and_passes_headers(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int, dict[str, str]]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            self.load_calls.append((url, pause, start_seconds, headers or {}))

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    controller = RecordingPlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = FakeVideo()
    session = make_player_session(start_index=0)
    session.playlist = [PlayItem(title="Episode 1", url="", vod_id="1-3458")]
    session.use_local_history = False
    session.playback_loader = lambda item: (setattr(item, "url", "http://emby/1.mp4"), setattr(item, "headers", {"User-Agent": "Yamby"}))

    window.open_session(session)

    assert window.video.load_calls == [("http://emby/1.mp4", False, 0, {"User-Agent": "Yamby"})]


def test_player_window_replaces_active_route_playlist_when_playback_loader_returns_replacement(qtbot) -> None:
    controller = FakePlayerController()
    replacement = [
        PlayItem(title="S1 - 1", url="http://m/1.mp4", play_source="quark"),
        PlayItem(title="S1 - 2", url="http://m/2.mp4", play_source="quark"),
    ]

    def load_item(item: PlayItem):
        assert item.title == "查看"
        return PlaybackLoadResult(replacement_playlist=replacement, replacement_start_index=0)

    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘剧集"),
        playlist=[PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="quark")],
        playlists=[
            [PlayItem(title="第1集", url="http://line/1.m3u8", play_source="播放源 1")],
            [PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="quark")],
        ],
        playlist_index=1,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        playback_loader=load_item,
    )

    window = PlayerWindow(controller, config=None, save_config=lambda: None)
    qtbot.addWidget(window)

    window.open_session(session)

    assert window.session is not None
    assert window.session.playlist_index == 1
    assert [item.title for item in window.session.playlist] == ["S1 - 1", "S1 - 2"]
    assert [item.title for item in window.session.playlists[1]] == ["S1 - 1", "S1 - 2"]
    assert window.current_index == 0
    assert window.playlist.count() == 2
    assert window.playlist.item(0).text() == "S1 - 1"


def test_player_window_route_replacement_keeps_other_route_groups_unchanged(qtbot) -> None:
    controller = FakePlayerController()
    first_group = [PlayItem(title="第1集", url="http://line/1.m3u8", play_source="播放源 1")]
    drive_group = [PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="quark")]

    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘剧集"),
        playlist=drive_group,
        playlists=[first_group, drive_group],
        playlist_index=1,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        playback_loader=lambda item: PlaybackLoadResult(
            replacement_playlist=[PlayItem(title="S1 - 1", url="http://m/1.mp4", play_source="quark")],
            replacement_start_index=0,
        ),
    )

    window = PlayerWindow(controller, config=None, save_config=lambda: None)
    qtbot.addWidget(window)

    window.open_session(session)

    assert window.session is not None
    assert [item.title for item in window.session.playlists[0]] == ["第1集"]
    assert [item.title for item in window.session.playlists[1]] == ["S1 - 1"]


def test_player_window_route_selector_uses_formatted_spider_play_source_label(qtbot) -> None:
    controller = FakePlayerController()
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘剧集"),
        playlist=[PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="网盘线(夸克)")],
        playlists=[
            [PlayItem(title="第1集", url="http://line/1.m3u8", play_source="播放源 1")],
            [PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="网盘线(夸克)")],
        ],
        playlist_index=1,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window = PlayerWindow(controller, config=None, save_config=lambda: None)
    qtbot.addWidget(window)

    window.open_session(session)

    assert window.playlist_group_combo.count() == 2
    assert window.playlist_group_combo.itemText(0) == "播放源 1"
    assert window.playlist_group_combo.itemText(1) == "网盘线(夸克)"


def test_player_window_stops_session_when_switching_items(qtbot) -> None:
    controller = RecordingPlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = make_player_session(start_index=0)

    window.open_session(session)
    controller.stop_calls.clear()
    controller.progress_calls.clear()
    window.video.load_calls.clear()

    window.play_next()

    qtbot.waitUntil(
        lambda: controller.progress_calls == [(0, 30, 1.0, 0, 0, False)] and controller.stop_calls == [0]
    )
    assert controller.progress_calls == [(0, 30, 1.0, 0, 0, False)]
    assert controller.stop_calls == [0]
    assert window.video.load_calls == [("http://m/2.m3u8", 0)]


def test_player_window_play_next_reports_progress_and_stops_without_blocking_ui(qtbot) -> None:
    class SlowRecordingPlayerController(RecordingPlayerController):
        def report_progress(
            self,
            session,
            current_index: int,
            position_seconds: int,
            speed: float,
            opening_seconds: int,
            ending_seconds: int,
            paused: bool,
            force_remote_report: bool = False,
        ) -> None:
            time.sleep(0.15)
            super().report_progress(
                session,
                current_index,
                position_seconds,
                speed,
                opening_seconds,
                ending_seconds,
                paused,
                force_remote_report,
            )

        def stop_playback(self, session, current_index: int) -> None:
            time.sleep(0.15)
            super().stop_playback(session, current_index)

    controller = SlowRecordingPlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=0))
    controller.progress_calls.clear()
    controller.stop_calls.clear()
    window.video.load_calls.clear()

    started_at = time.perf_counter()
    window.play_next()
    elapsed_seconds = time.perf_counter() - started_at

    assert elapsed_seconds < 0.1
    assert window.current_index == 1
    assert window.video.load_calls == [("http://m/2.m3u8", 0)]
    qtbot.waitUntil(
        lambda: controller.progress_calls == [(0, 30, 1.0, 0, 0, False)] and controller.stop_calls == [0]
    )


def test_player_window_playback_controls_show_shortcuts_and_pointing_cursor(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.play_button.toolTip() == "播放/暂停 (Space)"
    assert window.prev_button.toolTip() == "上一集 (PgUp)"
    assert window.next_button.toolTip() == "下一集 (PgDn)"
    assert window.backward_button.toolTip() == "后退 (Left)"
    assert window.forward_button.toolTip() == "前进 (Right)"
    assert window.mute_button.toolTip() == "静音 (M)"
    assert window.fullscreen_button.toolTip() == "全屏 (Enter)"
    assert window.play_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert window.refresh_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert window.fullscreen_button.cursor().shape() == Qt.CursorShape.PointingHandCursor


def test_player_window_adds_padding_around_bottom_controls(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    margins = window.bottom_layout.contentsMargins()

    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (12, 6, 12, 6)
    assert window.bottom_area.maximumHeight() == 72


def test_player_window_mouse_activity_in_video_restores_cursor_and_starts_autohide(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    cursor_autohide_calls: list[int | None] = []
    window.video.set_cursor_autohide = lambda value: cursor_autohide_calls.append(value)
    window.is_playing = True
    window._video_pointer_inside = True
    window._set_video_cursor_hidden(True)

    window._handle_video_mouse_activity()

    assert window.video.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert window.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert window._cursor_hide_timer.isActive() is True
    assert cursor_autohide_calls == [2000]


def test_player_window_uses_three_second_cursor_autohide_delay(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window._CURSOR_HIDE_DELAY_MS == 2000


def test_player_window_child_video_surface_enter_starts_autohide(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    window.is_playing = True

    QApplication.sendEvent(window.video_widget._placeholder, QEvent(QEvent.Type.Enter))

    assert window._video_pointer_inside is True
    assert window._cursor_hide_timer.isActive() is True


def test_player_window_resuming_playback_starts_autohide_when_cursor_is_already_over_video(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    resume_calls = {"count": 0}
    window.video.resume = lambda: resume_calls.__setitem__("count", resume_calls["count"] + 1)
    center_point = window.video_widget.rect().center()
    global_point = window.video_widget.mapToGlobal(center_point)
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: global_point))
    cursor_autohide_calls: list[int | None] = []
    window.video.set_cursor_autohide = lambda value: cursor_autohide_calls.append(value)
    window.is_playing = False

    window.toggle_playback()

    assert resume_calls["count"] == 1
    assert window.is_playing is True
    assert window._video_pointer_inside is True
    assert window._cursor_hide_timer.isActive() is True
    assert cursor_autohide_calls[-1] == 2000


def test_player_window_app_level_mouse_move_over_video_starts_autohide(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    window.is_playing = True
    center_point = window.video_widget.rect().center()
    global_point = window.video_widget.mapToGlobal(center_point)
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: global_point))

    move_event = QMouseEvent(
        QEvent.Type.MouseMove,
        window.rect().center(),
        global_point,
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window, move_event)

    assert window._video_pointer_inside is True
    assert window._cursor_hide_timer.isActive() is True


def test_player_window_polling_hides_cursor_after_three_seconds_without_events(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    center_point = window.video_widget.rect().center()
    global_point = window.video_widget.mapToGlobal(center_point)
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: global_point))
    window.is_playing = True
    window._handle_video_mouse_activity(now_ms=1000)

    window._poll_cursor_idle_state(now_ms=4000)

    assert window._video_pointer_inside is True
    assert window.cursor().shape() == Qt.CursorShape.BlankCursor


def test_player_window_cursor_idle_hides_video_cursor_only_when_playing_and_inside(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    window.is_playing = True
    window._video_pointer_inside = True

    window._hide_video_cursor_if_idle()

    assert window.video.cursor().shape() == Qt.CursorShape.BlankCursor
    assert window.cursor().shape() == Qt.CursorShape.BlankCursor


def test_player_window_pausing_playback_restores_video_cursor_and_stops_autohide(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    pause_calls = {"count": 0}
    window.video.pause = lambda: pause_calls.__setitem__("count", pause_calls["count"] + 1)
    cursor_autohide_calls: list[int | None] = []
    window.video.set_cursor_autohide = lambda value: cursor_autohide_calls.append(value)
    window.is_playing = True
    window._video_pointer_inside = True
    window._cursor_hide_timer.start(1500)
    window._set_video_cursor_hidden(True)

    window.toggle_playback()

    assert pause_calls["count"] == 1
    assert window.is_playing is False
    assert window.video.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert window.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert window._cursor_hide_timer.isActive() is False
    assert cursor_autohide_calls[-1] is None


def test_player_window_video_leave_restores_cursor_and_keeps_polling_while_playing(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    cursor_autohide_calls: list[int | None] = []
    window.video.set_cursor_autohide = lambda value: cursor_autohide_calls.append(value)
    window.is_playing = True
    window._video_pointer_inside = True
    window._cursor_hide_timer.start(1500)
    window._set_video_cursor_hidden(True)

    window._handle_video_leave()

    assert window._video_pointer_inside is False
    assert window.video.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert window.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert window._cursor_hide_timer.isActive() is True
    assert cursor_autohide_calls[-1] == 2000


def test_player_window_mouse_move_outside_video_keeps_native_autohide_armed_while_playing(
    qtbot, monkeypatch
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    cursor_autohide_calls: list[int | None] = []
    window.video.set_cursor_autohide = lambda value: cursor_autohide_calls.append(value)
    window.is_playing = True
    outside_local = window.rect().bottomRight()
    outside_global = window.mapToGlobal(outside_local)
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: outside_global))

    window._video_pointer_inside = True
    window._handle_video_mouse_activity(now_ms=1000)
    move_event = QMouseEvent(
        QEvent.Type.MouseMove,
        outside_local,
        outside_global,
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )

    QApplication.sendEvent(window, move_event)

    assert window._video_pointer_inside is False
    assert window._cursor_hide_timer.isActive() is True
    assert cursor_autohide_calls[-1] == 2000


def test_player_window_polling_restarts_autohide_when_cursor_reenters_video_after_leave(
    qtbot, monkeypatch
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    center_point = window.video_widget.rect().center()
    inside_global = window.video_widget.mapToGlobal(center_point)
    outside_global = window.mapToGlobal(window.rect().bottomRight())
    current_pos = {"value": outside_global}
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: current_pos["value"]))
    cursor_autohide_calls: list[int | None] = []
    window.video.set_cursor_autohide = lambda value: cursor_autohide_calls.append(value)
    window.is_playing = True
    window._video_pointer_inside = True
    window._handle_video_mouse_activity(now_ms=1000)

    window._handle_video_leave()
    current_pos["value"] = inside_global
    window._poll_cursor_idle_state(now_ms=1500)

    assert window._video_pointer_inside is True
    assert window._cursor_hide_timer.isActive() is True
    assert cursor_autohide_calls[-1] == 2000


def test_player_window_exit_fullscreen_restores_maximized_state(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.showMaximized()
    qtbot.waitUntil(window.isMaximized)

    window.toggle_fullscreen()
    assert window.isFullScreen() is True

    window.toggle_fullscreen()

    assert window.isFullScreen() is False
    assert window.isMaximized() is True


def test_player_window_control_buttons_drive_video_actions(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.pause_calls = 0
            self.resume_calls = 0
            self.toggle_mute_calls = 0
            self.seek_relative_calls: list[int] = []
            self.set_volume_calls: list[int] = []

        def pause(self) -> None:
            self.pause_calls += 1

        def resume(self) -> None:
            self.resume_calls += 1

        def toggle_mute(self) -> None:
            self.toggle_mute_calls += 1

        def seek_relative(self, seconds: int) -> None:
            self.seek_relative_calls.append(seconds)

        def set_volume(self, value: int) -> None:
            self.set_volume_calls.append(value)

        def set_speed(self, value: float) -> None:
            self.speed = value

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.backward_button.click()
    window.forward_button.click()
    window.mute_button.click()
    window.volume_slider.setValue(35)
    window.speed_combo.setCurrentText("1.5x")

    assert window.video.seek_relative_calls == [-15, 15]
    assert window.video.toggle_mute_calls == 1
    assert window.video.set_volume_calls[-1] == 35
    assert window.current_speed == 1.5


def test_player_window_wide_button_hides_sidebar(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    window.wide_button.click()
    assert window.sidebar_container.isHidden() is True

    window.wide_button.click()
    assert window.sidebar_container.isHidden() is False


def test_player_window_persists_pre_wide_splitter_state_when_saved_in_wide_mode(qtbot) -> None:
    config = AppConfig()
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.show()
    window.main_splitter.setSizes([900, 300])

    expected_sizes = window.main_splitter.sizes()
    expected_ratio = expected_sizes[0] / sum(expected_sizes)

    window.wide_button.click()
    window._persist_geometry()

    restored = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(restored)
    restored.show()
    restored_sizes = restored.main_splitter.sizes()
    restored_ratio = restored_sizes[0] / sum(restored_sizes)

    assert restored.sidebar_container.isHidden() is False
    assert restored_sizes[1] > 0
    assert abs(restored_ratio - expected_ratio) < 0.02


def test_player_window_persists_and_restores_main_splitter_state(qtbot) -> None:
    saved = {"called": 0}
    config = AppConfig()
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: saved.__setitem__("called", saved["called"] + 1))
    qtbot.addWidget(window)
    window.show()
    window.main_splitter.setSizes([900, 300])

    window._persist_geometry()

    assert config.player_main_splitter_state is not None
    assert saved["called"] >= 1

    restored = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(restored)
    restored.show()

    assert restored.main_splitter.saveState() == QByteArray(config.player_main_splitter_state)


def test_player_window_return_to_main_hides_window_and_stops_video_backend(qtbot, monkeypatch) -> None:
    emitted = {"count": 0}
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    pauses = {"count": 0}
    shutdowns = {"count": 0}

    class FakeVideo:
        def pause(self) -> None:
            pauses["count"] += 1

    window.session = object()
    window.video = FakeVideo()
    monkeypatch.setattr(window.video_widget, "shutdown", lambda: shutdowns.__setitem__("count", shutdowns["count"] + 1))
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))
    window.show()
    window._return_to_main()

    assert emitted["count"] == 1
    assert window.isHidden() is True
    assert window.session is not None
    assert config.last_active_window == "main"
    assert pauses["count"] == 1
    assert shutdowns["count"] == 1


def test_player_window_ctrl_q_quits_application(qtbot, monkeypatch) -> None:
    called = {"count": 0}
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)

    monkeypatch.setattr(QApplication, "quit", lambda *args, **kwargs: called.__setitem__("count", called["count"] + 1))

    window._quit_application()

    assert called["count"] == 1
    assert config.last_active_window == "player"


def test_player_window_quit_application_reports_progress_and_stops_current_playback(qtbot, monkeypatch) -> None:
    called = {"count": 0}
    controller = RecordingPlayerController()
    window = PlayerWindow(controller, config=AppConfig(last_active_window="player"), save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1))
    controller.progress_calls.clear()
    controller.stop_calls.clear()

    monkeypatch.setattr(QApplication, "quit", lambda *args, **kwargs: called.__setitem__("count", called["count"] + 1))

    window._quit_application()

    assert called["count"] == 1
    qtbot.waitUntil(
        lambda: controller.progress_calls == [(1, 30, 1.0, 0, 0, False)] and controller.stop_calls == [1]
    )
    assert controller.progress_calls == [(1, 30, 1.0, 0, 0, False)]
    assert controller.stop_calls == [1]


def test_player_window_quit_application_reports_progress_and_stop_without_blocking_ui(qtbot, monkeypatch) -> None:
    class SlowRecordingPlayerController(RecordingPlayerController):
        def report_progress(
            self,
            session,
            current_index: int,
            position_seconds: int,
            speed: float,
            opening_seconds: int,
            ending_seconds: int,
            paused: bool,
            force_remote_report: bool = False,
        ) -> None:
            time.sleep(0.15)
            super().report_progress(
                session,
                current_index,
                position_seconds,
                speed,
                opening_seconds,
                ending_seconds,
                paused,
                force_remote_report,
            )

        def stop_playback(self, session, current_index: int) -> None:
            time.sleep(0.15)
            super().stop_playback(session, current_index)

    called = {"count": 0}
    controller = SlowRecordingPlayerController()
    window = PlayerWindow(controller, config=AppConfig(last_active_window="player"), save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1))
    controller.progress_calls.clear()
    controller.stop_calls.clear()

    monkeypatch.setattr(QApplication, "quit", lambda *args, **kwargs: called.__setitem__("count", called["count"] + 1))

    started_at = time.perf_counter()
    window._quit_application()
    elapsed_seconds = time.perf_counter() - started_at

    assert elapsed_seconds < 0.1
    assert called["count"] == 1
    qtbot.waitUntil(
        lambda: controller.progress_calls == [(1, 30, 1.0, 0, 0, False)] and controller.stop_calls == [1]
    )


def test_player_window_periodic_progress_does_not_force_remote_report(qtbot) -> None:
    controller = RecordingPlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1), start_paused=True)
    controller.progress_calls.clear()
    controller.force_remote_report_calls.clear()

    window.report_progress()

    qtbot.waitUntil(lambda: controller.progress_calls == [(1, 30, 1.0, 0, 0, True)])
    assert controller.progress_calls == [(1, 30, 1.0, 0, 0, True)]
    assert controller.force_remote_report_calls == [False]


def test_player_window_quit_application_preserves_current_paused_state(qtbot, monkeypatch) -> None:
    config = AppConfig(last_active_window="player", last_player_paused=False)
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.is_playing = False

    monkeypatch.setattr(QApplication, "quit", lambda *args, **kwargs: None)

    window._quit_application()

    assert config.last_player_paused is True


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


def test_player_window_f1_opens_shortcut_help_dialog(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session())
    window.show()
    window.activateWindow()
    window.setFocus()

    send_key(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    rows = shortcut_table_rows(visible_shortcut_help_dialogs()[0])

    assert ("F1", "打开快捷键帮助") in rows
    assert ("Space", "播放/暂停") in rows
    assert ("Left", "后退 15 秒") in rows
    assert ("Ctrl+Right", "前进 60 秒") in rows
    assert ("M", "静音") in rows
    assert ("Enter", "切换全屏") in rows


def test_player_window_reuses_existing_shortcut_help_dialog(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session())
    window.show()
    window.activateWindow()
    window.setFocus()

    send_key(window, Qt.Key.Key_F1)
    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    first_dialog = visible_shortcut_help_dialogs()[0]

    send_key(window, Qt.Key.Key_F1)

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)
    assert visible_shortcut_help_dialogs()[0] is first_dialog


def test_player_window_return_to_main_closes_shortcut_help_dialog(qtbot) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig(last_active_window="player"), save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session())
    window.show()

    send_key(window, Qt.Key.Key_F1)
    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 1)

    window._return_to_main()

    qtbot.waitUntil(lambda: len(visible_shortcut_help_dialogs()) == 0, timeout=1000)
    assert window.help_dialog is None


def test_player_window_return_to_main_closes_video_context_menu(qtbot) -> None:
    class FakeMenu(QObject):
        aboutToHide = Signal()

        def __init__(self) -> None:
            super().__init__()
            self.visible = True
            self.hide_calls = 0

        def isVisible(self) -> bool:
            return self.visible

        def hide(self) -> None:
            self.hide_calls += 1
            self.visible = False
            self.aboutToHide.emit()

        def deleteLater(self) -> None:
            return None

    window = PlayerWindow(FakePlayerController(), config=AppConfig(last_active_window="player"), save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session())
    fake_menu = FakeMenu()
    window._video_context_menu = fake_menu

    window._return_to_main()

    assert fake_menu.hide_calls == 1
    assert window._video_context_menu is None


def test_player_window_keyboard_shortcuts_control_playback_navigation_and_view(qtbot) -> None:
    controller = RecordingPlayerController()
    video = RecordingVideo()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session())
    window.show()
    window.activateWindow()
    window.setFocus()

    send_key(window, Qt.Key.Key_Space, text=" ")
    assert video.pause_calls == 1
    assert window.is_playing is False

    send_key(window, Qt.Key.Key_Space, text=" ")
    assert video.resume_calls == 1
    assert window.is_playing is True

    send_key(window, Qt.Key.Key_Return, text="\r")
    assert window.isFullScreen() is True

    send_key(window, Qt.Key.Key_Escape)
    assert window.isFullScreen() is False

    send_key(window, Qt.Key.Key_M, text="m")
    assert video.toggle_mute_calls == 1

    send_key(window, Qt.Key.Key_Minus, text="-")
    assert window.current_speed == 0.75

    send_key(window, Qt.Key.Key_Equal, Qt.KeyboardModifier.ShiftModifier, text="+")
    assert window.current_speed == 1.0

    send_key(window, Qt.Key.Key_Equal, text="=")
    assert window.current_speed == 1.0

    send_key(window, Qt.Key.Key_Down)
    assert window.volume_slider.value() == 95

    send_key(window, Qt.Key.Key_Up)
    assert window.volume_slider.value() == 100

    send_key(window, Qt.Key.Key_Left)
    send_key(window, Qt.Key.Key_Right)
    send_key(window, Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier)
    send_key(window, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
    assert video.seek_relative_calls == [-15, 15, -60, 60]

    send_key(window, Qt.Key.Key_PageUp)
    assert window.current_index == 0
    assert window.playlist.currentRow() == 0

    send_key(window, Qt.Key.Key_PageDown)
    assert window.current_index == 1
    assert window.playlist.currentRow() == 1
    qtbot.waitUntil(lambda: controller.progress_calls == [(1, 30, 1.0, 0, 0, False), (0, 30, 1.0, 0, 0, False)])
    assert controller.progress_calls == [(1, 30, 1.0, 0, 0, False), (0, 30, 1.0, 0, 0, False)]


def test_player_window_toggle_playback_persists_last_player_paused(qtbot) -> None:
    config = AppConfig(last_player_paused=False)
    saved = {"count": 0}
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.toggle_playback()

    assert config.last_player_paused is True

    window.toggle_playback()

    assert config.last_player_paused is False
    assert saved["count"] >= 2


def test_player_window_pausing_playback_restores_application_title(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1))

    window.toggle_playback()

    assert window.is_playing is False
    assert window.windowTitle() == "alist-tvbox 播放器"


def test_player_window_opening_session_paused_keeps_application_title(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(make_player_session(start_index=1), start_paused=True)

    assert window.is_playing is False
    assert window.windowTitle() == "alist-tvbox 播放器"


def test_player_window_play_next_updates_window_title_to_new_item(qtbot) -> None:
    controller = RecordingPlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=0))

    window.play_next()

    assert window.current_index == 1
    assert window.windowTitle() == "Movie - Episode 2"


def test_player_window_escape_shortcut_returns_to_main_when_not_fullscreen(qtbot) -> None:
    emitted = {"count": 0}
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    pauses = {"count": 0}

    class FakeVideo:
        def pause(self) -> None:
            pauses["count"] += 1

    window.video = FakeVideo()
    window.session = object()
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))
    window.show()
    window.activateWindow()
    window.setFocus()

    send_key(window, Qt.Key.Key_Escape)

    assert window.isHidden() is True
    assert emitted["count"] == 1
    assert pauses["count"] == 1
    assert config.last_active_window == "main"


def test_player_window_return_to_main_persists_paused_restore_state(qtbot) -> None:
    config = AppConfig(last_active_window="player", last_player_paused=False)
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)

    class FakeVideo:
        def pause(self) -> None:
            return None

    window.session = object()
    window.video = FakeVideo()
    window._return_to_main()

    assert config.last_player_paused is True


def test_player_window_return_to_main_reports_current_progress_and_stops_current_playback(qtbot) -> None:
    controller = RecordingPlayerController()
    window = PlayerWindow(controller, config=AppConfig(last_active_window="player"), save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1))
    controller.progress_calls.clear()
    controller.stop_calls.clear()

    window._return_to_main()

    qtbot.waitUntil(
        lambda: controller.progress_calls == [(1, 30, 1.0, 0, 0, True)] and controller.stop_calls == [1]
    )
    assert controller.progress_calls == [(1, 30, 1.0, 0, 0, True)]
    assert controller.force_remote_report_calls == [True]
    assert controller.stop_calls == [1]


def test_player_window_return_to_main_restores_application_title(qtbot) -> None:
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=0))

    window._return_to_main()

    assert window.windowTitle() == "alist-tvbox 播放器"


def test_player_window_resume_from_main_reloads_current_item_and_updates_state(qtbot) -> None:
    config = AppConfig(last_active_window="main", last_player_paused=True)
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    video = RecordingVideo()
    window.video = video
    window.open_session(make_player_session(start_index=0))

    window._return_to_main()
    window.resume_from_main()

    assert video.pause_calls == 1
    assert video.resume_calls == 0
    assert video.load_calls == [("http://m/1.m3u8", 0), ("http://m/1.m3u8", 30)]
    assert window.is_playing is True
    assert window.windowTitle() == "Movie - Episode 1"
    assert config.last_player_paused is False


def test_player_window_close_clears_session_for_future_restore(qtbot) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig(last_active_window="player"), save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1))

    window.close()

    assert window.session is None
