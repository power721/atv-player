import re
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QContextMenuEvent, QCursor, QIcon, QImage, QKeyEvent, QKeySequence, QMouseEvent, QPixmap, QWindow
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QDoubleSpinBox, QLabel, QMenu, QPushButton, QSpinBox, QStyle, QStyleOptionComboBox, QTableWidget, QToolButton, QWidget
from PySide6.QtWidgets import QSplitter, QToolTip
from atv_player.controllers.player_controller import PlayerController, PlayerSession
from atv_player.danmaku.models import DanmakuSourceGroup, DanmakuSourceOption, DanmakuSourceSearchResult
from atv_player.metadata.cache import MetadataCache
from atv_player.metadata.hydrator import MetadataHydrator
from atv_player.metadata.merge import merge_metadata_record
from atv_player.metadata.models import MetadataContext, MetadataMatch, MetadataRecord
from atv_player.metadata.scrape import MetadataScrapeCandidate, MetadataScrapeGroup, MetadataScrapeService
from atv_player.models import (
    AppConfig,
    ExternalSubtitleOption,
    ExternalSubtitleSelection,
    HistoryRecord,
    PlaybackSource,
    PlaybackSourceGroup,
    PlayItem,
    PlaybackDetailAction,
    PlaybackDetailFieldAction,
    PlaybackDetailField,
    PlaybackDetailValuePart,
    PlaybackLoadResult,
    VideoQualityOption,
    VodItem,
    YtdlpAudioTrackOption,
)
from atv_player.plugins.controller import SpiderPluginController
from atv_player.player.mpv_widget import AudioTrack, Chapter, SubtitleTrack

import atv_player.danmaku.cache as danmaku_cache_module
import atv_player.metadata.dialog_cache as metadata_dialog_cache_module
import atv_player.plugins.controller as spider_controller_module
import atv_player.ui.poster_loader as poster_loader_module
import atv_player.ui.player_window as player_window_module
from atv_player.ui.player_window import ClickableSlider, PlayerWindow


def assert_timestamped_log_line(line: str, message: str) -> None:
    pattern = rf"\[\d{{2}}:\d{{2}}:\d{{2}}\.\d{{3}}\] {re.escape(message)}"
    assert re.fullmatch(pattern, line), line


def _player_window_is_always_on_top(window: PlayerWindow) -> bool:
    return window._is_always_on_top()


def _spin_until(predicate, timeout_seconds: float = 5.0) -> None:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@pytest.fixture(autouse=True)
def prevent_real_mpv_load(monkeypatch: pytest.MonkeyPatch) -> None:
    # PlayerWindow UI tests validate window behavior, not libmpv integration.
    # Keep accidental session opens from creating native mpv cores that can
    # leak threads across tests and segfault the Python process.
    monkeypatch.setattr("atv_player.player.mpv_widget.MpvWidget.load", lambda self, *args, **kwargs: None)


@pytest.fixture(autouse=True)
def isolate_player_window_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(player_window_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(metadata_dialog_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")


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
        duration_seconds: int = 0,
    ) -> None:
        return None

    def resolve_play_item_detail(self, session, play_item):
        return None

    def stop_playback(self, session, current_index: int) -> None:
        return None

    def on_item_started(self, session, current_index: int) -> None:
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
        duration_seconds: int = 0,
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


class PrefetchResetRecordingPlayerController(RecordingPlayerController):
    def __init__(self) -> None:
        super().__init__()
        self.reset_calls: list[PlayerSession] = []

    def reset_next_episode_danmaku_prefetch_state(self, session: PlayerSession) -> None:
        self.reset_calls.append(session)


class FakeHeatController:
    def __init__(self, summary_text: str = "23 人正在播放") -> None:
        self.summary_text = summary_text
        self.media_heat_calls: list[str] = []
        self.effective_watch_calls: list[dict[str, object]] = []

    def load_media_heat(self, media_key: str):
        self.media_heat_calls.append(media_key)
        return type(
            "HeatSummary",
            (),
            {
                "media_key": media_key,
                "best_display_text": lambda _self: self.summary_text,
            },
        )()

    def maybe_record_effective_watch(self, media, **kwargs) -> bool:
        self.effective_watch_calls.append({"media": media, **kwargs})
        return True


class RecordingVideo:
    def __init__(self) -> None:
        self.load_calls: list[tuple[str, int]] = []
        self.pause_calls = 0
        self.resume_calls = 0
        self.toggle_mute_calls = 0
        self.toggle_video_info_calls = 0
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

    def toggle_video_info(self) -> None:
        self.toggle_video_info_calls += 1

    def seek_relative(self, seconds: int) -> None:
        self.seek_relative_calls.append(seconds)

    def position_seconds(self) -> int:
        return 30

    def duration_seconds(self) -> int:
        return 120


class DetailFieldPayloadSpider:
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "红果短剧",
                    "vod_play_from": "默认线",
                    "vod_play_url": "第1集$/play/1#第2集$/play/2",
                    "ext": [
                        {"label": "播放", "value": "12万"},
                        {"label": "更新", "value": "2026-05-08"},
                    ],
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        payload = {
            "parse": 0,
            "url": f"https://stream.example{id}.m3u8",
        }
        if id == "/play/1":
            payload["ext"] = [
                {"label": "播放", "value": "18万"},
                {"label": "热度", "value": "95"},
            ]
        return payload


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


def make_bilibili_grouped_session() -> PlayerSession:
    main_group = [PlayItem(title="正片", url="http://bili/main.mp4", vod_id="BV-main", play_source="BiliBili")]
    related_group = [
        PlayItem(title="相关1", url="http://bili/r1.mp4", vod_id="BV-r1", play_source="相关视频"),
        PlayItem(title="相关2", url="http://bili/r2.mp4", vod_id="BV-r2", play_source="相关视频"),
    ]
    up_group = [PlayItem(title="UP主1", url="http://bili/u1.mp4", vod_id="BV-u1", play_source="UP主视频")]
    return PlayerSession(
        vod=VodItem(vod_id="BV-main", vod_name="B站视频"),
        playlist=main_group,
        playlists=[main_group, related_group, up_group],
        playlist_index=0,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        source_kind="bilibili",
    )


def make_sortable_player_session() -> PlayerSession:
    return PlayerSession(
        vod=VodItem(vod_id="series", vod_name="Series"),
        playlist=[
            PlayItem(
                title="第10集",
                original_title="Episode 10.mkv",
                url="10",
                size=100,
            ),
            PlayItem(
                title="第2集",
                original_title="Episode 2.mkv",
                url="2",
                size=20,
            ),
            PlayItem(
                title="第1集",
                original_title="Episode 1.mkv",
                url="1",
                size=10,
            ),
        ],
        start_index=1,
        start_position_seconds=0,
        speed=1.0,
    )


def test_player_window_playlist_sort_combo_uses_available_fields(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(make_sortable_player_session())

    assert not window.playlist_sort_combo.isHidden()
    assert [
        window.playlist_sort_combo.itemData(index)
        for index in range(window.playlist_sort_combo.count())
    ] == ["index", "name,asc", "name,desc", "size,asc", "size,desc"]


def test_player_window_playlist_sort_keeps_current_item_and_does_not_reload(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    video = RecordingVideo()
    window.video = video
    window.open_session(make_sortable_player_session())
    current = window.session.playlist[window.current_index]
    load_count = len(video.load_calls)

    window.playlist_sort_combo.setCurrentIndex(
        window.playlist_sort_combo.findData("name,asc")
    )

    assert [item.url for item in window.session.playlist] == ["1", "2", "10"]
    assert window.session.playlist[window.current_index] is current
    assert window.playlist.currentRow() == window.current_index
    assert len(video.load_calls) == load_count

    window.playlist_sort_combo.setCurrentIndex(
        window.playlist_sort_combo.findData("index")
    )
    assert [item.url for item in window.session.playlist] == ["10", "2", "1"]
    assert window.session.playlist[window.current_index] is current


def test_player_window_next_uses_sorted_playlist_order(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(make_sortable_player_session())
    window.playlist_sort_combo.setCurrentIndex(
        window.playlist_sort_combo.findData("name,asc")
    )
    played: list[int] = []
    monkeypatch.setattr(
        window,
        "_play_item_at_index",
        lambda index, **_kwargs: played.append(index),
    )

    window.play_next()

    assert played == [2]
    assert window.session.playlist[played[0]].url == "10"


def test_player_window_hides_playlist_sort_without_metadata_and_resets_new_session(
    qtbot,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(make_sortable_player_session())
    window.playlist_sort_combo.setCurrentIndex(
        window.playlist_sort_combo.findData("name,desc")
    )

    window.open_session(make_player_session(start_index=0))

    assert window.playlist_sort_combo.currentData() == "index"
    assert window.playlist_sort_combo.isHidden()


def test_player_window_playlist_sort_follows_source_and_preserves_target_episode(
    qtbot,
    monkeypatch,
) -> None:
    first = [
        PlayItem(title="A2", original_title="Episode 2.mkv", url="a2"),
        PlayItem(title="A1", original_title="Episode 1.mkv", url="a1"),
    ]
    second = [
        PlayItem(title="B2", original_title="Episode 2.mkv", url="b2"),
        PlayItem(title="B1", original_title="Episode 1.mkv", url="b1"),
    ]
    session = PlayerSession(
        vod=VodItem(vod_id="series", vod_name="Series"),
        playlist=first,
        playlists=[first, second],
        source_groups=[
            PlaybackSourceGroup(
                label="线路",
                sources=[
                    PlaybackSource(label="线路1", playlist=first),
                    PlaybackSource(label="线路2", playlist=second),
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window.playlist_sort_combo.setCurrentIndex(
        window.playlist_sort_combo.findData("name,asc")
    )
    monkeypatch.setattr(window, "_load_current_item", lambda **_kwargs: None)

    window._switch_active_source(0, 1)

    assert [item.url for item in window.session.playlist] == ["b1", "b2"]
    assert window.session.playlist[window.current_index].url == "b2"


def test_player_window_playlist_sort_falls_back_when_target_source_lacks_field(
    qtbot,
    monkeypatch,
) -> None:
    first = [PlayItem(title="A", original_title="A.mkv", url="a")]
    second = [PlayItem(title="B", url="b")]
    session = PlayerSession(
        vod=VodItem(vod_id="series", vod_name="Series"),
        playlist=first,
        playlists=[first, second],
        source_groups=[
            PlaybackSourceGroup(
                label="线路",
                sources=[
                    PlaybackSource(label="线路1", playlist=first),
                    PlaybackSource(label="线路2", playlist=second),
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window.playlist_sort_combo.setCurrentIndex(
        window.playlist_sort_combo.findData("name,desc")
    )
    monkeypatch.setattr(window, "_load_current_item", lambda **_kwargs: None)

    window._switch_active_source(0, 1)

    assert window._playlist_sort_state.mode == "index"
    assert window.playlist_sort_combo.isHidden()


def test_player_window_playlist_sort_applies_to_replacement_and_keeps_requested_item(
    qtbot,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(make_sortable_player_session())
    window.playlist_sort_combo.setCurrentIndex(
        window.playlist_sort_combo.findData("name,asc")
    )
    replacement = [
        PlayItem(title="第3集", original_title="Episode 3.mkv", url="3"),
        PlayItem(title="第1集", original_title="Episode 1.mkv", url="1-new"),
    ]

    window._apply_playback_loader_result(
        PlaybackLoadResult(
            replacement_playlist=replacement,
            replacement_start_index=0,
        )
    )

    assert [item.url for item in window.session.playlist] == ["1-new", "3"]
    assert window.session.playlist[window.current_index].url == "3"


def test_player_window_playlist_sort_survives_episode_title_enhancement(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    session = make_sortable_player_session()
    window.open_session(session)
    window.playlist_sort_combo.setCurrentIndex(
        window.playlist_sort_combo.findData("name,asc")
    )
    current = window.session.playlist[window.current_index]
    window._pending_episode_title_session = session
    window._episode_title_request_id = 7
    updated = [
        PlayItem(
            title="第10集",
            original_title="Episode 10.mkv",
            episode_display_title="新标题10",
            url="10",
        ),
        PlayItem(
            title="第1集",
            original_title="Episode 1.mkv",
            episode_display_title="新标题1",
            url="1",
        ),
        PlayItem(
            title="第2集",
            original_title="Episode 2.mkv",
            episode_display_title="新标题2",
            url="2",
        ),
    ]

    window._handle_episode_title_enhancement_succeeded(7, updated)

    assert [item.original_title for item in window.session.playlist] == [
        "Episode 1.mkv",
        "Episode 2.mkv",
        "Episode 10.mkv",
    ]
    assert window.session.playlist[window.current_index] is current

    window.playlist_sort_combo.setCurrentIndex(
        window.playlist_sort_combo.findData("index")
    )
    assert [item.original_title for item in window.session.playlist] == [
        "Episode 10.mkv",
        "Episode 1.mkv",
        "Episode 2.mkv",
    ]


def test_player_window_hides_playlist_sort_in_bilibili_tree_mode(qtbot) -> None:
    config = AppConfig(bilibili_grouped_playlist_tree_enabled=True)
    session = make_bilibili_grouped_session()
    for group in session.playlists:
        for index, item in enumerate(group):
            item.original_title = f"Episode {index + 1}.mp4"
    window = PlayerWindow(FakePlayerController(), config=config)
    qtbot.addWidget(window)

    window.open_session(session)

    assert window._bilibili_grouped_playlist_tree_enabled()
    assert window.playlist_sort_combo.isHidden()


def test_player_window_renders_heat_summary_in_detail_fields(qtbot) -> None:
    heat = FakeHeatController("23 人正在播放")
    session = make_player_session(start_index=0)
    session.vod = VodItem(vod_id="movie-1", vod_name="Movie")
    window = PlayerWindow(FakePlayerController(), heat_controller=heat)
    qtbot.addWidget(window)

    window.open_session(session)

    qtbot.waitUntil(
        lambda: "23 人正在播放"
        in "\n".join(label.text() for label in window.detail_fields_widget.findChildren(QLabel))
    )
    assert heat.media_heat_calls == ["title:movie"]


def test_player_window_reports_effective_watch_to_heat_controller(qtbot) -> None:
    heat = FakeHeatController()
    controller = RecordingPlayerController()
    window = PlayerWindow(controller, heat_controller=heat)
    qtbot.addWidget(window)
    session = make_player_session(start_index=0)
    session.vod = VodItem(
        vod_id="movie-1",
        vod_name="Movie",
        detail_fields=[PlaybackDetailField(label="TMDB ID", value="123")],
    )
    window.open_session(session)
    window.video = type(
        "Video",
        (),
        {
            "position_seconds": lambda _self: 60,
            "duration_seconds": lambda _self: 120,
        },
    )()

    window.report_progress()

    assert len(heat.effective_watch_calls) == 1
    call = heat.effective_watch_calls[0]
    assert call["media"].title == "Movie"
    assert call["position_seconds"] == 60
    assert call["duration_seconds"] == 120
    assert call["episode_index"] == 0
    assert call["episode_number"] == 0


def test_player_window_reports_effective_watch_episode_number(qtbot) -> None:
    heat = FakeHeatController()
    controller = RecordingPlayerController()
    window = PlayerWindow(controller, heat_controller=heat)
    qtbot.addWidget(window)
    session = make_player_session(start_index=2)
    session.vod = VodItem(
        vod_id="movie-1",
        vod_name="Movie",
        type_name="剧集",
        detail_fields=[PlaybackDetailField(label="TMDB ID", value="123")],
    )
    window.open_session(session)
    window.video = type(
        "Video",
        (),
        {
            "position_seconds": lambda _self: 60,
            "duration_seconds": lambda _self: 120,
        },
    )()

    window.report_progress()

    assert len(heat.effective_watch_calls) == 1
    call = heat.effective_watch_calls[0]
    assert call["episode_index"] == 2
    assert call["episode_number"] == 3


def test_player_window_skips_effective_watch_without_external_media_id(qtbot) -> None:
    heat = FakeHeatController()
    controller = RecordingPlayerController()
    window = PlayerWindow(controller, heat_controller=heat)
    qtbot.addWidget(window)
    session = make_player_session(start_index=0)
    session.vod = VodItem(vod_id="movie-1", vod_name="Movie")
    window.open_session(session)
    window.video = type(
        "Video",
        (),
        {
            "position_seconds": lambda _self: 60,
            "duration_seconds": lambda _self: 120,
        },
    )()

    window.report_progress()

    assert heat.effective_watch_calls == []


def test_player_window_details_panel_uses_global_light_theme_tokens(qtbot) -> None:
    from atv_player.ui.theme import ThemeManager, install_theme

    app = QApplication.instance() or QApplication([])
    manager = ThemeManager(system_theme_getter=lambda: "light")
    install_theme(app, manager, "light")

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    tokens = manager.tokens_for("light")
    assert tokens.panel_bg in window.details.styleSheet()


def test_player_window_immersive_controls_remain_dark_in_light_theme(qtbot) -> None:
    from atv_player.ui.theme import ThemeManager, install_theme

    app = QApplication.instance() or QApplication([])
    manager = ThemeManager(system_theme_getter=lambda: "light")
    install_theme(app, manager, "light")

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    player_tokens = manager.player_tokens_for("light")
    assert player_tokens.player_controls_bg in window.bottom_area.styleSheet()


def test_player_window_can_open_placeholder_session_without_playlist(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="占位电影", vod_pic="poster-card"),
        playlist=[],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        initial_log_message="正在加载详情...",
        is_placeholder=True,
    )

    window.open_session(session)
    assert window.session is session
    assert window.playlist.count() == 0
    assert window.metadata_view.toPlainText().startswith("名称: 占位电影")
    assert_timestamped_log_line(window.log_view.toPlainText(), "正在加载详情...")
    assert window.video.load_calls == []


def test_player_window_does_not_sync_refresh_track_state_immediately_for_embedded_mpv(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window.current_index = 0

    calls: list[str] = []

    monkeypatch.setattr(window, "_video_load", lambda *args, **kwargs: None)
    monkeypatch.setattr(window, "_configure_video_surface_widgets", lambda: None)
    monkeypatch.setattr(window.video, "set_speed", lambda value: None)
    monkeypatch.setattr(window.video, "set_volume", lambda value: None)
    monkeypatch.setattr(window, "_apply_muted_state", lambda: None)
    monkeypatch.setattr(window, "_refresh_subtitle_state", lambda: calls.append("refresh-subtitle"))
    monkeypatch.setattr(window, "_schedule_followup_subtitle_refresh_if_needed", lambda current_item: None)
    monkeypatch.setattr(window, "_refresh_audio_state", lambda: calls.append("refresh-audio"))
    monkeypatch.setattr(window, "_refresh_video_quality_state", lambda: None)
    monkeypatch.setattr(window, "_configure_danmaku_for_current_item", lambda: None)
    monkeypatch.setattr(window, "_reset_subtitle_combo", lambda: calls.append("reset-subtitle"))
    monkeypatch.setattr(window, "_reset_audio_combo", lambda: calls.append("reset-audio"))

    window._start_current_item_playback()

    assert calls == ["reset-subtitle", "reset-audio"]


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
    assert window.bottom_area.maximumHeight() == 88
    assert window.bottom_layout.spacing() == 4
    assert window.opening_spin.prefix() == "片头 "
    assert window.ending_spin.prefix() == "片尾 "
    assert window.opening_spin.width() == 105
    assert window.ending_spin.width() == 105
    assert "border: 1px solid" in window.opening_spin.styleSheet()
    assert "border: 1px solid" in window.ending_spin.styleSheet()


def test_player_window_hides_disabled_control_combos_when_width_is_tight(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())

    qtbot.addWidget(window)
    window.show()
    window.resize(1000, 700)

    assert window.subtitle_combo.isEnabled() is False
    assert window.subtitle_combo.isHidden() is True
    assert window.danmaku_combo.isEnabled() is False
    assert window.danmaku_combo.isHidden() is True
    assert window.video_quality_combo.isEnabled() is False
    assert window.video_quality_combo.isHidden() is True
    assert window.audio_combo.isEnabled() is False
    assert window.audio_combo.isHidden() is True
    assert window.parse_combo.isEnabled() is False
    assert window.parse_combo.isHidden() is True
    assert window.speed_combo.isHidden() is False


def test_player_window_keeps_enabled_control_combos_visible_when_width_is_tight(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())

    qtbot.addWidget(window)
    window.danmaku_combo.setEnabled(True)
    window.show()
    window.resize(1000, 700)

    assert window.danmaku_combo.isHidden() is False
    assert window.subtitle_combo.isHidden() is True


def test_player_window_volume_label_shows_current_volume(qtbot) -> None:
    config = AppConfig(player_volume=35)
    window = PlayerWindow(FakePlayerController(), config=config)

    qtbot.addWidget(window)

    assert window.volume_value_label.text() == "35%"

    window.volume_slider.setValue(72)

    assert window.volume_value_label.text() == "72%"


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

    assert window.danmaku_source_button.toolTip() == "弹幕源 (D)"
    assert window.danmaku_source_button.isEnabled() is True


def test_player_window_shows_danmaku_settings_button(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.danmaku_settings_button.toolTip() == "弹幕设置 (Ctrl+D)"
    assert window.danmaku_settings_button.isEnabled() is True
    assert (
        window.danmaku_settings_button.icon().pixmap(24, 24).toImage()
        != window.danmaku_source_button.icon().pixmap(24, 24).toImage()
    )


def test_player_window_shows_metadata_scrape_button_with_search_icon(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.metadata_scrape_button.toolTip() == "刮削 (S)"
    assert window.metadata_scrape_button.isEnabled() is True


def test_player_window_places_metadata_scrape_button_in_playback_controls(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.metadata_scrape_button.parentWidget() is window.danmaku_source_button.parentWidget()
    assert window.metadata_scrape_button.parentWidget() is not window.sidebar_actions_widget


def test_player_window_uses_dedicated_metadata_scrape_icon(qtbot, monkeypatch) -> None:
    calls: list[str] = []

    def fake_load_icon(path) -> QIcon:
        calls.append(str(path))
        return QIcon()

    monkeypatch.setattr(player_window_module, "load_icon", fake_load_icon)

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert str(window._icons_dir / "scrape.svg") in calls


def test_player_window_metadata_scrape_dialog_prefills_title_year_and_provider(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=object(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    dialog = window._ensure_metadata_scrape_dialog()
    window._open_metadata_scrape_dialog()

    assert dialog.windowTitle() == "刮削"
    assert window._metadata_scrape_title_edit.text() == "深空彼岸"
    assert window._metadata_scrape_year_edit.text() == "2026"
    assert window._metadata_scrape_category_combo.currentData() == ""
    assert window._metadata_scrape_provider_combo.currentData() == ""


def test_player_window_metadata_scrape_dialog_prefills_current_media_title(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="我的王室死对头(2026)", vod_year="2026"),
        playlist=[PlayItem(title="正片", url="https://media.example/1.mp4", media_title="我的王室死对头(2026)")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=object(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()

    assert window._metadata_scrape_title_edit.text() == "我的王室死对头"
    assert window._metadata_scrape_year_edit.text() == "2026"


def test_player_window_metadata_scrape_dialog_normalizes_leading_topic_marker_in_title(qtbot) -> None:
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="# 牧神记 年番2", vod_year="2024"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()

    assert window._metadata_scrape_title_edit.text() == "牧神记 年番2"

    window._rerun_metadata_scrape_search()

    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)
    assert service.search_calls == [("牧神记 年番2", "2024", "")]


def test_player_window_metadata_scrape_dialog_strips_trailing_noise_but_keeps_separate_year(qtbot) -> None:
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(
            vod_id="v1",
            vod_name="主角 (2026)剧情 张嘉益 刘浩存 4KHDR60FPS 更新17集",
            vod_year="2026",
        ),
        playlist=[PlayItem(title="正片", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()

    assert window._metadata_scrape_title_edit.text() == "主角"

    window._rerun_metadata_scrape_search()

    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)
    assert service.search_calls == [("主角", "2026", "")]


def test_player_window_metadata_scrape_dialog_strips_bracketed_noise_and_uses_separate_year(qtbot) -> None:
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(
            vod_id="v1",
            vod_name="牧神记(2026)【更83集】【4K.高码率】【内嵌简中】【动画/奇幻/冒险】",
            vod_year="2026",
            category_name="动漫",
            type_name="动画",
        ),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()

    assert window._metadata_scrape_title_edit.text() == "牧神记"

    window._rerun_metadata_scrape_search()

    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)
    assert service.search_calls == [("牧神记", "2026", "")]


def test_player_window_metadata_scrape_dialog_prefers_embedded_title_year_over_conflicting_vod_year(qtbot) -> None:
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(
            vod_id="v1",
            vod_name="西游记 (1986) 4K 2025年重新深度修复4K",
            vod_year="2025",
        ),
        playlist=[PlayItem(title="正片", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()

    assert window._metadata_scrape_title_edit.text() == "西游记"
    assert window._metadata_scrape_year_edit.text() == "1986"

    window._rerun_metadata_scrape_search()

    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)
    assert service.search_calls == [("西游记", "1986", "")]


def test_player_window_metadata_scrape_search_passes_category_and_type_into_query(qtbot) -> None:
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="牧神记", vod_year="2024", category_name="动漫", type_name="动画"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()

    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)
    assert service.search_queries == [("动漫", "动画")]


def test_player_window_metadata_scrape_search_uses_selected_category_override(qtbot) -> None:
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="流浪地球", vod_year="2019", category_name="剧集", type_name="连续剧"),
        playlist=[PlayItem(title="正片", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()
    window._metadata_scrape_category_combo.setCurrentIndex(window._metadata_scrape_category_combo.findData("动漫"))
    window._rerun_metadata_scrape_search()

    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)
    assert service.search_queries == [("动漫", "连续剧")]


def test_player_window_metadata_scrape_dialog_clears_previous_results_when_reopened_for_another_item(qtbot) -> None:
    service = FakeMetadataScrapeService()
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            metadata_scrape_service=service,
        )
    )
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._close_metadata_scrape_dialog()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="v2", vod_name="牧神记", vod_year="2024"),
            playlist=[PlayItem(title="第1集", url="https://media.example/2.mp4")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            metadata_scrape_service=service,
        )
    )
    window._open_metadata_scrape_dialog()

    assert window._metadata_scrape_title_edit.text() == "牧神记"
    assert window._metadata_scrape_year_edit.text() == "2024"
    assert window._metadata_scrape_group_list.count() == 0
    assert window._metadata_scrape_result_list.count() == 0
    assert window._metadata_scrape_groups == []
    assert window._metadata_scrape_status_label.text() == ""


def test_player_window_metadata_scrape_dialog_preserves_user_query_when_reopened_for_same_item(qtbot) -> None:
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()
    window._metadata_scrape_title_edit.setText("手动修改标题")
    window._metadata_scrape_year_edit.setText("2030")
    window._metadata_scrape_category_combo.setCurrentIndex(window._metadata_scrape_category_combo.findData("动漫"))
    window._metadata_scrape_dialog.close()
    window._open_metadata_scrape_dialog()

    assert window._metadata_scrape_title_edit.text() == "手动修改标题"
    assert window._metadata_scrape_year_edit.text() == "2030"
    assert window._metadata_scrape_category_combo.currentData() == "动漫"


def test_player_window_metadata_scrape_dialog_rerun_persists_query_state_in_file_cache(qtbot, monkeypatch, tmp_path) -> None:
    import atv_player.metadata.dialog_cache as metadata_dialog_cache_module

    monkeypatch.setattr(metadata_dialog_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()
    window._metadata_scrape_title_edit.setText("手动修改标题")
    window._metadata_scrape_year_edit.setText("2030")
    window._metadata_scrape_category_combo.setCurrentIndex(window._metadata_scrape_category_combo.findData("动漫"))
    window._rerun_metadata_scrape_search()

    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    restarted = PlayerWindow(FakePlayerController())
    qtbot.addWidget(restarted)
    restarted.open_session(
        PlayerSession(
            vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            metadata_scrape_service=FakeMetadataScrapeService(),
        )
    )
    restarted._open_metadata_scrape_dialog()

    assert restarted._metadata_scrape_title_edit.text() == "手动修改标题"
    assert restarted._metadata_scrape_year_edit.text() == "2030"
    assert restarted._metadata_scrape_category_combo.currentData() == "动漫"


def test_player_window_metadata_scrape_dialog_reset_persists_query_state_in_file_cache(qtbot, monkeypatch, tmp_path) -> None:
    import atv_player.metadata.dialog_cache as metadata_dialog_cache_module

    monkeypatch.setattr(metadata_dialog_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="仙剑奇侠传叁", vod_year="2025"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()
    window._metadata_scrape_title_edit.setText("仙剑奇侠传三")
    window._metadata_scrape_year_edit.setText("2025")
    window._metadata_scrape_category_combo.setCurrentIndex(window._metadata_scrape_category_combo.findData("动漫"))
    window._reset_metadata_scrape_state()

    assert service.reset_calls == [("仙剑奇侠传三", "2025", "", "", [])]

    restarted = PlayerWindow(FakePlayerController())
    qtbot.addWidget(restarted)
    restarted.open_session(
        PlayerSession(
            vod=VodItem(vod_id="v1", vod_name="仙剑奇侠传叁", vod_year="2025"),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            metadata_scrape_service=FakeMetadataScrapeService(),
        )
    )
    restarted._open_metadata_scrape_dialog()

    assert restarted._metadata_scrape_title_edit.text() == "仙剑奇侠传三"
    assert restarted._metadata_scrape_year_edit.text() == "2025"
    assert restarted._metadata_scrape_category_combo.currentData() == "动漫"


def test_player_window_metadata_scrape_dialog_apply_persists_query_state_in_file_cache(qtbot, monkeypatch, tmp_path) -> None:
    import atv_player.metadata.dialog_cache as metadata_dialog_cache_module

    monkeypatch.setattr(metadata_dialog_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    service = FakeMetadataScrapeService()
    service.cached_groups = list(service.groups)
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)
    window._metadata_scrape_title_edit.setText("手动绑定标题")
    window._metadata_scrape_year_edit.setText("2031")
    window._metadata_scrape_category_combo.setCurrentIndex(window._metadata_scrape_category_combo.findData("动漫"))
    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: len(service.apply_calls) == 1, timeout=1000)

    restarted = PlayerWindow(FakePlayerController())
    qtbot.addWidget(restarted)
    restarted.open_session(
        PlayerSession(
            vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            metadata_scrape_service=FakeMetadataScrapeService(),
        )
    )
    restarted._open_metadata_scrape_dialog()

    assert restarted._metadata_scrape_title_edit.text() == "手动绑定标题"
    assert restarted._metadata_scrape_year_edit.text() == "2031"
    assert restarted._metadata_scrape_category_combo.currentData() == "动漫"


def test_player_window_metadata_scrape_dialog_restore_default_resets_title_year_and_category_in_file_cache(
    qtbot, monkeypatch, tmp_path
) -> None:
    import atv_player.metadata.dialog_cache as metadata_dialog_cache_module

    monkeypatch.setattr(metadata_dialog_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    first = PlayerWindow(FakePlayerController())
    qtbot.addWidget(first)
    first.open_session(
        PlayerSession(
            vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            metadata_scrape_service=FakeMetadataScrapeService(),
        )
    )
    first._open_metadata_scrape_dialog()
    first._metadata_scrape_title_edit.setText("手动修改标题")
    first._metadata_scrape_year_edit.setText("2030")
    first._metadata_scrape_category_combo.setCurrentIndex(first._metadata_scrape_category_combo.findData("动漫"))
    first._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: first._metadata_scrape_result_list.count() == 1, timeout=1000)

    second = PlayerWindow(FakePlayerController())
    qtbot.addWidget(second)
    second.open_session(
        PlayerSession(
            vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            metadata_scrape_service=FakeMetadataScrapeService(),
        )
    )
    second._open_metadata_scrape_dialog()

    assert second._metadata_scrape_restore_query_button.text() == "恢复默认"

    second._metadata_scrape_restore_query_button.click()

    assert second._metadata_scrape_title_edit.text() == "深空彼岸"
    assert second._metadata_scrape_year_edit.text() == "2026"
    assert second._metadata_scrape_category_combo.currentData() == ""

    restarted = PlayerWindow(FakePlayerController())
    qtbot.addWidget(restarted)
    restarted.open_session(
        PlayerSession(
            vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            metadata_scrape_service=FakeMetadataScrapeService(),
        )
    )
    restarted._open_metadata_scrape_dialog()

    assert restarted._metadata_scrape_title_edit.text() == "深空彼岸"
    assert restarted._metadata_scrape_year_edit.text() == "2026"
    assert restarted._metadata_scrape_category_combo.currentData() == ""


def test_player_window_metadata_scrape_dialog_ignores_blank_cached_query_and_falls_back_to_current_media(
    qtbot, monkeypatch, tmp_path
) -> None:
    import atv_player.metadata.dialog_cache as metadata_dialog_cache_module

    monkeypatch.setattr(metadata_dialog_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    metadata_dialog_cache_module.save_cached_metadata_scrape_dialog_state(
        "家业",
        "2026",
        metadata_dialog_cache_module.MetadataScrapeDialogState(title="", year="", category=""),
    )

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="家业", vod_year="2026"),
        playlist=[PlayItem(title="05-(1.09 GB)", url="https://media.example/5.mp4", media_title="家业")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=FakeMetadataScrapeService(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()

    assert window._metadata_scrape_title_edit.text() == "家业"
    assert window._metadata_scrape_year_edit.text() == "2026"


def test_player_window_metadata_scrape_dialog_reuses_existing_dialog_and_reactivates_it(qtbot, monkeypatch) -> None:
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=object(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    window._open_metadata_scrape_dialog()

    dialog = window._metadata_scrape_dialog
    assert dialog is not None

    calls: list[str] = []
    monkeypatch.setattr(dialog, "raise_", lambda: calls.append("raise"))
    monkeypatch.setattr(dialog, "activateWindow", lambda: calls.append("activate"))

    window._open_metadata_scrape_dialog()

    assert window._metadata_scrape_dialog is dialog
    assert calls == ["raise", "activate"]


def test_player_window_metadata_scrape_dialog_ignores_inflight_previous_search_when_reopened(qtbot) -> None:
    class BlockingMetadataScrapeService(FakeMetadataScrapeService):
        def __init__(self) -> None:
            super().__init__()
            self.search_started = threading.Event()
            self.release_search = threading.Event()

        def search(self, query, provider_filter: str = "", cache_only: bool = False) -> list[MetadataScrapeGroup]:
            if cache_only:
                return super().search(query, provider_filter=provider_filter, cache_only=True)
            self.search_started.set()
            assert self.release_search.wait(timeout=1)
            return super().search(query, provider_filter=provider_filter, cache_only=False)

    blocking_service = BlockingMetadataScrapeService()
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
            playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            metadata_scrape_service=blocking_service,
        )
    )
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    assert blocking_service.search_started.wait(timeout=1)

    window._close_metadata_scrape_dialog()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="v2", vod_name="牧神记", vod_year="2024"),
            playlist=[PlayItem(title="第1集", url="https://media.example/2.mp4")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            metadata_scrape_service=FakeMetadataScrapeService(),
        )
    )
    window._open_metadata_scrape_dialog()

    blocking_service.release_search.set()
    qtbot.wait(100)

    assert window._metadata_scrape_title_edit.text() == "牧神记"
    assert window._metadata_scrape_result_list.count() == 0
    assert window._metadata_scrape_group_list.count() == 0


def test_player_window_metadata_scrape_dialog_reuses_cached_auto_hydration_results(qtbot, tmp_path: Path) -> None:
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

    class FakeProvider:
        name = "tmdb"

        def __init__(self) -> None:
            self.search_calls: list[tuple[str, str]] = []

        def can_enrich(self, _context) -> bool:
            return True

        def search(self, query) -> list[MetadataMatch]:
            self.search_calls.append((query.title, query.year))
            return [MetadataMatch(provider="tmdb", provider_id="movie:1", title="深空彼岸", year="2026", score=1.0)]

        def get_detail(self, match: MetadataMatch) -> MetadataRecord:
            return MetadataRecord(
                provider=match.provider,
                provider_id=match.provider_id,
                title=match.title,
                year=match.year,
                overview="自动刮削简介",
            )

    provider = FakeProvider()
    cache = MetadataCache(tmp_path)
    hydrator = MetadataHydrator(cache=cache, providers=[provider])
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda current_session: hydrator.hydrate(
            MetadataContext(
                vod=current_session.vod,
                source_kind="plugin",
                current_item=current_session.playlist[current_session.start_index],
            )
        ),
        metadata_scrape_service=MetadataScrapeService(cache=cache, providers=[provider]),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(session)

    qtbot.waitUntil(lambda: "自动刮削简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert provider.search_calls == [("深空彼岸", "2026")]

    window._open_metadata_scrape_dialog()

    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)
    assert window._metadata_scrape_group_list.count() == 1
    assert window._metadata_scrape_result_list.item(0).text() == "深空彼岸 (2026) · 电影"
    assert provider.search_calls == [("深空彼岸", "2026")]


class FakeMetadataScrapeService:
    def __init__(self, provider_options: list[tuple[str, str]] | None = None) -> None:
        self.search_calls: list[tuple[str, str, str]] = []
        self.search_queries: list[tuple[str, str]] = []
        self.apply_calls: list[tuple[str, str]] = []
        self.build_episode_title_playlist_calls: list[tuple[str, str]] = []
        self.build_episode_title_playlist_allow_ai_fallback_values: list[bool] = []
        self.reset_calls: list[tuple[str, str, str, str, list[tuple[str, str]]]] = []
        self.reset_query_contexts: list[tuple[str, str]] = []
        self.cached_groups: list[MetadataScrapeGroup] = []
        self._provider_options = list(
            provider_options
            or [
                ("official_douban", "豆瓣官方"),
                ("tmdb", "TMDB"),
                ("local_douban", "本地豆瓣"),
                ("douban", "豆瓣"),
                ("plugin", "插件"),
            ]
        )
        self.groups = [
            MetadataScrapeGroup(
                provider="tmdb",
                provider_label="TMDB",
                items=[
                    MetadataScrapeCandidate(
                        provider="tmdb",
                        provider_label="TMDB",
                        provider_id="movie:1",
                        title="深空彼岸",
                        year="2026",
                    )
                ],
            ),
            MetadataScrapeGroup(provider="official_douban", provider_label="豆瓣官方", items=[]),
        ]

    def provider_options(self, query=None) -> list[tuple[str, str]]:
        del query
        return list(self._provider_options)

    def search(self, query, provider_filter: str = "", cache_only: bool = False) -> list[MetadataScrapeGroup]:
        if cache_only:
            return list(self.cached_groups)
        self.search_calls.append((query.title, query.year, provider_filter))
        self.search_queries.append((getattr(query, "category_name", ""), getattr(query, "type_name", "")))
        return self.groups

    def apply(self, vod: VodItem, candidate: MetadataScrapeCandidate) -> VodItem:
        self.apply_calls.append((vod.vod_name, candidate.provider_id))
        return VodItem(
            vod_id=vod.vod_id,
            vod_name=vod.vod_name,
            vod_year="2026",
            vod_pic="https://img.example/poster.jpg",
            vod_content="豆瓣简介",
            detail_fields=[PlaybackDetailField(label="TMDB ID", value="1")],
            metadata_field_sources={"poster": "tmdb", "overview": "tmdb", "detail_fields": "tmdb"},
        )

    def build_episode_title_playlist(
        self,
        vod: VodItem,
        playlist: list[PlayItem],
        *,
        preferred_candidate=None,
        allow_ai_fallback: bool = True,
    ):
        self.build_episode_title_playlist_calls.append((vod.vod_name, getattr(preferred_candidate, "provider", "")))
        self.build_episode_title_playlist_allow_ai_fallback_values.append(allow_ai_fallback)
        return [
            PlayItem(
                title=item.title,
                original_title=item.original_title or item.title,
                episode_display_title="第1集 第01话 金银米小圈1",
                episode_title_source="tencent",
                url=item.url,
                path=item.path,
                index=item.index,
                detail_fields=list(item.detail_fields),
            )
            for item in playlist
        ]

    def reset(
        self,
        query,
        *,
        bound_provider: str = "",
        bound_provider_id: str = "",
        detail_keys: list[tuple[str, str]] | None = None,
    ) -> None:
        self.reset_query_contexts.append((getattr(query, "source_kind", ""), getattr(query, "vod_id", "")))
        self.reset_calls.append(
            (query.title, query.year, bound_provider, bound_provider_id, list(detail_keys or []))
        )


class FakeMetadataBindingRepository:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, str, str, str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.load_calls: list[tuple[str, str]] = []
        self.bindings_by_key = {}
        self.binding = None
        self.redirects: dict[tuple[str, str], tuple[str, str]] = {}

    def load(self, title, year):
        self.load_calls.append((title, year))
        key = (title, year)
        if key in self.bindings_by_key:
            return self.bindings_by_key[key]
        return self.binding

    def save(self, title, year, *, provider, provider_id, matched_title="", matched_year="") -> None:
        self.saved.append((title, year, provider, provider_id, matched_title, matched_year))

    def save_query_redirect(self, title, year, redirect_title, redirect_year="") -> None:
        redirect_title = str(redirect_title or "").strip()
        if not redirect_title:
            return
        self.saved.append(
            (
                title,
                year,
                "__query_redirect__",
                redirect_title,
                redirect_title,
                str(redirect_year or "").strip(),
            )
        )

    def load_query_redirect(self, title, year):
        return self.redirects.get((str(title or "").strip(), str(year or "").strip()))

    def delete(self, title, year) -> None:
        self.deleted.append((title, year))


def test_player_window_metadata_scrape_dialog_hides_providers_missing_from_service(qtbot) -> None:
    service = FakeMetadataScrapeService(
        provider_options=[
            ("local_douban", "本地豆瓣"),
            ("plugin", "插件"),
        ]
    )
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()

    assert [window._metadata_scrape_provider_combo.itemData(index) for index in range(window._metadata_scrape_provider_combo.count())] == [
        "",
        "local_douban",
        "plugin",
    ]


def test_player_window_metadata_scrape_search_selects_first_result_without_auto_apply(qtbot) -> None:
    service = FakeMetadataScrapeService()
    service.groups[0].items[0].subtitle = "动漫"
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()

    window._rerun_metadata_scrape_search()

    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)
    assert window._metadata_scrape_result_list.currentRow() == 0
    assert window._metadata_scrape_result_list.item(0).text() == "深空彼岸 (2026) · 动漫"
    assert "原始简介" in window.metadata_view.toPlainText()
    assert service.apply_calls == []


def test_player_window_metadata_scrape_apply_refreshes_metadata_and_saves_binding(qtbot) -> None:
    service = FakeMetadataScrapeService()
    bindings = FakeMetadataBindingRepository()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
        metadata_binding_repository=bindings,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: "豆瓣简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert service.apply_calls == [("深空彼岸", "movie:1")]
    assert bindings.saved == [("深空彼岸", "2026", "tmdb", "movie:1", "深空彼岸", "2026")]
    assert "元数据已更新" in window.log_view.toPlainText()
    assert "已绑定手动刮削结果" in window.log_view.toPlainText()


def test_player_window_danmaku_source_dialog_falls_back_to_current_vod_title_when_search_title_missing(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window.session.vod.vod_name = "深空彼岸"
    window._open_danmaku_source_dialog()

    assert window._danmaku_source_title_edit.text() == "深空彼岸"


def test_player_window_rerun_danmaku_search_uses_fallback_current_vod_title(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, str | None, str | None, list[PlayItem] | None, bool, int, str]] = []

        def refresh_danmaku_sources(
            self,
            item: PlayItem,
            query_override: str | None = None,
            search_title_override: str | None = None,
            search_episode_override: str | None = None,
            playlist: list[PlayItem] | None = None,
            force_refresh: bool = False,
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> None:
            self.calls.append(
                (
                    query_override,
                    search_title_override,
                    search_episode_override,
                    playlist,
                    force_refresh,
                    media_duration_seconds,
                    provider_filter,
                )
            )

    controller = FakeDanmakuController()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)
    window.session.vod.vod_name = "深空彼岸"
    window._open_danmaku_source_dialog()
    qtbot.waitUntil(lambda: controller.calls == [(None, "深空彼岸", None, session.playlist, True, 120, "")])
    qtbot.waitUntil(lambda: session.playlist[0].danmaku_pending is False)

    window._rerun_current_item_danmaku_search()

    qtbot.waitUntil(
        lambda: controller.calls
        == [
            (None, "深空彼岸", None, session.playlist, True, 120, ""),
            (None, "深空彼岸", "", session.playlist, True, 120, ""),
        ]
    )


def test_player_window_danmaku_source_rerun_button_queues_while_auto_search_is_active(qtbot) -> None:
    class BlockingDanmakuController:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, str | None, str | None]] = []
            self.search_started = threading.Event()
            self.release_search = threading.Event()

        def load_cached_danmaku_sources(self, item: PlayItem) -> bool:
            return False

        def refresh_danmaku_sources(
            self,
            item: PlayItem,
            query_override: str | None = None,
            search_title_override: str | None = None,
            search_episode_override: str | None = None,
            playlist: list[PlayItem] | None = None,
            force_refresh: bool = False,
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> None:
            self.calls.append((query_override, search_title_override, search_episode_override))
            self.search_started.set()
            assert self.release_search.wait(timeout=1)

    controller = BlockingDanmakuController()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)

    window._open_danmaku_source_dialog()
    assert controller.search_started.wait(timeout=1)
    assert window._danmaku_source_rerun_button is not None
    assert window._danmaku_source_rerun_button.isEnabled() is True

    window._danmaku_source_rerun_button.click()
    controller.release_search.set()

    qtbot.waitUntil(
        lambda: controller.calls
        == [
            (None, "深空彼岸", None),
            (None, "深空彼岸", ""),
        ]
    )


def test_player_window_open_danmaku_source_dialog_loads_cached_results_with_fallback_current_vod_title(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.seen_titles: list[str] = []

        def load_cached_danmaku_sources(self, item: PlayItem) -> bool:
            self.seen_titles.append(item.danmaku_search_title)
            if item.danmaku_search_title != "深空彼岸":
                return False
            item.danmaku_search_episode = "1集"
            item.danmaku_search_query = "深空彼岸 1集"
            item.danmaku_candidates = [
                DanmakuSourceGroup(
                    provider="tencent",
                    provider_label="腾讯",
                    options=[DanmakuSourceOption(provider="tencent", name="深空彼岸 第1集", url="https://v.qq.com/demo")],
                )
            ]
            item.selected_danmaku_provider = "tencent"
            item.selected_danmaku_url = "https://v.qq.com/demo"
            item.selected_danmaku_title = "深空彼岸 第1集"
            return True

    controller = FakeDanmakuController()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window.session.vod.vod_name = "深空彼岸"

    window._open_danmaku_source_dialog()

    assert controller.seen_titles == ["深空彼岸"]
    assert window._danmaku_source_provider_list.count() == 1
    assert window._danmaku_source_title_edit.text() == "深空彼岸"


def test_player_window_open_danmaku_source_dialog_auto_searches_when_cache_misses_with_fallback_title(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.cache_titles: list[str] = []
            self.refresh_calls: list[tuple[str | None, str | None, str | None, list[PlayItem] | None, bool, int, str]] = []

        def load_cached_danmaku_sources(self, item: PlayItem) -> bool:
            self.cache_titles.append(item.danmaku_search_title)
            return False

        def refresh_danmaku_sources(
            self,
            item: PlayItem,
            query_override: str | None = None,
            search_title_override: str | None = None,
            search_episode_override: str | None = None,
            playlist: list[PlayItem] | None = None,
            force_refresh: bool = False,
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> None:
            self.refresh_calls.append(
                (
                    query_override,
                    search_title_override,
                    search_episode_override,
                    playlist,
                    force_refresh,
                    media_duration_seconds,
                    provider_filter,
                )
            )
            item.danmaku_search_episode = "1集"
            item.danmaku_search_query = "深空彼岸 1集"
            item.danmaku_candidates = [
                DanmakuSourceGroup(
                    provider="tencent",
                    provider_label="腾讯",
                    options=[DanmakuSourceOption(provider="tencent", name="深空彼岸 第1集", url="https://v.qq.com/demo")],
                )
            ]

    controller = FakeDanmakuController()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)
    window.session.vod.vod_name = "深空彼岸"

    window._open_danmaku_source_dialog()

    qtbot.waitUntil(
        lambda: controller.refresh_calls == [(None, "深空彼岸", None, session.playlist, True, 120, "")]
    )
    assert controller.cache_titles == ["深空彼岸"]
    assert window._danmaku_source_title_edit.text() == "深空彼岸"


def test_player_window_open_danmaku_source_dialog_auto_searches_when_stale_query_exists_but_no_candidates(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.refresh_calls: list[tuple[str | None, str | None, str | None, list[PlayItem] | None, bool, int, str]] = []

        def load_cached_danmaku_sources(self, item: PlayItem) -> bool:
            return False

        def refresh_danmaku_sources(
            self,
            item: PlayItem,
            query_override: str | None = None,
            search_title_override: str | None = None,
            search_episode_override: str | None = None,
            playlist: list[PlayItem] | None = None,
            force_refresh: bool = False,
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> None:
            self.refresh_calls.append(
                (
                    query_override,
                    search_title_override,
                    search_episode_override,
                    playlist,
                    force_refresh,
                    media_duration_seconds,
                    provider_filter,
                )
            )

    controller = FakeDanmakuController()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_year="2026", vod_content="原始简介"),
        playlist=[
            PlayItem(
                title="第1集",
                url="https://media.example/1.mp4",
                danmaku_search_query="旧标题 1集",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)
    window.session.vod.vod_name = "深空彼岸"

    window._open_danmaku_source_dialog()

    qtbot.waitUntil(
        lambda: controller.refresh_calls == [(None, "深空彼岸", None, session.playlist, True, 120, "")]
    )
    assert window._danmaku_source_title_edit.text() == "深空彼岸"


def test_player_window_open_danmaku_source_dialog_normalizes_automatic_title_and_auto_searches(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.cache_titles: list[str] = []
            self.refresh_calls: list[tuple[str | None, str | None, str | None, list[PlayItem] | None, bool, int, str]] = []

        def load_cached_danmaku_sources(self, item: PlayItem) -> bool:
            self.cache_titles.append(item.danmaku_search_title)
            return False

        def refresh_danmaku_sources(
            self,
            item: PlayItem,
            query_override: str | None = None,
            search_title_override: str | None = None,
            search_episode_override: str | None = None,
            playlist: list[PlayItem] | None = None,
            force_refresh: bool = False,
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> None:
            self.refresh_calls.append(
                (
                    query_override,
                    search_title_override,
                    search_episode_override,
                    playlist,
                    force_refresh,
                    media_duration_seconds,
                    provider_filter,
                )
            )

    controller = FakeDanmakuController()
    noisy_title = "📺 电视剧：良陈美锦 (2026) S01E26"
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="8@swf2fkq3zrk@t58d", vod_year="2026", vod_content="原始简介"),
        playlist=[
            PlayItem(
                title="第26集",
                url="https://media.example/26.mp4",
                media_title=noisy_title,
                danmaku_search_title=noisy_title,
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)

    window._open_danmaku_source_dialog()

    qtbot.waitUntil(
        lambda: controller.refresh_calls == [(None, "良陈美锦 (2026)", None, session.playlist, True, 120, "")]
    )
    assert controller.cache_titles == ["良陈美锦 (2026)"]
    assert window._danmaku_source_title_edit.text() == "良陈美锦 (2026)"


def test_player_window_metadata_scrape_apply_still_works_after_metadata_hydration_request_started(qtbot) -> None:
    service = FakeMetadataScrapeService()
    bindings = FakeMetadataBindingRepository()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda current_session: VodItem(
            vod_id=current_session.vod.vod_id,
            vod_name=current_session.vod.vod_name,
            vod_year=current_session.vod.vod_year,
            vod_content=current_session.vod.vod_content,
            metadata_field_sources=dict(current_session.vod.metadata_field_sources),
        ),
        metadata_scrape_service=service,
        metadata_binding_repository=bindings,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: "豆瓣简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert service.apply_calls == [("深空彼岸", "movie:1")]
    assert bindings.saved == [("深空彼岸", "2026", "tmdb", "movie:1", "深空彼岸", "2026")]
    assert "已绑定手动刮削结果" in window.log_view.toPlainText()


def test_player_window_metadata_scrape_apply_saves_binding_under_original_query_after_auto_hydration(qtbot) -> None:
    service = FakeMetadataScrapeService()
    bindings = FakeMetadataBindingRepository()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="黑袍纠察队第五季", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
        metadata_binding_repository=bindings,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window.session.vod = VodItem(
        vod_id="v1",
        vod_name="黑袍纠察队",
        vod_year="2019",
        vod_content="自动简介",
    )
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: "豆瓣简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert service.apply_calls == [("黑袍纠察队", "movie:1")]
    # 原始查询 key 仍是主绑定；刮削后标题另存一条别名绑定，供历史重开时按
    # 历史 vod_name（已被改写成刮削标题）查询命中。
    assert bindings.saved == [
        ("黑袍纠察队第五季", "2026", "tmdb", "movie:1", "深空彼岸", "2026"),
        ("黑袍纠察队", "2026", "tmdb", "movie:1", "深空彼岸", "2026"),
    ]


def test_player_window_metadata_scrape_alias_binding_covers_drive_history_reopen_and_reset(qtbot) -> None:
    # 网盘资源打开时 media_title 是分享卡片标题；播放中刮削把 vod_name 改写成正确
    # 标题并上报进播放历史，历史重开会用该标题覆盖 media_title。绑定必须同时存在
    # 于原始标题与刮削后标题两个 key 下，重开才能命中；重置时两条都要清理。
    service = FakeMetadataScrapeService()
    bindings = FakeMetadataBindingRepository()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="等不到说我爱你（82集）崔秀子＆蒋文琦", vod_content="原始简介"),
        playlist=[
            PlayItem(
                title="第1集",
                url="https://media.example/1.mp4",
                media_title="等不到说我爱你（82集）崔秀子＆蒋文琦",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
        metadata_binding_repository=bindings,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    # 自动水合先把 vod_name 更正为干净标题，随后手动刮削以该标题应用
    window.session.vod = VodItem(vod_id="v1", vod_name="等不到说我爱你", vod_content="自动简介")
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: "已绑定手动刮削结果" in window.log_view.toPlainText(), timeout=1000)
    assert bindings.saved == [
        ("等不到说我爱你（82集）崔秀子＆蒋文琦", "", "tmdb", "movie:1", "深空彼岸", "2026"),
        ("等不到说我爱你", "", "tmdb", "movie:1", "深空彼岸", "2026"),
    ]

    window._reset_metadata_scrape_state()

    qtbot.waitUntil(lambda: "已重置元数据缓存与手动绑定" in window.log_view.toPlainText(), timeout=1000)
    assert ("等不到说我爱你（82集）崔秀子＆蒋文琦", "") in bindings.deleted
    assert ("等不到说我爱你", "") in bindings.deleted


def test_player_window_metadata_scrape_reset_persists_manual_query_as_redirect(qtbot) -> None:
    # "自动刮削"按手输的修正标题重跑自动水合；修正需持久化为查询重定向，否则重开
    # 会话后按混淆目录名（如网盘目录 "X 心动的XH9"）搜索又找不到元数据。
    service = FakeMetadataScrapeService()
    bindings = FakeMetadataBindingRepository()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="X 心动的XH9"),
        playlist=[
            PlayItem(
                title="2026.08.18-第3期下",
                url="https://media.example/1.mp4",
                media_title="X 心动的XH9",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
        metadata_binding_repository=bindings,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()
    window._metadata_scrape_title_edit.setText("心动的信号 第九季")

    window._reset_metadata_scrape_state()

    redirect_saves = [row for row in bindings.saved if row[2] == "__query_redirect__"]
    assert redirect_saves == [
        ("X 心动的XH9", "", "__query_redirect__", "心动的信号 第九季", "心动的信号 第九季", "")
    ]
    assert window._metadata_hydration_override_title == "心动的信号 第九季"


def test_player_window_danmaku_source_dialog_recomputes_empty_episode(qtbot) -> None:
    # 网盘替换播放列表等前置流程可能把集数留空且已带候选：对话框打开时兜底重算
    # 默认集数，保证集数框有值、后续搜索带上集数锚点。
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.resolve_calls: list[tuple[PlayItem, list[PlayItem] | None]] = []

        def resolve_default_danmaku_search_episode(self, item: PlayItem, playlist: list[PlayItem] | None = None) -> str:
            self.resolve_calls.append((item, playlist))
            return "20260818 第3期下"

    controller = FakeDanmakuController()
    item = PlayItem(
        title="2026.08.18-第3期下",
        url="https://media.example/1.mp4",
        media_title="X 心动的XH9",
        danmaku_search_title="X 心动的XH9",
        danmaku_search_episode="",
        danmaku_candidates=[
            DanmakuSourceGroup(provider="tencent", provider_label="腾讯", options=[])
        ],
    )
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="X 心动的XH9"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)

    window._open_danmaku_source_dialog()

    assert window._danmaku_source_episode_edit.text() == "20260818 第3期下"
    assert item.danmaku_search_query == "X 心动的XH9 20260818 第3期下"


def test_player_window_open_session_seeds_danmaku_title_from_query_redirect(qtbot) -> None:
    # 弹幕搜索用混淆目录名（"X 心动的XH9"）什么都搜不到：打开会话时把查询重
    # 定向记录的修正标题预填到 danmaku_search_title，自动搜索与弹幕源对话框都
    # 直接使用修正标题；偏好/手动设置过的其它标题不被覆盖。
    bindings = FakeMetadataBindingRepository()
    bindings.redirects[("X 心动的XH9", "")] = ("心动的信号 第九季", "")
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="X 心动的XH9"),
        playlist=[
            PlayItem(
                title="2026.08.18-第3期下",
                url="https://media.example/1.mp4",
                media_title="X 心动的XH9",
                danmaku_search_title="X 心动的XH9",
            ),
            PlayItem(
                title="2026.08.18-第3期上",
                url="https://media.example/2.mp4",
                media_title="X 心动的XH9",
                danmaku_search_title="用户手动指定",
                danmaku_search_query_overridden=True,
            ),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_binding_repository=bindings,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    assert window.session.playlist[0].danmaku_search_title == "心动的信号 第九季"
    assert window.session.playlist[1].danmaku_search_title == "用户手动指定"


def test_player_window_rerun_episode_title_enhancement_forces_refresh(qtbot) -> None:
    # 右键菜单"重写剧集标题"手动触发：忽略 episode_titles_hydrated 与已保存的
    # 标题缓存（episode_titles_force_refresh），重新执行增强器。
    enhancer_calls: list[bool] = []

    def enhancer(current_session) -> list[PlayItem] | None:
        enhancer_calls.append(bool(current_session.episode_titles_force_refresh))
        current_session.episode_titles_force_refresh = False
        return None

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="X 心动的XH9"),
        playlist=[PlayItem(title="2026.08.18-第3期下", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        episode_title_enhancer=enhancer,
        episode_titles_hydrated=True,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)

    window._rerun_episode_title_enhancement()

    qtbot.waitUntil(lambda: bool(enhancer_calls), timeout=2000)
    assert enhancer_calls == [True]
    assert window.session.episode_titles_hydrated is True


def test_player_window_metadata_scrape_apply_ignores_inflight_auto_hydration_result(qtbot) -> None:
    service = FakeMetadataScrapeService()
    ready = threading.Event()

    def hydrate(current_session: PlayerSession) -> VodItem:
        assert ready.wait(timeout=1)
        return VodItem(
            vod_id=current_session.vod.vod_id,
            vod_name="自动标题",
            vod_year="2019",
            vod_content="自动简介",
        )

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=hydrate,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: "豆瓣简介" in window.metadata_view.toPlainText(), timeout=1000)
    ready.set()
    qtbot.wait(100)

    assert "自动简介" not in window.metadata_view.toPlainText()
    assert "豆瓣简介" in window.metadata_view.toPlainText()


def test_player_window_metadata_hydration_survives_late_detail_resolution_overwrite(qtbot) -> None:
    class DetailResolvingController(FakePlayerController):
        def resolve_play_item_detail(self, session, play_item):
            if not play_item.vod_id or session.detail_resolver is None:
                return None
            if play_item.vod_id in session.resolved_vod_by_id:
                resolved_vod = session.resolved_vod_by_id[play_item.vod_id]
            else:
                resolved_vod = session.detail_resolver(play_item)
                session.resolved_vod_by_id[play_item.vod_id] = resolved_vod
            if resolved_vod is None:
                return None
            play_item.url = resolved_vod.items[0].url if resolved_vod.items else resolved_vod.vod_play_url
            return resolved_vod

    release_detail_resolution = threading.Event()

    def detail_resolver(item: PlayItem) -> VodItem:
        assert release_detail_resolution.wait(timeout=1)
        return VodItem(
            vod_id=item.vod_id,
            vod_name="原始标题",
            vod_content="原始简介",
            items=[PlayItem(title=item.title, url=item.url, vod_id=item.vod_id)],
        )

    def metadata_hydrator(_session: PlayerSession) -> VodItem:
        return VodItem(
            vod_id="movie-1",
            vod_name="刮削后的标题",
            vod_year="2026",
            vod_content="刮削后的简介",
        )

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="原始标题", vod_content="原始简介"),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8", vod_id="ep-1")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        detail_resolver=detail_resolver,
        metadata_hydrator=metadata_hydrator,
    )
    window = PlayerWindow(DetailResolvingController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: "刮削后的标题" in window.metadata_view.toPlainText(), timeout=1000)

    release_detail_resolution.set()
    qtbot.waitUntil(lambda: "ep-1" in window.session.resolved_vod_by_id, timeout=1000)
    qtbot.wait(50)

    assert "刮削后的标题" in window.metadata_view.toPlainText()
    assert "刮削后的简介" in window.metadata_view.toPlainText()
    assert "原始标题" not in window.metadata_view.toPlainText()
    assert "原始简介" not in window.metadata_view.toPlainText()


def test_player_window_metadata_original_toggle_switches_between_enhanced_and_original_content(qtbot) -> None:
    class FakeVideo:
        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
        ) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda _session: VodItem(
            vod_id="v1",
            vod_name="增强标题",
            vod_year="2024",
            vod_content="增强简介",
            detail_fields=[PlaybackDetailField(label="TMDB ID", value="1")],
        ),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: "增强简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert window._metadata_original_toggle.isHidden() is False
    assert window._metadata_original_toggle.isChecked() is False

    qtbot.mouseClick(window._metadata_original_toggle, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: "原始简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert "增强简介" not in window.metadata_view.toPlainText()
    assert "TMDB ID: 1" not in window.metadata_view.toPlainText()

    qtbot.mouseClick(window._metadata_original_toggle, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: "增强简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert "原始简介" not in window.metadata_view.toPlainText()


def test_player_window_hides_metadata_original_toggle_when_metadata_matches_original(qtbot) -> None:
    class FakeVideo:
        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
        ) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda _session: VodItem(
            vod_id="v1",
            vod_name="原始标题",
            vod_year="2026",
            vod_content="原始简介",
        ),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window._pending_metadata_session is None, timeout=1000)
    assert window._metadata_original_toggle.isHidden() is True


def test_player_window_metadata_original_toggle_has_visible_switch_styling(qtbot) -> None:
    class FakeVideo:
        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
        ) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda _session: VodItem(
            vod_id="v1",
            vod_name="增强标题",
            vod_year="2024",
            vod_content="增强简介",
        ),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window._metadata_original_toggle.isHidden() is False, timeout=1000)
    assert window._metadata_original_toggle.minimumWidth() >= 40
    assert window._metadata_original_toggle.minimumHeight() >= 18
    assert window._metadata_original_toggle.graphicsEffect() is not None
    assert window._metadata_heading_row.contentsMargins().right() >= 6


def test_player_window_metadata_scrape_apply_original_toggle_restores_original_item_detail_fields(qtbot) -> None:
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026", vod_content="原始简介"),
        playlist=[
            PlayItem(
                title="第1集",
                url="https://media.example/1.mp4",
                detail_fields=[PlaybackDetailField(label="站内热度", value="99")],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: "TMDB ID: 1" in window.metadata_view.toPlainText(), timeout=1000)
    qtbot.mouseClick(window._metadata_original_toggle, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "站内热度: 99" in window.metadata_view.toPlainText(), timeout=1000)
    assert "TMDB ID: 1" not in window.metadata_view.toPlainText()
    assert "原始简介" in window.metadata_view.toPlainText()


def test_player_window_metadata_original_toggle_uses_late_resolved_detail_as_original_snapshot(qtbot) -> None:
    class DetailResolvingController(FakePlayerController):
        def resolve_play_item_detail(self, session, play_item):
            if not play_item.vod_id or session.detail_resolver is None:
                return None
            resolved_vod = session.detail_resolver(play_item)
            session.resolved_vod_by_id[play_item.vod_id] = resolved_vod
            play_item.url = resolved_vod.items[0].url if resolved_vod.items else resolved_vod.vod_play_url
            return resolved_vod

    release_detail_resolution = threading.Event()

    def detail_resolver(item: PlayItem) -> VodItem:
        assert release_detail_resolution.wait(timeout=1)
        return VodItem(
            vod_id=item.vod_id,
            vod_name="站内标题",
            vod_content="站内简介",
            items=[PlayItem(title=item.title, url=item.url, vod_id=item.vod_id)],
        )

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="原始标题", vod_content="原始简介"),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8", vod_id="ep-1")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        detail_resolver=detail_resolver,
        metadata_hydrator=lambda _session: VodItem(
            vod_id="movie-1",
            vod_name="刮削后的标题",
            vod_year="2026",
            vod_content="刮削后的简介",
        ),
    )
    window = PlayerWindow(DetailResolvingController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: "刮削后的简介" in window.metadata_view.toPlainText(), timeout=1000)

    release_detail_resolution.set()
    qtbot.waitUntil(lambda: "ep-1" in window.session.resolved_vod_by_id, timeout=1000)
    qtbot.mouseClick(window._metadata_original_toggle, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: "站内简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert "刮削后的简介" not in window.metadata_view.toPlainText()


def test_player_window_hides_metadata_original_toggle_when_detail_panel_is_closed(qtbot) -> None:
    class FakeVideo:
        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
        ) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda _session: VodItem(vod_id="v1", vod_name="增强标题", vod_year="2024", vod_content="增强简介"),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: window._metadata_original_toggle.isHidden() is False, timeout=1000)

    window.toggle_details_button.click()

    assert window._metadata_original_toggle.isHidden() is True

    window.toggle_details_button.click()

    assert window._metadata_original_toggle.isHidden() is False


def test_player_window_hides_metadata_original_toggle_for_youtube_playback(qtbot) -> None:
    class FakeVideo:
        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
        ) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="abc123xyz89", vod_name="原始标题", vod_content="原始简介", detail_style="youtube"),
        playlist=[PlayItem(title="YouTube 视频", url="https://media.example/1.mp4", vod_id="abc123xyz89")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda _session: VodItem(
            vod_id="abc123xyz89",
            vod_name="增强标题",
            vod_content="增强简介",
            detail_style="youtube",
        ),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window._pending_metadata_session is None, timeout=1000)
    assert window._metadata_original_toggle.isHidden() is True
    assert window.session.show_original_metadata is False


def test_player_window_open_session_resets_metadata_original_toggle_to_enhanced_view(qtbot) -> None:
    class FakeVideo:
        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
        ) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    first = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda _session: VodItem(vod_id="v1", vod_name="增强标题", vod_year="2024", vod_content="增强简介"),
    )
    second = PlayerSession(
        vod=VodItem(vod_id="v2", vod_name="第二个标题", vod_year="2025", vod_content="第二个简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/2.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda _session: VodItem(
            vod_id="v2",
            vod_name="第二个增强标题",
            vod_year="2025",
            vod_content="第二个增强简介",
        ),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(first)
    qtbot.waitUntil(lambda: "增强简介" in window.metadata_view.toPlainText(), timeout=1000)
    qtbot.mouseClick(window._metadata_original_toggle, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "原始简介" in window.metadata_view.toPlainText(), timeout=1000)

    window.open_session(second)

    qtbot.waitUntil(lambda: "第二个增强简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert window._metadata_original_toggle.isChecked() is False
    assert "第二个简介" not in window.metadata_view.toPlainText()


def test_player_window_metadata_scrape_apply_replaces_current_item_detail_fields(qtbot) -> None:
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026", vod_content="原始简介"),
        playlist=[
            PlayItem(
                title="第1集",
                url="https://media.example/1.mp4",
                detail_fields=[PlaybackDetailField(label="站内热度", value="99")],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    assert "站内热度: 99" in window.metadata_view.toPlainText()

    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: "TMDB ID: 1" in window.metadata_view.toPlainText(), timeout=1000)
    assert "站内热度: 99" not in window.metadata_view.toPlainText()


def test_player_window_metadata_scrape_apply_refreshes_playlist_titles_from_selected_provider(qtbot) -> None:
    service = FakeMetadataScrapeService(
        provider_options=[
            ("tencent", "腾讯"),
            ("tmdb", "TMDB"),
        ]
    )
    service.groups = [
        MetadataScrapeGroup(
            provider="tencent",
            provider_label="腾讯",
            items=[
                MetadataScrapeCandidate(
                    provider="tencent",
                    provider_label="腾讯",
                    provider_id="tx:1",
                    title="米小圈上学记4",
                    year="2026",
                    raw={"episode_sites": [{"episodeInfoList": [{"title": "第01话 金银米小圈1"}]}]},
                )
            ],
        )
    ]
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="米小圈上学记4", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="01.mp4", original_title="01.mp4", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: window.session.playlist[0].episode_display_title == "第1集 第01话 金银米小圈1", timeout=1000)
    assert window.playlist_title_mode == "episode"
    assert service.build_episode_title_playlist_calls == [("米小圈上学记4", "tencent")]
    assert service.build_episode_title_playlist_allow_ai_fallback_values == [False]


def test_player_window_metadata_scrape_apply_rebuilds_playlist_titles_off_ui_thread(qtbot) -> None:
    class ThreadRecordingMetadataScrapeService(FakeMetadataScrapeService):
        def __init__(self) -> None:
            super().__init__()
            self.build_threads: list[threading.Thread] = []

        def build_episode_title_playlist(self, vod: VodItem, playlist: list[PlayItem], *, preferred_candidate=None):
            self.build_threads.append(threading.current_thread())
            return super().build_episode_title_playlist(vod, playlist, preferred_candidate=preferred_candidate)

    service = ThreadRecordingMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="米小圈上学记4", vod_year="2026", vod_content="原始简介"),
        playlist=[PlayItem(title="01.mp4", original_title="01.mp4", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: bool(service.build_threads), timeout=1000)
    assert service.build_threads[0] is not threading.main_thread()


def test_player_window_metadata_scrape_apply_passes_selected_category_override_to_episode_title_rewrite(qtbot) -> None:
    class RecordingMetadataScrapeService(FakeMetadataScrapeService):
        def __init__(self) -> None:
            super().__init__(provider_options=[("tmdb", "TMDB")])
            self.build_episode_title_playlist_category_names: list[str] = []

        def build_episode_title_playlist(self, vod: VodItem, playlist: list[PlayItem], *, preferred_candidate=None):
            self.build_episode_title_playlist_category_names.append(str(vod.category_name or "").strip())
            return super().build_episode_title_playlist(vod, playlist, preferred_candidate=preferred_candidate)

    service = RecordingMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="仙剑奇侠传三", vod_year="2025", category_name="剧集", vod_content="原始简介"),
        playlist=[PlayItem(title="01.mp4", original_title="01.mp4", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()
    window._metadata_scrape_category_combo.setCurrentIndex(window._metadata_scrape_category_combo.findData("动漫"))
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: window.session.playlist[0].episode_display_title == "第1集 第01话 金银米小圈1", timeout=1000)
    assert service.build_episode_title_playlist_category_names == ["动漫"]


def test_player_window_metadata_scrape_reset_clears_binding_and_restarts_auto_search(qtbot) -> None:
    service = FakeMetadataScrapeService()
    bindings = FakeMetadataBindingRepository()
    bindings.binding = type("Binding", (), {"provider": "tmdb", "provider_id": "tv:42:season:5"})()
    hydration_calls: list[tuple[str, str, str]] = []
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="黑袍纠察队", vod_year="2026", vod_content="当前简介"),
        playlist=[
            PlayItem(
                title="第1集",
                url="https://media.example/1.mp4",
                media_title="📺 电视剧：黑袍纠察队 (2026) S05E01",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
        metadata_binding_repository=bindings,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window.session.metadata_hydrator = lambda current_session: (
        hydration_calls.append(
            (
                current_session.vod.vod_name,
                current_session.playlist[current_session.start_index].media_title,
                current_session.vod.vod_year,
            )
        )
        or VodItem(
            vod_id="v1",
            vod_name=current_session.vod.vod_name,
            vod_year=current_session.vod.vod_year,
            vod_content="自动简介",
        )
    )
    window.session.metadata_hydrated = True
    window._metadata_scrape_binding_title = "黑袍纠察队第五季"
    window._metadata_scrape_binding_year = "2026"
    window._open_metadata_scrape_dialog()
    window._metadata_scrape_title_edit.setText("手动改过的标题")
    window._metadata_scrape_year_edit.setText("2030")
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._reset_metadata_scrape_state()

    qtbot.waitUntil(lambda: "自动简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert bindings.deleted == [("黑袍纠察队第五季", "2026")]
    assert service.reset_calls == [
        (
            "手动改过的标题",
            "2030",
            "tmdb",
            "tv:42:season:5",
            [("tmdb", "movie:1")],
        )
    ]
    assert hydration_calls == [("手动改过的标题", "手动改过的标题", "2030")]
    assert service.search_calls[-1] == ("手动改过的标题", "2030", "")
    assert window._metadata_scrape_title_edit.text() == "手动改过的标题"
    assert window._metadata_scrape_year_edit.text() == "2030"
    assert "已重置元数据缓存与手动绑定" in window.log_view.toPlainText()


def test_player_window_metadata_scrape_reset_passes_selected_category_override_to_auto_hydration(qtbot) -> None:
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="仙剑奇侠传三", vod_year="2025", category_name="剧集", vod_content="当前简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4", media_title="仙剑奇侠传三")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    hydration_calls: list[tuple[str, str, str, str]] = []
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window.session.metadata_hydrator = lambda current_session: (
        hydration_calls.append(
            (
                current_session.vod.vod_name,
                current_session.playlist[current_session.start_index].media_title,
                current_session.vod.vod_year,
                current_session.vod.category_name,
            )
        )
        or current_session.vod
    )
    window.session.metadata_hydrated = True
    window._open_metadata_scrape_dialog()
    window._metadata_scrape_title_edit.setText("仙剑奇侠传三")
    window._metadata_scrape_year_edit.setText("2025")
    window._metadata_scrape_category_combo.setCurrentIndex(window._metadata_scrape_category_combo.findData("动漫"))
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._reset_metadata_scrape_state()

    qtbot.waitUntil(lambda: len(hydration_calls) == 1, timeout=1000)
    assert hydration_calls == [("仙剑奇侠传三", "仙剑奇侠传三", "2025", "动漫")]


def test_player_window_metadata_scrape_reset_passes_source_context_to_cache_reset(qtbot) -> None:
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="ss45969", vod_name="牧神记", vod_content="当前简介"),
        playlist=[PlayItem(title="第1话", url="https://media.example/1.mp4", media_title="牧神记")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        source_kind="bilibili",
        source_key="bilibili",
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()

    window._reset_metadata_scrape_state()

    assert service.reset_query_contexts == [("bilibili", "ss45969")]


def test_player_window_metadata_scrape_reset_uses_bilibili_season_id_detail_field(qtbot) -> None:
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(
            vod_id="ep3537929",
            vod_name="牧神记",
            vod_content="当前简介",
            detail_style="bilibili",
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
        ),
        playlist=[PlayItem(title="第1话", url="https://media.example/1.mp4", media_title="牧神记")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        source_kind="bilibili",
        source_key="bilibili",
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()

    window._reset_metadata_scrape_state()

    assert service.reset_query_contexts == [("bilibili", "season$45969")]


def test_player_window_metadata_scrape_reset_uses_original_bilibili_season_id_after_manual_apply(qtbot) -> None:
    service = FakeMetadataScrapeService()
    bindings = FakeMetadataBindingRepository()
    hydration_vods: list[tuple[str, str, list[tuple[str, str]]]] = []
    session = PlayerSession(
        vod=VodItem(
            vod_id="113367389900623-26520061025-1112251",
            vod_name="牧神记",
            vod_content="当前简介",
            detail_style="bilibili",
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
        ),
        playlist=[PlayItem(title="第1话", url="https://media.example/1.mp4", media_title="牧神记")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        source_kind="bilibili",
        source_key="bilibili",
        metadata_scrape_service=service,
        metadata_binding_repository=bindings,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._apply_selected_metadata_scrape_result()
    qtbot.waitUntil(lambda: "TMDB ID: 1" in window.metadata_view.toPlainText(), timeout=1000)
    assert bindings.saved == [("bilibili:season:45969", "", "tmdb", "movie:1", "深空彼岸", "2026")]
    assert "Season ID" not in window.session.vod.detail_fields[0].label
    bindings.bindings_by_key[("bilibili:season:45969", "")] = type(
        "Binding",
        (),
        {"provider": "tmdb", "provider_id": "movie:1"},
    )()
    window.session.metadata_hydrator = lambda current_session: (
        hydration_vods.append(
            (
                current_session.vod.vod_id,
                current_session.vod.vod_content,
                [(field.label, field.value) for field in current_session.vod.detail_fields],
            )
        )
        or current_session.vod
    )

    window._reset_metadata_scrape_state()

    qtbot.waitUntil(lambda: len(hydration_vods) == 1, timeout=1000)
    assert bindings.deleted == [("bilibili:season:45969", "")]
    assert service.reset_query_contexts[-1] == ("bilibili", "season$45969")
    assert hydration_vods[0][1] == "当前简介"
    assert ("Season ID", "45969") in hydration_vods[0][2]
    assert ("TMDB ID", "1") not in hydration_vods[0][2]


def test_player_window_metadata_scrape_reset_clears_episode_title_payload_caches(qtbot, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(player_window_module, "app_cache_dir", lambda: tmp_path / "app-cache", raising=False)
    cache = MetadataCache((tmp_path / "app-cache") / "metadata")
    cache.save_payload("tmdb_episode_search", "家业\x1f2026", [{"id": 275966, "name": "家业"}])
    cache.save_payload("tmdb_episode_season_detail", "275966:1", {"episodes": [{"episode_number": 7, "name": "旧标题"}]})
    cache.save_payload(
        "episode_title_playlist",
        "playlist-cache-key",
        {"order": [0], "titles": [{"display": "第7集 第 7 集", "source": "tmdb"}]},
    )
    cache.save_payload("other_namespace", "keep-me", {"ok": True})

    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="家业", vod_year="2026", category_name="剧集"),
        playlist=[PlayItem(title="07-(0.99 GB)", url="https://media.example/7.mp4", media_title="家业")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)
    window.session.metadata_hydrator = lambda current_session: current_session.vod
    window.session.metadata_hydrated = True
    window._open_metadata_scrape_dialog()

    window._reset_metadata_scrape_state()

    assert cache.load_payload("tmdb_episode_search", "家业\x1f2026", ttl_seconds=86400) is None
    assert cache.load_payload("tmdb_episode_season_detail", "275966:1", ttl_seconds=86400) is None
    assert cache.load_payload("episode_title_playlist", "playlist-cache-key", ttl_seconds=86400) is None
    assert cache.load_payload("other_namespace", "keep-me", ttl_seconds=86400) == {"ok": True}


def test_player_window_metadata_scrape_reset_restarts_episode_title_enhancement_even_when_signature_is_unchanged(qtbot) -> None:
    service = FakeMetadataScrapeService()
    hydration_calls: list[int] = []
    enhancement_calls: list[str] = []

    def hydrate(current_session: PlayerSession) -> VodItem:
        hydration_calls.append(len(hydration_calls) + 1)
        return VodItem(
            vod_id=current_session.vod.vod_id,
            vod_name=current_session.vod.vod_name,
            vod_year=current_session.vod.vod_year,
            category_name=current_session.vod.category_name,
            vod_content="重置后简介" if len(hydration_calls) > 1 else "首次简介",
        )

    def enhance(current_session: PlayerSession) -> list[PlayItem] | None:
        enhancement_calls.append(str(current_session.vod.vod_name or "").strip())
        return None

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="家业", vod_year="2026", category_name="剧集", vod_content="原始简介"),
        playlist=[PlayItem(title="07-(0.99 GB)", url="https://media.example/7.mp4", media_title="家业")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=hydrate,
        metadata_scrape_service=service,
        episode_title_enhancer=enhance,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    qtbot.waitUntil(lambda: "首次简介" in window.metadata_view.toPlainText(), timeout=1000)
    qtbot.waitUntil(lambda: enhancement_calls == ["家业"], timeout=1000)

    window._open_metadata_scrape_dialog()
    window._reset_metadata_scrape_state()

    qtbot.waitUntil(lambda: "重置后简介" in window.metadata_view.toPlainText(), timeout=1000)
    qtbot.waitUntil(lambda: len(hydration_calls) == 2, timeout=1000)
    assert enhancement_calls == ["家业", "家业"]


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
    assert any(action.text() == "弹幕设置" for action in menu.actions())


def test_player_window_video_context_menu_keeps_danmaku_source_action_enabled_without_candidates(qtbot) -> None:
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>',
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
    danmaku_source_action = next(action for action in menu.actions() if action.text() == "弹幕源")

    assert danmaku_source_action.isEnabled() is True
    assert window.danmaku_source_button.isEnabled() is True


def test_player_window_opens_danmaku_source_dialog_for_current_item(qtbot) -> None:
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_search_title="红果短剧",
        danmaku_search_episode="1集",
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
    assert window._danmaku_source_title_edit.text() == "红果短剧"
    assert window._danmaku_source_episode_edit.text() == "1集"
    assert window._danmaku_source_provider_list.count() == 1


class FakeOffsetDanmakuController:
    def __init__(self, offsets: dict[tuple[str, str], float] | None = None) -> None:
        self.offsets = dict(offsets or {})
        self.saved: list[float] = []

    def load_danmaku_offset(
        self,
        item: PlayItem,
        playlist: list[PlayItem] | None = None,
    ) -> float:
        del playlist
        return self.offsets.get((item.title, item.selected_danmaku_provider), 0.0)

    def save_danmaku_offset(
        self,
        item: PlayItem,
        value: float,
        playlist: list[PlayItem] | None = None,
    ) -> None:
        del playlist
        normalized = float(value)
        self.offsets[(item.title, item.selected_danmaku_provider)] = normalized
        self.saved.append(normalized)


def build_window_with_loaded_danmaku(
    qtbot,
    controller: object,
    *,
    items: list[PlayItem] | None = None,
) -> tuple[PlayerWindow, list[PlayItem]]:
    playlist = items or [
        PlayItem(
            title="第1集",
            url="https://stream.example/1.m3u8",
            media_title="红果短剧",
            danmaku_xml='<i><d p="5,1,25,16777215">ok</d></i>',
            selected_danmaku_provider="tencent",
            selected_danmaku_url="https://v.qq.com/1",
        )
    ]
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=playlist,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.session = session
    window.current_index = 0
    return window, playlist


def test_danmaku_source_dialog_shows_saved_episode_offset(qtbot) -> None:
    controller = FakeOffsetDanmakuController({("第1集", "tencent"): -3.0})
    window, _items = build_window_with_loaded_danmaku(qtbot, controller)

    window._open_danmaku_source_dialog()

    assert window._danmaku_source_offset_spin is not None
    assert window._danmaku_source_offset_spin.minimum() == -600.0
    assert window._danmaku_source_offset_spin.maximum() == 600.0
    assert window._danmaku_source_offset_spin.singleStep() == 0.5
    assert window._danmaku_source_offset_spin.value() == -3.0
    assert window._danmaku_source_offset_spin.isEnabled() is True
    assert window._danmaku_source_offset_spin.height() == 32


def test_danmaku_source_offset_change_debounces_save_and_rerender(qtbot, monkeypatch) -> None:
    controller = FakeOffsetDanmakuController()
    window, items = build_window_with_loaded_danmaku(qtbot, controller)
    renders: list[float] = []
    monkeypatch.setattr(
        window,
        "_configure_danmaku_for_current_item",
        lambda: renders.append(items[0].danmaku_offset_seconds),
    )

    window._open_danmaku_source_dialog()
    window._danmaku_source_offset_spin.setValue(-2.0)
    window._danmaku_source_offset_spin.setValue(-3.0)

    qtbot.waitUntil(lambda: controller.saved == [-3.0], timeout=1000)
    assert renders == [-3.0]


def test_danmaku_source_offset_controls_disable_without_xml_provider_or_while_loading(qtbot) -> None:
    controller = FakeOffsetDanmakuController()
    window, items = build_window_with_loaded_danmaku(qtbot, controller)
    item = items[0]
    item.danmaku_xml = ""

    window._open_danmaku_source_dialog()

    assert window._danmaku_source_offset_spin.isEnabled() is False
    item.danmaku_xml = '<i><d p="5,1,25,16777215">ok</d></i>'
    item.selected_danmaku_provider = ""
    window._sync_danmaku_offset_controls(item)
    assert window._danmaku_source_offset_spin.isEnabled() is False
    item.selected_danmaku_provider = "tencent"
    window._active_danmaku_source_task_counts[id(item)] = 1
    window._sync_danmaku_offset_controls(item)
    assert window._danmaku_source_offset_spin.isEnabled() is False


def test_danmaku_source_offset_reset_saves_zero_and_rerenders(qtbot, monkeypatch) -> None:
    controller = FakeOffsetDanmakuController({("第1集", "tencent"): -3.0})
    window, items = build_window_with_loaded_danmaku(qtbot, controller)
    renders: list[float] = []
    monkeypatch.setattr(
        window,
        "_configure_danmaku_for_current_item",
        lambda: renders.append(items[0].danmaku_offset_seconds),
    )

    window._open_danmaku_source_dialog()
    window._danmaku_source_offset_reset_button.click()

    qtbot.waitUntil(lambda: controller.saved == [0.0], timeout=1000)
    assert renders == [0.0]


def test_danmaku_source_switch_loads_provider_offset_before_rerender(qtbot, monkeypatch) -> None:
    controller = FakeOffsetDanmakuController(
        {
            ("第1集", "tencent"): -3.0,
            ("第1集", "youku"): 2.5,
        }
    )
    window, items = build_window_with_loaded_danmaku(qtbot, controller)
    item = items[0]
    renders: list[float] = []
    monkeypatch.setattr(
        window,
        "_configure_danmaku_for_current_item",
        lambda: renders.append(item.danmaku_offset_seconds),
    )
    window._open_danmaku_source_dialog()

    item.selected_danmaku_provider = "youku"
    window._handle_danmaku_source_task_finished(item, configure_danmaku=True)

    assert item.danmaku_offset_seconds == 2.5
    assert window._danmaku_source_offset_spin.value() == 2.5
    assert renders == [2.5]


def test_danmaku_source_episode_change_restores_episode_offset(qtbot, monkeypatch) -> None:
    items = [
        PlayItem(
            title="第1集",
            url="https://stream.example/1.m3u8",
            danmaku_xml='<i><d p="5,1,25,16777215">one</d></i>',
            selected_danmaku_provider="tencent",
        ),
        PlayItem(
            title="第2集",
            url="https://stream.example/2.m3u8",
            danmaku_xml='<i><d p="5,1,25,16777215">two</d></i>',
            selected_danmaku_provider="tencent",
        ),
    ]
    controller = FakeOffsetDanmakuController(
        {
            ("第1集", "tencent"): -3.0,
            ("第2集", "tencent"): 4.0,
        }
    )
    window, _items = build_window_with_loaded_danmaku(qtbot, controller, items=items)
    window._open_danmaku_source_dialog()
    monkeypatch.setattr(window, "_load_current_item", lambda **_kwargs: None)

    window._play_item_at_index(1)

    assert items[1].danmaku_offset_seconds == 4.0
    assert window._danmaku_source_offset_spin.value() == 4.0


def test_danmaku_source_offset_missing_controller_apis_falls_back_to_zero(qtbot) -> None:
    window, items = build_window_with_loaded_danmaku(qtbot, object())
    items[0].danmaku_offset_seconds = 9.0

    window._open_danmaku_source_dialog()

    assert items[0].danmaku_offset_seconds == 0.0
    assert window._danmaku_source_offset_spin.value() == 0.0


def test_player_window_saves_danmaku_render_mode_from_dialog(qtbot) -> None:
    saved = {"called": 0}
    config = AppConfig()
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("called", saved["called"] + 1),
    )
    qtbot.addWidget(window)

    dialog = window._ensure_danmaku_settings_dialog()
    dialog.show()
    qtbot.waitUntil(lambda: len(visible_danmaku_settings_dialogs()) == 1)
    window._danmaku_render_mode_combo.setCurrentIndex(window._danmaku_render_mode_combo.findData("mixed"))

    assert dialog.windowTitle() == "弹幕设置"
    assert config.preferred_danmaku_render_mode == "mixed"
    assert saved["called"] == 1


def test_player_window_disables_danmaku_position_preset_in_static_mode(qtbot) -> None:
    config = AppConfig()
    config.preferred_danmaku_render_mode = "static"
    window = PlayerWindow(FakePlayerController(), config=config)
    qtbot.addWidget(window)

    dialog = window._ensure_danmaku_settings_dialog()
    dialog.show()
    qtbot.waitUntil(lambda: len(visible_danmaku_settings_dialogs()) == 1)

    assert window._danmaku_position_preset_combo is not None
    assert window._danmaku_position_preset_combo.isEnabled() is False

    window._danmaku_render_mode_combo.setCurrentIndex(window._danmaku_render_mode_combo.findData("mixed"))

    assert window._danmaku_position_preset_combo.isEnabled() is True


def test_player_window_applies_bordered_form_styles_in_danmaku_settings_dialog(qtbot) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig())
    qtbot.addWidget(window)

    dialog = window._ensure_danmaku_settings_dialog()
    dialog.show()
    qtbot.waitUntil(lambda: len(visible_danmaku_settings_dialogs()) == 1)

    tokens = player_window_module.current_theme_manager().tokens_for(player_window_module.current_resolved_theme())

    assert window._danmaku_render_mode_combo is not None
    assert window._danmaku_position_preset_combo is not None
    assert window._danmaku_line_count_spin is not None
    assert window._danmaku_opacity_spin is not None
    assert window._danmaku_scroll_speed_spin is not None
    assert window._danmaku_render_mode_combo.property("flat_combo_border_color") == tokens.input_border
    assert window._danmaku_position_preset_combo.property("flat_combo_disabled_border_color") == tokens.border_subtle
    assert window._danmaku_outline_strength_combo is None
    assert f"border: 1px solid {tokens.input_border};" in window._danmaku_line_count_spin.styleSheet()
    assert f"border: 1px solid {tokens.input_border};" in window._danmaku_opacity_spin.styleSheet()
    assert f"background-color: {tokens.input_bg};" in window._danmaku_scroll_speed_spin.styleSheet()

    window._danmaku_render_mode_combo.setCurrentIndex(window._danmaku_render_mode_combo.findData("static"))

    assert window._danmaku_position_preset_combo.isEnabled() is False
    assert window._danmaku_position_preset_combo.property("flat_combo_disabled_field_bg") == tokens.panel_alt_bg


def test_player_window_uses_uniform_label_widths_in_danmaku_settings_dialog(qtbot) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig())
    qtbot.addWidget(window)

    dialog = window._ensure_danmaku_settings_dialog()
    dialog.show()
    qtbot.waitUntil(lambda: len(visible_danmaku_settings_dialogs()) == 1)

    expected_texts = {
        "显示行数",
        "显示模式",
        "位置预设",
        "颜色模式",
        "统一颜色",
        "文字大小",
        "透明度",
        "滚动速率",
    }
    labels_by_text = {
        label.text(): label
        for label in dialog.findChildren(QLabel)
        if label.text() in expected_texts
    }

    assert set(labels_by_text) == expected_texts
    assert len({label.width() for label in labels_by_text.values()}) == 1
    expected_min_width = max(dialog.fontMetrics().horizontalAdvance(text) for text in expected_texts) + 8
    assert next(iter(labels_by_text.values())).width() >= expected_min_width


def test_player_window_uses_color_palette_button_in_danmaku_settings_dialog(qtbot) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig())
    qtbot.addWidget(window)

    dialog = window._ensure_danmaku_settings_dialog()
    dialog.show()
    qtbot.waitUntil(lambda: len(visible_danmaku_settings_dialogs()) == 1)

    assert window._danmaku_uniform_color_edit is None
    assert isinstance(window._danmaku_uniform_color_button, QPushButton)
    assert window._danmaku_uniform_color_button.text() == "#FFFFFF"
    assert window._danmaku_uniform_color_button.isEnabled() is False

    window._danmaku_color_mode_combo.setCurrentIndex(window._danmaku_color_mode_combo.findData("uniform"))

    assert window._danmaku_uniform_color_button.isEnabled() is True

    window._danmaku_uniform_color_button.click()

    assert window._danmaku_uniform_color_dialog is not None
    assert window._danmaku_uniform_color_dialog.isVisible() is True

    window._danmaku_color_mode_combo.setCurrentIndex(window._danmaku_color_mode_combo.findData("source"))

    assert window._danmaku_uniform_color_button.isEnabled() is False


def test_player_window_saves_advanced_danmaku_settings_from_dialog(qtbot) -> None:
    saved = {"called": 0}
    config = AppConfig()
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("called", saved["called"] + 1),
    )
    qtbot.addWidget(window)

    dialog = window._ensure_danmaku_settings_dialog()
    dialog.show()
    qtbot.waitUntil(lambda: len(visible_danmaku_settings_dialogs()) == 1)

    assert isinstance(window._danmaku_line_count_spin, QSpinBox)
    assert isinstance(window._danmaku_font_size_spin, QSpinBox)
    assert isinstance(window._danmaku_scroll_speed_spin, QDoubleSpinBox)

    window._danmaku_line_count_spin.setValue(8)
    window._danmaku_font_size_spin.setValue(40)
    window._danmaku_scroll_speed_spin.setValue(0.8)

    assert config.preferred_danmaku_line_count == 8
    assert config.preferred_danmaku_font_size == 40
    assert config.preferred_danmaku_scroll_speed == pytest.approx(0.8)
    assert saved["called"] == 3


def test_player_window_saves_and_resets_danmaku_readability_settings(qtbot) -> None:
    saved = {"called": 0}
    config = AppConfig()
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("called", saved["called"] + 1),
    )
    qtbot.addWidget(window)

    dialog = window._ensure_danmaku_settings_dialog()
    dialog.show()
    qtbot.waitUntil(lambda: len(visible_danmaku_settings_dialogs()) == 1)

    assert isinstance(window._danmaku_opacity_spin, QSpinBox)
    window._danmaku_opacity_spin.setValue(63)

    assert config.preferred_danmaku_opacity == 65
    assert window._danmaku_opacity_spin.value() == 65
    assert window._danmaku_outline_strength_combo is None
    assert config.preferred_danmaku_outline_strength == "strong"

    window._restore_default_danmaku_render_settings()

    assert config.preferred_danmaku_opacity == 85
    assert config.preferred_danmaku_outline_strength == "strong"
    assert saved["called"] >= 2


def test_player_window_keypad_zero_disables_active_danmaku(qtbot, monkeypatch) -> None:
    saved = {"called": 0}
    config = AppConfig(preferred_danmaku_enabled=True, preferred_danmaku_line_count=4)
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>',
    )
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("called", saved["called"] + 1),
    )
    qtbot.addWidget(window)
    window.session = session
    window.current_index = 0
    window.danmaku_combo.setEnabled(True)
    window._danmaku_active = True

    cleared = {"called": 0}
    monkeypatch.setattr(window, "_clear_active_danmaku", lambda: cleared.__setitem__("called", cleared["called"] + 1))

    send_key(window, Qt.Key.Key_0, Qt.KeyboardModifier.KeypadModifier, "0")

    assert config.preferred_danmaku_enabled is False
    assert config.preferred_danmaku_line_count == 4
    assert window.danmaku_combo.currentIndex() == 1
    assert cleared["called"] == 1
    assert saved["called"] == 1


def test_player_window_keypad_digit_sets_danmaku_line_count_immediately(qtbot, monkeypatch) -> None:
    saved = {"called": 0}
    config = AppConfig(preferred_danmaku_enabled=False, preferred_danmaku_line_count=1)
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>',
    )
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("called", saved["called"] + 1),
    )
    qtbot.addWidget(window)
    window.session = session
    window.current_index = 0
    window.danmaku_combo.setEnabled(True)

    enabled_line_counts: list[int] = []
    monkeypatch.setattr(window, "_enable_danmaku", lambda line_count: enabled_line_counts.append(line_count))

    send_key(window, Qt.Key.Key_7, Qt.KeyboardModifier.KeypadModifier, "7")

    assert config.preferred_danmaku_enabled is True
    assert config.preferred_danmaku_line_count == 7
    assert window.danmaku_combo.currentIndex() == 8
    assert enabled_line_counts == [7]
    assert saved["called"] == 1


def test_player_window_shows_danmaku_source_option_duration_in_dialog(qtbot) -> None:
    item = PlayItem(
        title="正片",
        url="https://stream.example/movie.m3u8",
        media_title="疯狂动物城2",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[
                    DanmakuSourceOption(
                        provider="tencent",
                        name="疯狂动物城2",
                        url="https://v.qq.com/movie",
                        duration_seconds=5935,
                    )
                ],
            )
        ],
        selected_danmaku_provider="tencent",
        selected_danmaku_url="https://v.qq.com/movie",
        selected_danmaku_title="疯狂动物城2",
        danmaku_search_query="疯狂动物城2",
    )
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="疯狂动物城2"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    window._open_danmaku_source_dialog()

    assert window._danmaku_source_option_list is not None
    assert window._danmaku_source_option_list.count() == 1
    assert window._danmaku_source_option_list.item(0).text() == "疯狂动物城2 · 1:38:55"


def test_player_window_keeps_danmaku_source_option_url_when_duration_is_displayed(qtbot) -> None:
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[
                    DanmakuSourceOption(
                        provider="tencent",
                        name="红果短剧 第1集",
                        url="https://v.qq.com/demo",
                        duration_seconds=1458,
                    )
                ],
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

    assert window._selected_danmaku_source_url_from_dialog() == "https://v.qq.com/demo"


def test_player_window_keeps_title_only_for_unknown_danmaku_source_duration(qtbot) -> None:
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

    assert window._danmaku_source_option_list is not None
    assert window._danmaku_source_option_list.item(0).text() == "红果短剧 第1集"


def test_player_window_opens_danmaku_source_dialog_by_loading_cached_search_result(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.cache_load_calls = 0
            self.refresh_calls = 0

        def load_cached_danmaku_sources(self, item: PlayItem) -> bool:
            self.cache_load_calls += 1
            item.danmaku_search_title = "红果短剧"
            item.danmaku_search_episode = "1集"
            item.danmaku_search_query = "红果短剧 1集"
            item.danmaku_candidates = [
                DanmakuSourceGroup(
                    provider="tencent",
                    provider_label="腾讯",
                    options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
                )
            ]
            item.selected_danmaku_provider = "tencent"
            item.selected_danmaku_url = "https://v.qq.com/demo"
            item.selected_danmaku_title = "红果短剧 第1集"
            return True

        def refresh_danmaku_sources(
            self,
            item: PlayItem,
            query_override: str | None = None,
            force_refresh: bool = False,
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> None:
            self.refresh_calls += 1

    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
    )
    controller = FakeDanmakuController()
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    window._open_danmaku_source_dialog()

    assert controller.cache_load_calls == 1
    assert controller.refresh_calls == 0
    assert window._danmaku_source_title_edit.text() == "红果短剧"
    assert window._danmaku_source_episode_edit.text() == "1集"
    assert window._danmaku_source_provider_list.count() == 1


def test_player_window_reset_danmaku_source_query_restores_default(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, str | None, str | None, list[PlayItem] | None]] = []

        def refresh_danmaku_sources(
            self,
            item: PlayItem,
            query_override: str | None = None,
            search_title_override: str | None = None,
            search_episode_override: str | None = None,
            playlist: list[PlayItem] | None = None,
            force_refresh: bool = False,
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> None:
            self.calls.append((query_override, search_title_override, search_episode_override, playlist))
            item.danmaku_search_title = "红果短剧"
            item.danmaku_search_episode = "1集"
            item.danmaku_search_query = "红果短剧 1集" if query_override is None else query_override
            item.danmaku_search_query_overridden = query_override is not None

    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_search_title="红果短剧 腾讯版",
        danmaku_search_episode="特别篇",
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

    qtbot.waitUntil(
        lambda: window._danmaku_source_title_edit.text() == "红果短剧"
        and window._danmaku_source_episode_edit.text() == "1集"
    )
    assert item.danmaku_search_query_overridden is False
    assert session.danmaku_controller.calls == [(None, None, None, session.playlist)]


def test_player_window_auto_searches_danmaku_sources_when_current_item_is_pending(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, str | None, str | None, list[PlayItem] | None, bool, int, str]] = []

        def refresh_danmaku_sources(
            self,
            item: PlayItem,
            query_override: str | None = None,
            search_title_override: str | None = None,
            search_episode_override: str | None = None,
            playlist: list[PlayItem] | None = None,
            force_refresh: bool = False,
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> None:
            self.calls.append(
                (
                    query_override,
                    search_title_override,
                    search_episode_override,
                    playlist,
                    force_refresh,
                    media_duration_seconds,
                    provider_filter,
                )
            )

    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_search_query="红果短剧 1集",
        danmaku_pending=True,
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

    assert window._danmaku_source_rerun_button is not None
    qtbot.waitUntil(
        lambda: session.danmaku_controller.calls
        == [(None, "红果短剧", None, session.playlist, True, 0, "")]
    )


def test_player_window_allows_danmaku_source_switch_while_current_item_is_pending(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.switch_calls: list[str] = []

        def switch_danmaku_source(self, item: PlayItem, page_url: str) -> str:
            self.switch_calls.append(page_url)
            return ""

    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_pending=True,
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
    controller = FakeDanmakuController()
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    window._open_danmaku_source_dialog()

    assert window._danmaku_source_switch_button is not None
    assert window._danmaku_source_switch_button.isEnabled() is True

    window._switch_current_item_danmaku_source()

    qtbot.waitUntil(lambda: controller.switch_calls == ["https://v.qq.com/demo"])


def test_player_window_rerun_danmaku_search_runs_async_with_force_refresh(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, str | None, str | None, list[PlayItem] | None, bool, int, str]] = []

        def refresh_danmaku_sources(
            self,
            item: PlayItem,
            query_override: str | None = None,
            search_title_override: str | None = None,
            search_episode_override: str | None = None,
            playlist: list[PlayItem] | None = None,
            force_refresh: bool = False,
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> None:
            self.calls.append(
                (
                    query_override,
                    search_title_override,
                    search_episode_override,
                    playlist,
                    force_refresh,
                    media_duration_seconds,
                    provider_filter,
                )
            )
            time.sleep(0.05)
            item.danmaku_search_title = search_title_override or ""
            item.danmaku_search_episode = search_episode_override or ""
            item.danmaku_search_query = " ".join(
                part for part in (item.danmaku_search_title, item.danmaku_search_episode) if part
            ).strip()
            item.danmaku_search_provider = provider_filter
            item.danmaku_candidates = [
                DanmakuSourceGroup(
                    provider="tencent",
                    provider_label="腾讯",
                    options=[DanmakuSourceOption(provider="tencent", name="刷新结果", url="https://v.qq.com/refreshed")],
                )
            ]

    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_search_title="红果短剧",
        danmaku_search_episode="1集",
        danmaku_search_query="红果短剧 1集",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="旧结果", url="https://v.qq.com/old")],
            )
        ],
    )
    controller = FakeDanmakuController()
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    window._open_danmaku_source_dialog()
    assert window._danmaku_source_title_edit is not None
    assert window._danmaku_source_episode_edit is not None
    window._danmaku_source_title_edit.setText("红果短剧 腾讯版")
    window._danmaku_source_episode_edit.setText("2集")

    window._rerun_current_item_danmaku_search()

    assert item.danmaku_pending is True
    assert window._danmaku_source_title_edit.text() == "红果短剧 腾讯版"
    assert window._danmaku_source_episode_edit.text() == "2集"
    assert window._danmaku_source_status_label is not None
    assert window._danmaku_source_status_label.text() == "搜索中（全部）..."
    qtbot.waitUntil(
        lambda: controller.calls == [(None, "红果短剧 腾讯版", "2集", session.playlist, True, 120, "")]
    )
    qtbot.waitUntil(lambda: item.danmaku_pending is False)
    qtbot.waitUntil(lambda: window._danmaku_source_status_label.text() == "")
    qtbot.waitUntil(
        lambda: window._danmaku_source_option_list is not None
        and window._danmaku_source_option_list.count() == 1
        and window._danmaku_source_option_list.item(0).text() == "刷新结果"
    )
    assert window._danmaku_source_option_list.item(0).text() == "刷新结果"


def test_player_window_reset_danmaku_source_query_passes_runtime_duration(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, str | None, str | None, list[PlayItem] | None, int]] = []

        def refresh_danmaku_sources(
            self,
            item: PlayItem,
            query_override: str | None = None,
            search_title_override: str | None = None,
            search_episode_override: str | None = None,
            playlist: list[PlayItem] | None = None,
            force_refresh: bool = False,
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> None:
            self.calls.append((query_override, search_title_override, search_episode_override, playlist, media_duration_seconds))
            item.danmaku_search_title = "红果短剧"
            item.danmaku_search_episode = "1集"
            item.danmaku_search_query = "红果短剧 1集" if query_override is None else query_override
            item.danmaku_search_query_overridden = query_override is not None

    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_search_query="红果短剧 腾讯版",
        danmaku_search_query_overridden=True,
    )
    controller = FakeDanmakuController()
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    window._open_danmaku_source_dialog()
    window._reset_current_item_danmaku_search_query()

    qtbot.waitUntil(lambda: len(controller.calls) >= 2)
    assert controller.calls[-1][:3] == (None, None, None)
    assert controller.calls[-1][3] is session.playlist
    assert controller.calls[-1][4] == 120


def test_player_window_rerun_danmaku_search_passes_selected_provider_filter(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, str | None, str | None, list[PlayItem] | None, bool, int, str]] = []

        def refresh_danmaku_sources(
            self,
            item: PlayItem,
            query_override: str | None = None,
            search_title_override: str | None = None,
            search_episode_override: str | None = None,
            playlist: list[PlayItem] | None = None,
            force_refresh: bool = False,
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ) -> None:
            self.calls.append(
                (
                    query_override,
                    search_title_override,
                    search_episode_override,
                    playlist,
                    force_refresh,
                    media_duration_seconds,
                    provider_filter,
                )
            )
            item.danmaku_search_provider = provider_filter
            item.danmaku_candidates = [
                DanmakuSourceGroup(
                    provider="youku",
                    provider_label="优酷",
                    options=[DanmakuSourceOption(provider="youku", name="优酷结果", url="https://v.youku.com/demo")],
                )
            ]

    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_search_title="红果短剧",
        danmaku_search_episode="1集",
        danmaku_search_query="红果短剧 1集",
    )
    controller = FakeDanmakuController()
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    window._open_danmaku_source_dialog()

    assert window._danmaku_source_search_provider_combo is not None
    assert window._danmaku_source_search_provider_combo.currentText() == "全部"

    target_index = next(
        index
        for index in range(window._danmaku_source_search_provider_combo.count())
        if window._danmaku_source_search_provider_combo.itemData(index) == "youku"
    )
    window._danmaku_source_search_provider_combo.setCurrentIndex(target_index)
    window._rerun_current_item_danmaku_search()

    qtbot.waitUntil(lambda: len(controller.calls) >= 2)
    assert controller.calls[-1][:3] == (None, "红果短剧", "1集")
    assert controller.calls[-1][3] is session.playlist
    assert controller.calls[-1][4:] == (True, 120, "youku")
    assert item.danmaku_search_provider == "youku"


def test_player_window_danmaku_source_dialog_has_episode_url_download_controls(qtbot) -> None:
    item = PlayItem(title="第1集", url="https://media.example/1.m3u8")
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=object(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)

    window._open_danmaku_source_dialog()

    assert window._danmaku_source_url_edit is not None
    assert window._danmaku_source_url_download_button is not None
    assert window._danmaku_source_url_download_button.text() == "下载"
    assert window._danmaku_source_title_edit is not None
    assert window._danmaku_source_episode_edit is not None
    assert (
        window._danmaku_source_url_edit.height()
        == window._danmaku_source_title_edit.height()
        == window._danmaku_source_episode_edit.height()
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "请输入单集链接"),
        ("v.qq.com/x/1", "请输入完整的 http(s) 单集链接"),
        ("https:///x/1", "请输入完整的 http(s) 单集链接"),
    ],
)
def test_player_window_rejects_invalid_episode_url_without_starting_task(
    qtbot,
    value: str,
    message: str,
) -> None:
    class RecordingController:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def download_danmaku_from_url(self, _item: PlayItem, url: str) -> str:
            self.calls.append(url)
            return ""

    controller = RecordingController()
    item = PlayItem(title="第1集", url="https://media.example/1.m3u8")
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)
    window._open_danmaku_source_dialog()
    window._danmaku_source_url_edit.setText(value)

    window._download_current_item_danmaku_url()

    assert controller.calls == []
    assert window._danmaku_source_status_label.text() == message


def test_player_window_downloads_episode_url_and_reloads_danmaku(qtbot, monkeypatch) -> None:
    class BlockingController:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.started = threading.Event()
            self.release = threading.Event()

        def download_danmaku_from_url(self, item: PlayItem, url: str) -> str:
            self.calls.append(url)
            self.started.set()
            assert self.release.wait(timeout=1)
            item.danmaku_xml = '<i><d p="1,1,25,16777215">manual</d></i>'
            item.selected_danmaku_provider = "tencent"
            item.selected_danmaku_url = url
            item.selected_danmaku_title = "第1集"
            return item.danmaku_xml

    controller = BlockingController()
    item = PlayItem(title="第1集", url="https://media.example/1.m3u8")
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)
    renders: list[str] = []
    monkeypatch.setattr(
        window,
        "_configure_danmaku_for_current_item",
        lambda: renders.append(item.danmaku_xml),
    )
    window._open_danmaku_source_dialog()
    window._danmaku_source_url_edit.setText(
        " https://v.qq.com/x/cover/demo/ep1.html "
    )

    window._danmaku_source_url_download_button.click()

    assert controller.started.wait(timeout=1)
    assert item.danmaku_pending is True
    assert window._danmaku_source_url_download_button.isEnabled() is False
    controller.release.set()
    qtbot.waitUntil(
        lambda: window._danmaku_source_url_download_button.isEnabled() is True
    )
    assert item.danmaku_pending is False
    assert renders
    assert all("manual" in xml for xml in renders)
    assert controller.calls == ["https://v.qq.com/x/cover/demo/ep1.html"]
    assert item.selected_danmaku_provider == "tencent"
    assert item.selected_danmaku_url == "https://v.qq.com/x/cover/demo/ep1.html"
    assert window._danmaku_source_url_download_button.isEnabled() is True


def test_player_window_episode_url_enter_starts_download(qtbot) -> None:
    class RecordingController:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def download_danmaku_from_url(self, item: PlayItem, url: str) -> str:
            self.calls.append(url)
            item.danmaku_xml = "<i>manual</i>"
            return item.danmaku_xml

    controller = RecordingController()
    item = PlayItem(title="第1集", url="https://media.example/1.m3u8")
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)
    window._open_danmaku_source_dialog()
    window._danmaku_source_url_edit.setText(
        "https://v.qq.com/x/cover/demo/ep1.html"
    )

    window._danmaku_source_url_edit.returnPressed.emit()

    qtbot.waitUntil(
        lambda: controller.calls == ["https://v.qq.com/x/cover/demo/ep1.html"]
    )


def test_player_window_episode_url_failure_keeps_source_and_shows_status(qtbot) -> None:
    class FailingController:
        def download_danmaku_from_url(self, _item: PlayItem, _url: str) -> str:
            raise RuntimeError("boom")

    item = PlayItem(
        title="第1集",
        url="https://media.example/1.m3u8",
        danmaku_xml="<i>old</i>",
        selected_danmaku_provider="youku",
        selected_danmaku_url="https://youku/old",
        selected_danmaku_title="旧来源",
    )
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=FailingController(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)
    window._open_danmaku_source_dialog()
    window._danmaku_source_url_edit.setText(
        "https://v.qq.com/x/cover/demo/ep1.html"
    )

    window._download_current_item_danmaku_url()

    qtbot.waitUntil(lambda: item.danmaku_pending is False)
    qtbot.waitUntil(
        lambda: window._danmaku_source_status_label.text()
        == "单集链接弹幕下载失败: boom"
    )
    qtbot.waitUntil(
        lambda: "单集链接弹幕下载失败: boom" in window.log_view.toPlainText()
    )
    assert item.danmaku_xml == "<i>old</i>"
    assert item.selected_danmaku_provider == "youku"
    assert item.selected_danmaku_url == "https://youku/old"
    assert item.selected_danmaku_title == "旧来源"


def test_player_window_danmaku_search_provider_combo_includes_builtin_sources(qtbot) -> None:
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_search_title="红果短剧",
        danmaku_search_episode="1集",
        danmaku_search_query="红果短剧 1集",
    )
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=object(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    window._open_danmaku_source_dialog()

    assert window._danmaku_source_search_provider_combo is not None
    assert [
        (
            str(window._danmaku_source_search_provider_combo.itemData(index) or ""),
            window._danmaku_source_search_provider_combo.itemText(index),
        )
        for index in range(window._danmaku_source_search_provider_combo.count())
    ] == [
        ("", "全部"),
        ("tencent", "腾讯"),
        ("youku", "优酷"),
        ("bilibili", "B站"),
        ("iqiyi", "爱奇艺"),
        ("mgtv", "芒果"),
        ("sohu", "搜狐"),
        ("migu", "咪咕"),
        ("renren", "人人"),
    ]


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

    qtbot.waitUntil(lambda: item.danmaku_xml != "")
    assert item.selected_danmaku_url == "https://v.qq.com/demo"
    assert "ok" in item.danmaku_xml


def test_player_window_clear_danmaku_button_removes_loaded_danmaku_and_keeps_source_selection(qtbot) -> None:
    class DanmakuRecordingVideo(RecordingVideo):
        def __init__(self) -> None:
            super().__init__()
            self.removed_subtitle_tracks: list[int | None] = []

        def remove_subtitle_track(self, track_id: int | None) -> None:
            self.removed_subtitle_tracks.append(track_id)

    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>',
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
    window.video = DanmakuRecordingVideo()
    window._danmaku_track_id = 7
    window._danmaku_active = True

    window.open_session(session)
    window._open_danmaku_source_dialog()

    assert window._danmaku_source_clear_button is not None
    assert window._danmaku_source_clear_button.isEnabled() is True

    window._clear_current_item_danmaku_source()

    assert item.danmaku_xml == ""
    assert item.selected_danmaku_url == "https://v.qq.com/demo"
    assert item.selected_danmaku_provider == "tencent"
    assert item.selected_danmaku_title == "红果短剧 第1集"
    assert window.video.removed_subtitle_tracks == [7]
    assert window._danmaku_source_clear_button.isEnabled() is False
    assert window._danmaku_source_switch_button is not None
    assert window._danmaku_source_switch_button.isEnabled() is True


def test_player_window_manual_danmaku_source_switch_logs_failure_without_raising(qtbot) -> None:
    class FakeDanmakuController:
        def switch_danmaku_source(self, item: PlayItem, page_url: str) -> str:
            time.sleep(0.05)
            raise RuntimeError("switch boom")

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

    window.open_session(session)
    window._open_danmaku_source_dialog()

    window._switch_current_item_danmaku_source()

    assert item.danmaku_pending is True
    assert window._danmaku_source_status_label is not None
    assert window._danmaku_source_status_label.text() == "下载中（腾讯）..."
    qtbot.waitUntil(lambda: item.danmaku_pending is False)
    qtbot.waitUntil(lambda: window._danmaku_source_status_label.text() == "")
    qtbot.waitUntil(lambda: "弹幕切换失败: switch boom" in window.log_view.toPlainText())


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
    window.video = RecordingVideo()
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
    assert window.playlist_group_combo.isHidden() is True
    assert window.playlist_source_combo.isHidden() is True
    assert window.playlist_group_combo.count() == 1
    assert window.playlist_group_combo.itemText(0) == "网盘线(夸克)"
    assert window.video.load_calls == []
    assert "播放失败: 没有可用的播放地址: 查看" in window.log_view.toPlainText()


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


def test_player_window_prepares_non_proxy_live_streams(qtbot) -> None:
    class FakeM3U8AdFilter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def should_prepare(self, url: str) -> bool:
            return ".m3u8" in url

        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            self.calls.append((url, dict(headers or {})))
            return "http://127.0.0.1:2323/m3u?v=proxy-live-1"

    session = PlayerSession(
        vod=VodItem(vod_id="live-1", vod_name="Live", detail_style="live"),
        playlist=[
            PlayItem(
                title="线路 1",
                url="https://media.example/live/index.m3u8",
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
    qtbot.waitUntil(lambda: video.load_calls == [("http://127.0.0.1:2323/m3u?v=proxy-live-1", 0)])

    assert filter_service.calls == [
        (
            "https://media.example/live/index.m3u8",
            {"Referer": "https://site.example"},
        )
    ]


def test_player_window_skips_proxy_prepare_for_live_extensionless_streams(qtbot) -> None:
    class FakeM3U8AdFilter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def should_prepare(self, url: str) -> bool:
            return True

        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            self.calls.append((url, dict(headers or {})))
            return "http://127.0.0.1:2323/m3u?v=proxy-live-2"

    session = PlayerSession(
        vod=VodItem(vod_id="live-2", vod_name="Live", detail_style="live"),
        playlist=[
            PlayItem(
                title="线路 1",
                url="http://192.168.50.60:4567/p/web/0@128440",
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
    qtbot.waitUntil(lambda: video.load_calls == [("http://192.168.50.60:4567/p/web/0@128440", 0)])

    assert filter_service.calls == []


def test_player_window_skips_proxy_prepare_for_live_backend_proxy_with_extra_ids(qtbot) -> None:
    class FakeM3U8AdFilter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def should_prepare(self, url: str) -> bool:
            return True

        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            self.calls.append((url, dict(headers or {})))
            return "http://127.0.0.1:2323/m3u?v=proxy-live-3"

    session = PlayerSession(
        vod=VodItem(vod_id="live-3", vod_name="Live", detail_style="live"),
        playlist=[
            PlayItem(
                title="线路 1",
                url="http://media.example/p/demo-token/12@3456@78@90",
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
    qtbot.waitUntil(lambda: video.load_calls == [("http://media.example/p/demo-token/12@3456@78@90", 0)])

    assert filter_service.calls == []


def test_player_window_rewrites_remote_iso_to_local_proxy_url(qtbot) -> None:
    class FakeM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return url.endswith(".iso")

        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            return "http://127.0.0.1:2323/iso/test/BDMV/STREAM/00080.m2ts"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="正片", url="http://media.example/disc.iso")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    video = RecordingVideo()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=FakeM3U8AdFilter())
    qtbot.addWidget(window)
    window.video = video

    window.open_session(session)

    qtbot.waitUntil(lambda: video.load_calls == [("http://127.0.0.1:2323/iso/test/BDMV/STREAM/00080.m2ts", 0)])


def test_player_window_rewrites_direct_parse_result_to_local_proxy_url(qtbot) -> None:
    class FakeM3U8AdFilter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def should_prepare(self, url: str) -> bool:
            return ".m3u8" in url

        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            self.calls.append((url, dict(headers or {})))
            return "http://127.0.0.1:2323/m3u?v=proxy-direct-1"

    def load_item(item: PlayItem) -> None:
        item.url = "https://api.hls.one:4433/Cache/qq/parsed.m3u8?vkey=demo"
        item.original_url = "https://jx.xmflv.com/?url=https://v.qq.com/x/cover/demo/vid123.html"
        item.headers = {"Referer": "https://jx.xmflv.com/"}
        item.parse_required = True

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="解析播放",
                url="",
                original_url="https://jx.xmflv.com/?url=https://v.qq.com/x/cover/demo/vid123.html",
                vod_id="https://v.qq.com/x/cover/demo/vid123.html",
                parse_required=True,
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    session.playback_loader = load_item
    filter_service = FakeM3U8AdFilter()
    video = RecordingVideo()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=filter_service)
    qtbot.addWidget(window)
    window.video = video

    window.open_session(session)

    qtbot.waitUntil(lambda: video.load_calls == [("http://127.0.0.1:2323/m3u?v=proxy-direct-1", 0)])
    assert filter_service.calls == [
        (
            "https://api.hls.one:4433/Cache/qq/parsed.m3u8?vkey=demo",
            {"Referer": "https://jx.xmflv.com/"},
        )
    ]
    assert session.playlist[0].original_url == "https://api.hls.one:4433/Cache/qq/parsed.m3u8?vkey=demo"


def test_player_window_rewrites_dash_data_uri_to_local_proxy_url(qtbot) -> None:
    class FakeM3U8AdFilter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def should_prepare(self, url: str) -> bool:
            return url.startswith("data:application/dash+xml;base64,")

        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            self.calls.append((url, dict(headers or {})))
            return "http://127.0.0.1:2323/dash/proxy-dash-1.mpd"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="data:application/dash+xml;base64,PE1QRD48L01QRD4=",
                headers={"Referer": "https://www.bilibili.com/"},
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
    qtbot.waitUntil(lambda: video.load_calls == [("http://127.0.0.1:2323/dash/proxy-dash-1.mpd", 0)])

    assert filter_service.calls == [
        (
            "data:application/dash+xml;base64,PE1QRD48L01QRD4=",
            {"Referer": "https://www.bilibili.com/"},
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


def test_player_window_does_not_fallback_to_direct_iso_on_prepare_failure(qtbot) -> None:
    class FailingM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return url.endswith(".iso")

        def prepare(self, url: str, headers: dict[str, str] | None = None) -> str:
            raise ValueError("远程 ISO 不是受支持的 Blu-ray 目录结构")

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="正片", url="http://media.example/disc.iso")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    video = RecordingVideo()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=FailingM3U8AdFilter())
    qtbot.addWidget(window)
    window.video = video

    window.open_session(session)
    qtbot.waitUntil(lambda: "远程 ISO 不是受支持的 Blu-ray 目录结构" in window.log_view.toPlainText())

    assert video.load_calls == []


def test_player_window_populates_dash_video_quality_options_after_prepare(qtbot) -> None:
    class FakeM3U8AdFilter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str], str | None]] = []

        def should_prepare(self, url: str) -> bool:
            return url.startswith("data:application/dash+xml;base64,")

        def prepare(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            dash_video_id: str | None = None,
        ) -> str:
            self.calls.append((url, dict(headers or {}), dash_video_id))
            return "http://127.0.0.1:2323/dash/proxy-dash-1.mpd"

        def dash_video_qualities(self, prepared_url: str) -> list[VideoQualityOption]:
            assert prepared_url == "http://127.0.0.1:2323/dash/proxy-dash-1.mpd"
            return [
                VideoQualityOption(id="v1080", label="1080P AVC 2.8 Mbps", width=1920, height=1080, bandwidth=2800000),
                VideoQualityOption(id="v720", label="720P AVC 1.2 Mbps", width=1280, height=720, bandwidth=1200000),
            ]

        def selected_dash_video_quality(self, prepared_url: str) -> str | None:
            assert prepared_url == "http://127.0.0.1:2323/dash/proxy-dash-1.mpd"
            return "v1080"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="data:application/dash+xml;base64,PE1QRD48L01QRD4=",
                headers={"Referer": "https://www.bilibili.com/"},
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
    qtbot.waitUntil(lambda: video.load_calls == [("http://127.0.0.1:2323/dash/proxy-dash-1.mpd", 0)])

    assert filter_service.calls == [
        (
            "data:application/dash+xml;base64,PE1QRD48L01QRD4=",
            {"Referer": "https://www.bilibili.com/"},
            None,
        )
    ]
    assert [window.video_quality_combo.itemData(index) for index in range(window.video_quality_combo.count())] == [
        "v1080",
        "v720",
    ]
    assert window.video_quality_combo.currentData() == "v1080"
    assert window.video_quality_combo.isEnabled() is True


def test_player_window_populates_spider_video_quality_options(qtbot) -> None:
    class PassThroughM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return False

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://media.example/video-1080.m3u8",
                playback_qualities=[
                    VideoQualityOption(id="1080p", label="1080P", url="https://media.example/video-1080.m3u8"),
                    VideoQualityOption(id="720p", label="720P", url="https://media.example/video-720.m3u8"),
                ],
                selected_playback_quality_id="1080p",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=PassThroughM3U8AdFilter())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    assert [window.video_quality_combo.itemData(index) for index in range(window.video_quality_combo.count())] == [
        "1080p",
        "720p",
    ]
    assert window.video_quality_combo.currentData() == "1080p"
    assert window.video_quality_combo.isEnabled() is True


def test_player_window_switches_spider_video_quality_with_position_and_pause_preserved(qtbot) -> None:
    class PassThroughM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return False

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
        ) -> None:
            del headers, poster_image_path
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 93

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://media.example/video-1080.m3u8",
                playback_qualities=[
                    VideoQualityOption(id="1080p", label="1080P", url="https://media.example/video-1080.m3u8"),
                    VideoQualityOption(id="720p", label="720P", url="https://media.example/video-720.m3u8"),
                ],
                selected_playback_quality_id="1080p",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=PassThroughM3U8AdFilter())
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    assert video.load_calls == [("https://media.example/video-1080.m3u8", False, 0)]

    window.is_playing = False
    window.video_quality_combo.setCurrentIndex(1)

    qtbot.waitUntil(lambda: len(video.load_calls) == 2)
    assert video.load_calls[-1] == ("https://media.example/video-720.m3u8", True, 93)
    assert session.playlist[0].selected_playback_quality_id == "720p"


def test_player_window_switches_ytdlp_quality_via_selected_ytdl_format_with_position_and_pause_preserved(qtbot) -> None:
    class PassThroughM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return False

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int, str]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
            ytdl_format: str = "",
        ) -> None:
            del headers, poster_image_path
            self.load_calls.append((url, pause, start_seconds, ytdl_format))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

        def position_seconds(self) -> int:
            return 93

    loader_calls: list[str] = []

    def playback_loader(item: PlayItem) -> None:
        loader_calls.append(item.selected_playback_quality_id)
        item.url = "https://www.youtube.com/watch?v=test123"
        item.audio_url = ""
        item.ytdl_format = "299+140"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://www.youtube.com/watch?v=test123",
                original_url="https://www.youtube.com/watch?v=test123",
                playback_qualities=[
                    VideoQualityOption(id="ytdlp_2160", label="2160p", ytdl_format="401+140"),
                    VideoQualityOption(id="ytdlp_1080", label="1080p", ytdl_format="299+140"),
                    VideoQualityOption(id="ytdlp_720", label="720p", ytdl_format="298+140"),
                ],
                selected_playback_quality_id="ytdlp_1080",
                ytdl_format="299+140",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        async_playback_loader=True,
    )
    session.playback_loader = playback_loader

    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=PassThroughM3U8AdFilter())
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    qtbot.waitUntil(lambda: len(video.load_calls) == 1)
    assert video.load_calls == [("https://www.youtube.com/watch?v=test123", False, 0, "299+140")]
    assert loader_calls == ["ytdlp_1080"]

    window.is_playing = False
    window.video_quality_combo.setCurrentIndex(2)

    qtbot.waitUntil(lambda: len(video.load_calls) == 2)
    assert video.load_calls[-1] == ("https://www.youtube.com/watch?v=test123", True, 93, "298+140")
    assert loader_calls == ["ytdlp_1080"]
    assert session.playlist[0].selected_playback_quality_id == "ytdlp_720"


def test_player_window_switches_ytdlp_quality_via_loader_when_audio_track_is_selected(qtbot) -> None:
    class PassThroughM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return False

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int, str]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
            ytdl_format: str = "",
        ) -> None:
            del headers, poster_image_path
            self.load_calls.append((url, pause, start_seconds, ytdl_format))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 93

    loader_calls: list[tuple[str, str]] = []

    def playback_loader(item: PlayItem) -> None:
        loader_calls.append((item.selected_playback_quality_id, item.selected_audio_track_id))
        item.url = "https://www.youtube.com/watch?v=test123"
        suffix = "141" if item.selected_audio_track_id == "ytdlp_audio_zh_141" else "140"
        height = (item.selected_playback_quality_id or "ytdlp_1080").removeprefix("ytdlp_")
        item.audio_url = ""
        item.ytdl_format = f"{'299' if height == '1080' else '298'}+{suffix}"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://www.youtube.com/watch?v=test123",
                original_url="https://www.youtube.com/watch?v=test123",
                playback_qualities=[
                    VideoQualityOption(id="ytdlp_1080", label="1080p", ytdl_format="299+140"),
                    VideoQualityOption(id="ytdlp_720", label="720p", ytdl_format="298+140"),
                ],
                selected_playback_quality_id="ytdlp_1080",
                ytdl_format="299+141",
                audio_tracks=[
                    YtdlpAudioTrackOption(id="ytdlp_audio_en_140", label="English Original", lang="en", is_original=True),
                    YtdlpAudioTrackOption(id="ytdlp_audio_zh_141", label="中文配音", lang="zh"),
                ],
                selected_audio_track_id="ytdlp_audio_zh_141",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    session.playback_loader = playback_loader

    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=PassThroughM3U8AdFilter())
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    assert loader_calls == [("ytdlp_1080", "ytdlp_audio_zh_141")]
    assert video.load_calls == [("https://www.youtube.com/watch?v=test123", False, 0, "299+141")]

    window.is_playing = False
    window.video_quality_combo.setCurrentIndex(1)

    assert loader_calls == [
        ("ytdlp_1080", "ytdlp_audio_zh_141"),
        ("ytdlp_720", "ytdlp_audio_zh_141"),
    ]


def test_player_window_does_not_warm_up_mpv_while_async_playback_loader_resolves(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.warm_up_calls = 0
            self.load_calls: list[str] = []

        def warm_up_async(self) -> None:
            self.warm_up_calls += 1

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
        ) -> None:
            del pause, start_seconds, headers
            self.load_calls.append(url)

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    release_loader = threading.Event()

    def playback_loader(item: PlayItem) -> None:
        assert release_loader.wait(timeout=2)
        item.url = "https://media.example/1.mp4"

    session = PlayerSession(
        vod=VodItem(vod_id="yt:video:abc123", vod_name="YouTube"),
        playlist=[PlayItem(title="正片", url="", vod_id="yt:video:abc123")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        async_playback_loader=True,
    )
    session.playback_loader = playback_loader

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    qtbot.wait(800)
    assert video.warm_up_calls == 0
    release_loader.set()
    qtbot.waitUntil(lambda: len(video.load_calls) == 1)
    assert video.warm_up_calls == 0
    assert video.load_calls == ["https://media.example/1.mp4"]


def test_player_window_skips_mpv_warmup_when_async_playback_loader_returns_quickly(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.warm_up_calls = 0
            self.load_calls: list[str] = []

        def warm_up_async(self) -> None:
            self.warm_up_calls += 1

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
        ) -> None:
            del pause, start_seconds, headers
            self.load_calls.append(url)

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    def playback_loader(item: PlayItem) -> None:
        item.url = "https://media.example/1.mp4"

    session = PlayerSession(
        vod=VodItem(vod_id="yt:video:abc123", vod_name="YouTube"),
        playlist=[PlayItem(title="正片", url="", vod_id="yt:video:abc123")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        async_playback_loader=True,
    )
    session.playback_loader = playback_loader

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    qtbot.waitUntil(lambda: len(video.load_calls) == 1)
    qtbot.wait(800)
    assert video.warm_up_calls == 0


def test_player_window_preloads_ytdlp_passthrough_without_redundant_metadata_hydration(qtbot) -> None:
    class PassThroughM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return False

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, str]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
            ytdl_format: str = "",
        ) -> None:
            del pause, start_seconds, headers, poster_image_path
            self.load_calls.append((url, ytdl_format))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    loader_calls: list[str] = []
    hydration_calls: list[str] = []

    def playback_loader(item: PlayItem) -> None:
        if item.url == "https://www.youtube.com/watch?v=abc123xyz89":
            loader_calls.append(item.url)
            item.url = "https://stream.test/1080/video.mp4"
            item.audio_url = "https://stream.test/audio.webm"
            item.ytdl_format = ""
            item.playback_qualities = [VideoQualityOption(id="ytdlp_1080", label="1080p", ytdl_format="299+140")]
            return
        hydration_calls.append(item.url)
        item.audio_tracks = [
            YtdlpAudioTrackOption(id="ytdlp_audio_en_140", label="English", lang="en", format_id="140")
        ]
        item.external_subtitles = [
            ExternalSubtitleOption(
                name="English [yt-dlp]",
                lang="en",
                url="https://sub.example/en.vtt",
                format="vtt",
                source="ytdlp",
            )
        ]

    session = PlayerSession(
        vod=VodItem(vod_id="abc123xyz89", vod_name="YouTube"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://www.youtube.com/watch?v=abc123xyz89",
                original_url="https://www.youtube.com/watch?v=abc123xyz89",
                ytdl_format="bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        async_playback_loader=True,
    )
    session.playback_loader = playback_loader

    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=PassThroughM3U8AdFilter())
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    qtbot.waitUntil(lambda: loader_calls == ["https://www.youtube.com/watch?v=abc123xyz89"])
    qtbot.waitUntil(lambda: video.load_calls == [("https://stream.test/1080/video.mp4", "")])
    window._handle_video_file_loaded()
    assert window._pending_ytdlp_metadata_hydration is None
    window._start_pending_ytdlp_metadata_hydration_if_current()
    assert hydration_calls == []


def test_player_window_switches_resolved_ytdlp_quality_via_loader_instead_of_page_url(qtbot) -> None:
    class PassThroughM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return False

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int, str]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
            ytdl_format: str = "",
        ) -> None:
            del headers, poster_image_path
            self.load_calls.append((url, pause, start_seconds, ytdl_format))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 93

    loader_calls: list[str] = []

    def playback_loader(item: PlayItem) -> None:
        loader_calls.append(item.selected_playback_quality_id)
        suffix = (item.selected_playback_quality_id or "ytdlp_1080").removeprefix("ytdlp_")
        item.url = f"https://stream.test/{suffix}/playlist.m3u8"
        item.audio_url = ""
        item.ytdl_format = ""
        item.playback_qualities = [
            VideoQualityOption(id="ytdlp_1080", label="1080p", ytdl_format="299+140"),
            VideoQualityOption(id="ytdlp_720", label="720p", ytdl_format="298+140"),
        ]

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://stream.test/1080/playlist.m3u8",
                original_url="https://www.youtube.com/watch?v=test123",
                playback_qualities=[
                    VideoQualityOption(id="ytdlp_1080", label="1080p", ytdl_format="299+140"),
                    VideoQualityOption(id="ytdlp_720", label="720p", ytdl_format="298+140"),
                ],
                selected_playback_quality_id="ytdlp_1080",
                ytdl_format="",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    session.playback_loader = playback_loader

    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=PassThroughM3U8AdFilter())
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    assert video.load_calls == [("https://stream.test/1080/playlist.m3u8", False, 0, "")]
    assert loader_calls == ["ytdlp_1080"]

    window.is_playing = False
    window.video_quality_combo.setCurrentIndex(1)

    assert video.load_calls[-1] == ("https://stream.test/720/playlist.m3u8", True, 93, "")
    assert loader_calls == ["ytdlp_1080", "ytdlp_720"]
    assert session.playlist[0].selected_playback_quality_id == "ytdlp_720"


def test_player_window_switches_ytdlp_quality_via_direct_resolved_url_without_reloading(qtbot) -> None:
    class PassThroughM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return False

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int, str]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
            ytdl_format: str = "",
        ) -> None:
            del headers, poster_image_path
            self.load_calls.append((url, pause, start_seconds, ytdl_format))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 93

    loader_calls: list[str] = []

    def playback_loader(item: PlayItem) -> None:
        loader_calls.append(item.selected_playback_quality_id)
        item.url = "https://stream.test/1080/playlist.m3u8"
        item.audio_url = ""
        item.ytdl_format = ""
        item.playback_qualities = [
            VideoQualityOption(
                id="ytdlp_1080",
                label="1080p",
                url="https://stream.test/1080/playlist.m3u8",
                ytdl_format="299+140",
            ),
            VideoQualityOption(
                id="ytdlp_720",
                label="720p",
                url="https://stream.test/720/playlist.m3u8",
                ytdl_format="298+140",
            ),
        ]

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://stream.test/1080/playlist.m3u8",
                original_url="https://www.youtube.com/watch?v=test123",
                playback_qualities=[
                    VideoQualityOption(
                        id="ytdlp_1080",
                        label="1080p",
                        url="https://stream.test/1080/playlist.m3u8",
                        ytdl_format="299+140",
                    ),
                    VideoQualityOption(
                        id="ytdlp_720",
                        label="720p",
                        url="https://stream.test/720/playlist.m3u8",
                        ytdl_format="298+140",
                    ),
                ],
                selected_playback_quality_id="ytdlp_1080",
                ytdl_format="",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    session.playback_loader = playback_loader

    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=PassThroughM3U8AdFilter())
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    assert video.load_calls == [("https://stream.test/1080/playlist.m3u8", False, 0, "")]
    assert loader_calls == ["ytdlp_1080"]

    window.is_playing = False
    window.video_quality_combo.setCurrentIndex(1)

    assert video.load_calls[-1] == ("https://stream.test/720/playlist.m3u8", True, 93, "")
    assert loader_calls == ["ytdlp_1080"]
    assert session.playlist[0].original_url == "https://www.youtube.com/watch?v=test123"
    assert session.playlist[0].selected_playback_quality_id == "ytdlp_720"


def test_player_window_does_not_prepare_ytdlp_page_url_after_loader_resolves_direct_media(qtbot) -> None:
    class RecordingM3U8AdFilter:
        def __init__(self) -> None:
            self.should_prepare_calls: list[str] = []
            self.prepare_calls: list[str] = []

        def should_prepare(self, url: str) -> bool:
            self.should_prepare_calls.append(url)
            return True

        def prepare(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            dash_video_id: str | None = None,
        ) -> str:
            del headers, dash_video_id
            self.prepare_calls.append(url)
            return url

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
            audio_files: str = "",
        ) -> None:
            del headers, poster_image_path, audio_files
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    def playback_loader(item: PlayItem) -> None:
        item.url = "https://www.youtube.com/watch?v=test123"
        item.audio_url = ""
        item.ytdl_format = "299+140"
        item.playback_qualities = [VideoQualityOption(id="ytdlp_1080", label="1080P", ytdl_format="299+140")]
        item.selected_playback_quality_id = "ytdlp_1080"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="",
                original_url="https://www.youtube.com/watch?v=test123",
                vod_id="https://www.youtube.com/watch?v=test123",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    session.playback_loader = playback_loader

    ad_filter = RecordingM3U8AdFilter()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=ad_filter)
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    assert ad_filter.should_prepare_calls == []
    assert ad_filter.prepare_calls == []


def test_player_window_does_not_proxy_youtube_resolved_hls_url_without_quality_marker(qtbot) -> None:
    class RecordingM3U8AdFilter:
        def __init__(self) -> None:
            self.should_prepare_calls: list[str] = []
            self.prepare_calls: list[str] = []

        def should_prepare(self, url: str) -> bool:
            self.should_prepare_calls.append(url)
            return ".m3u8" in url

        def prepare(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            dash_video_id: str | None = None,
        ) -> str:
            del headers, dash_video_id
            self.prepare_calls.append(url)
            return "http://127.0.0.1:2323/m3u?v=unexpected-youtube-proxy"

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
            audio_files: str = "",
        ) -> None:
            del headers, poster_image_path, audio_files
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    resolved_hls_url = "https://manifest.googlevideo.com/playlist/index.m3u8"

    def playback_loader(item: PlayItem) -> None:
        item.url = resolved_hls_url
        item.original_url = "https://www.youtube.com/watch?v=test123"
        item.headers = {"Referer": "https://www.youtube.com/"}

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie", detail_style="youtube"),
        playlist=[
            PlayItem(
                title="正片",
                url="",
                original_url="https://www.youtube.com/watch?v=test123",
                vod_id="https://www.youtube.com/watch?v=test123",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    session.playback_loader = playback_loader

    ad_filter = RecordingM3U8AdFilter()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=ad_filter)
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    qtbot.waitUntil(lambda: len(video.load_calls) == 1)
    assert video.load_calls == [(resolved_hls_url, False, 0)]
    assert ad_filter.should_prepare_calls == []
    assert ad_filter.prepare_calls == []


def test_player_window_skips_dash_prepare_for_ytdlp_separate_stream_urls(qtbot) -> None:
    class RecordingM3U8AdFilter:
        def __init__(self) -> None:
            self.should_prepare_calls: list[str] = []
            self.prepare_calls: list[str] = []

        def should_prepare(self, url: str) -> bool:
            self.should_prepare_calls.append(url)
            return True

        def prepare(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            dash_video_id: str | None = None,
        ) -> str:
            del headers, dash_video_id
            self.prepare_calls.append(url)
            return url

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int, str]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
            audio_files: str = "",
        ) -> None:
            del headers, poster_image_path
            self.load_calls.append((url, pause, start_seconds, audio_files))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    def playback_loader(item: PlayItem) -> None:
        item.url = "https://stream.test/video-1080-avc.mp4"
        item.audio_url = "https://stream.test/audio-140.m4a"
        item.ytdl_format = ""
        item.playback_qualities = [VideoQualityOption(id="ytdlp_1080", label="1080P")]
        item.selected_playback_quality_id = "ytdlp_1080"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="",
                original_url="https://www.youtube.com/watch?v=test123",
                vod_id="https://www.youtube.com/watch?v=test123",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    session.playback_loader = playback_loader

    ad_filter = RecordingM3U8AdFilter()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=ad_filter)
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    assert video.load_calls == [("https://stream.test/video-1080-avc.mp4", False, 0, "https://stream.test/audio-140.m4a")]
    assert ad_filter.should_prepare_calls == []
    assert ad_filter.prepare_calls == []


def test_player_window_prepares_ytdlp_dash_data_uri_after_loader_resolves_separate_streams(qtbot) -> None:
    class RecordingM3U8AdFilter:
        def __init__(self) -> None:
            self.should_prepare_calls: list[str] = []
            self.prepare_calls: list[str] = []

        def should_prepare(self, url: str) -> bool:
            self.should_prepare_calls.append(url)
            return url.startswith("data:application/dash+xml;base64,")

        def prepare(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            dash_video_id: str | None = None,
        ) -> str:
            del headers, dash_video_id
            self.prepare_calls.append(url)
            return "http://127.0.0.1:2323/dash/ytdlp-1080.mpd"

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
            audio_files: str = "",
        ) -> None:
            del headers, poster_image_path, audio_files
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    def playback_loader(item: PlayItem) -> None:
        item.url = "data:application/dash+xml;base64,PE1QRD48L01QRD4="
        item.audio_url = ""
        item.playback_qualities = [VideoQualityOption(id="ytdlp_1080", label="1080P")]
        item.selected_playback_quality_id = "ytdlp_1080"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="",
                original_url="https://www.youtube.com/watch?v=test123",
                vod_id="https://www.youtube.com/watch?v=test123",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    session.playback_loader = playback_loader

    ad_filter = RecordingM3U8AdFilter()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=ad_filter)
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    qtbot.waitUntil(lambda: len(video.load_calls) == 1)
    assert video.load_calls == [("http://127.0.0.1:2323/dash/ytdlp-1080.mpd", False, 0)]
    assert ad_filter.should_prepare_calls == ["data:application/dash+xml;base64,PE1QRD48L01QRD4="]
    assert ad_filter.prepare_calls == ["data:application/dash+xml;base64,PE1QRD48L01QRD4="]


def test_player_window_does_not_rehydrate_ytdlp_item_just_because_it_has_no_subtitles(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    item = PlayItem(
        title="YouTube",
        url="https://manifest.googlevideo.com/api/manifest/hls_playlist/test.m3u8",
        original_url="https://www.youtube.com/watch?v=test123",
        playback_qualities=[VideoQualityOption(id="ytdlp_1080", label="1080P")],
        audio_tracks=[YtdlpAudioTrackOption(id="ytdlp_audio_en_251", label="English", lang="en")],
        external_subtitles=[],
    )

    assert window._should_delay_ytdlp_metadata_hydration(item) is False


def test_player_window_switches_ytdlp_dash_quality_using_original_page_url(qtbot) -> None:
    class RecordingM3U8AdFilter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def should_prepare(self, url: str) -> bool:
            return url.startswith("data:application/dash+xml;base64,")

        def prepare(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            dash_video_id: str | None = None,
        ) -> str:
            del headers
            self.calls.append((url, dash_video_id))
            suffix = (dash_video_id or "ytdlp_1080").removeprefix("ytdlp_")
            return f"http://127.0.0.1:2323/dash/ytdlp-{suffix}.mpd"

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
            audio_files: str = "",
        ) -> None:
            del headers, poster_image_path, audio_files
            self.load_calls.append((url, pause, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 93

    source_urls_seen: list[str] = []

    def playback_loader(item: PlayItem) -> None:
        source_urls_seen.append(item.original_url)
        item.url = "data:application/dash+xml;base64,PE1QRD48L01QRD4="
        item.audio_url = ""
        item.playback_qualities = [
            VideoQualityOption(id="ytdlp_1080", label="1080P"),
            VideoQualityOption(id="ytdlp_720", label="720P"),
        ]
        item.selected_playback_quality_id = item.selected_playback_quality_id or "ytdlp_1080"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="",
                original_url="https://www.youtube.com/watch?v=test123",
                vod_id="https://www.youtube.com/watch?v=test123",
                playback_qualities=[
                    VideoQualityOption(id="ytdlp_1080", label="1080P"),
                    VideoQualityOption(id="ytdlp_720", label="720P"),
                ],
                selected_playback_quality_id="ytdlp_1080",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    session.playback_loader = playback_loader

    ad_filter = RecordingM3U8AdFilter()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=ad_filter)
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    qtbot.waitUntil(lambda: len(video.load_calls) == 1)
    assert video.load_calls == [("http://127.0.0.1:2323/dash/ytdlp-1080.mpd", False, 0)]
    qtbot.waitUntil(lambda: window.video_quality_combo.count() == 2)
    assert source_urls_seen == ["https://www.youtube.com/watch?v=test123"]
    assert session.playlist[0].original_url == "https://www.youtube.com/watch?v=test123"

    window.is_playing = False
    window.video_quality_combo.setCurrentIndex(1)

    qtbot.waitUntil(lambda: len(video.load_calls) == 2)
    assert source_urls_seen == [
        "https://www.youtube.com/watch?v=test123",
        "https://www.youtube.com/watch?v=test123",
    ]
    assert video.load_calls[-1] == ("http://127.0.0.1:2323/dash/ytdlp-1080.mpd", True, 93)
    assert session.playlist[0].selected_playback_quality_id == "ytdlp_720"


def test_player_window_switches_dash_video_quality_with_position_and_pause_preserved(qtbot) -> None:
    class FakeM3U8AdFilter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str], str | None]] = []
            self.selected_by_url = {
                "http://127.0.0.1:2323/dash/proxy-dash-1080.mpd": "v1080",
                "http://127.0.0.1:2323/dash/proxy-dash-720.mpd": "v720",
            }

        def should_prepare(self, url: str) -> bool:
            return url.startswith("data:application/dash+xml;base64,")

        def prepare(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            dash_video_id: str | None = None,
        ) -> str:
            self.calls.append((url, dict(headers or {}), dash_video_id))
            selected = dash_video_id or "v1080"
            return f"http://127.0.0.1:2323/dash/proxy-dash-{selected.removeprefix('v')}.mpd"

        def dash_video_qualities(self, prepared_url: str) -> list[VideoQualityOption]:
            return [
                VideoQualityOption(id="v1080", label="1080P AVC 2.8 Mbps", width=1920, height=1080, bandwidth=2800000),
                VideoQualityOption(id="v720", label="720P AVC 1.2 Mbps", width=1280, height=720, bandwidth=1200000),
            ]

        def selected_dash_video_quality(self, prepared_url: str) -> str | None:
            return self.selected_by_url[prepared_url]

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
            return 93

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="data:application/dash+xml;base64,PE1QRD48L01QRD4=",
                headers={"Referer": "https://www.bilibili.com/"},
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    filter_service = FakeM3U8AdFilter()
    video = FakeVideo()
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=filter_service)
    qtbot.addWidget(window)
    window.video = video

    window.open_session(session)
    qtbot.waitUntil(lambda: video.load_calls == [("http://127.0.0.1:2323/dash/proxy-dash-1080.mpd", False, 0)])
    window.is_playing = False

    window.video_quality_combo.setCurrentIndex(1)

    qtbot.waitUntil(lambda: len(video.load_calls) == 2)
    assert filter_service.calls == [
        (
            "data:application/dash+xml;base64,PE1QRD48L01QRD4=",
            {"Referer": "https://www.bilibili.com/"},
            None,
        ),
        (
            "data:application/dash+xml;base64,PE1QRD48L01QRD4=",
            {"Referer": "https://www.bilibili.com/"},
            "v720",
        ),
    ]
    assert video.load_calls[-1] == ("http://127.0.0.1:2323/dash/proxy-dash-720.mpd", True, 93)
    assert window.video_quality_combo.currentData() == "v720"


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
    from atv_player.ui.theme import ThemeManager, install_theme

    app = QApplication.instance() or QApplication([])
    manager = ThemeManager(system_theme_getter=lambda: "light")
    install_theme(app, manager, "light")

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    tokens = manager.tokens_for("light")
    assert window.details is not None
    assert window.metadata_section is not None
    assert window.log_section is not None
    assert window.metadata_view.isReadOnly() is True
    assert window.log_view.isReadOnly() is True
    assert window.details.layout().indexOf(window.metadata_section) != -1
    assert window.details.layout().indexOf(window.log_section) != -1
    assert window.metadata_section.layout().indexOf(window.metadata_view) != -1
    assert window.log_section.layout().indexOf(window.log_view) != -1
    assert "QListWidget::item:selected" in window.playlist.styleSheet()
    assert "QTabBar::tab:selected" in window.playlist_title_tabs.styleSheet()
    assert "padding: 12px 14px" in window.metadata_view.styleSheet()
    assert "padding: 10px 12px" in window.log_view.styleSheet()
    assert tokens.accent in window.metadata_heading.styleSheet()
    assert tokens.accent in window.log_heading.styleSheet()


def test_player_window_limits_playback_log_height_to_one_quarter_of_details(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    qtbot.waitExposed(window)

    window.details.resize(window.details.width(), 480)
    QApplication.processEvents()
    expected = max(window.details.height() // 4, 1)

    assert window.log_section.maximumHeight() == expected

    window.details.resize(window.details.width(), 640)
    QApplication.processEvents()
    expected = max(window.details.height() // 4, 1)

    assert window.log_section.maximumHeight() == expected


def test_player_window_styles_playlist_items_by_playback_state(qtbot) -> None:
    from atv_player.ui.theme import ThemeManager, install_theme

    app = QApplication.instance() or QApplication([])
    manager = ThemeManager(system_theme_getter=lambda: "light")
    install_theme(app, manager, "light")

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1))

    tokens = manager.tokens_for("light")
    previous_item = window.playlist.item(0)
    current_item = window.playlist.item(1)
    upcoming_item = window.playlist.item(2)

    assert previous_item.foreground().color().name() == QColor(tokens.text_secondary).name()
    assert current_item.foreground().color().name() == QColor(tokens.accent).name()
    assert current_item.font().bold() is True
    assert upcoming_item.foreground().color().name() == QColor(tokens.text_primary).name()


def test_player_window_marks_current_playlist_item_with_play_indicator(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1))

    assert window.playlist.item(0).icon().isNull() is True
    assert window.playlist.item(1).icon().isNull() is False
    assert window.playlist.item(2).icon().isNull() is True

    window.current_index = 2
    window.playlist.setCurrentRow(2)
    window._sync_playlist_item_styles()

    assert window.playlist.item(1).icon().isNull() is True
    assert window.playlist.item(2).icon().isNull() is False


def test_player_window_uses_compact_playlist_item_density(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.playlist.spacing() == 1
    assert "min-height: 24px" in window.playlist.styleSheet()
    assert "padding: 4px 8px" in window.playlist.styleSheet()


def test_player_window_uses_smaller_player_combos_and_disabled_state_styles(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.subtitle_combo.setEnabled(False)
    window._apply_theme()

    manager = player_window_module.current_theme_manager()
    theme = player_window_module.current_resolved_theme()
    player_tokens = manager.player_tokens_for(theme)

    assert "min-height: 28px" in window.speed_combo.styleSheet()
    assert (
        "QComboBox {\n        height: 28px;\n        min-height: 28px;\n        max-height: 28px;\n        padding: 0 18px 0 4px;\n        border: none;"
        in window.speed_combo.styleSheet()
    )
    assert window.speed_combo.property("flat_combo_height") == 28
    assert "background: transparent;" in window.speed_combo.styleSheet()
    assert "#ffffff" not in window.speed_combo.styleSheet()
    assert "background: transparent;" in window.subtitle_combo.styleSheet()
    assert "QComboBox:disabled" in window.subtitle_combo.styleSheet()
    assert "QComboBox:disabled::drop-down" in window.subtitle_combo.styleSheet()
    popup_qss = window.speed_combo.view().styleSheet()
    assert f"background: {player_tokens.player_button_bg};" in popup_qss
    assert f"color: {player_tokens.player_text_on_dark};" in popup_qss
    assert f"selection-background-color: {player_tokens.player_button_hover_bg};" in popup_qss


def test_player_window_dark_theme_player_combo_uses_enabled_border_and_muted_disabled_state(qtbot) -> None:
    from atv_player.ui.theme import ThemeManager, install_theme

    app = QApplication.instance() or QApplication([])
    manager = ThemeManager(system_theme_getter=lambda: "dark")
    install_theme(app, manager, "dark")

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.subtitle_combo.setEnabled(False)
    window._apply_theme()

    player_tokens = manager.player_tokens_for("dark")

    assert window.speed_combo.property("flat_combo_border_color") == player_tokens.player_button_border
    assert window.subtitle_combo.property("flat_combo_disabled_text_color") == player_tokens.player_button_border
    assert window.subtitle_combo.property("flat_combo_disabled_arrow_color") == player_tokens.player_button_border
    assert window.subtitle_combo.property("flat_combo_disabled_field_bg") == player_tokens.player_button_pressed_bg
    assert window.subtitle_combo.property("flat_combo_disabled_border_color") == "transparent"
    assert f"background: {player_tokens.player_button_pressed_bg};" in window.subtitle_combo.styleSheet()


def test_player_window_dialog_provider_combos_use_bordered_theme_after_creation(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="深空彼岸", vod_year="2026"),
        playlist=[
            PlayItem(
                title="第1集",
                url="https://stream.example/1.m3u8",
                media_title="深空彼岸",
                danmaku_search_title="深空彼岸",
                danmaku_search_episode="1集",
                danmaku_search_query="深空彼岸 1集",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=object(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._open_metadata_scrape_dialog()
    window._open_danmaku_source_dialog()

    tokens = player_window_module.current_theme_manager().tokens_for(player_window_module.current_resolved_theme())

    assert window._metadata_scrape_category_combo is not None
    assert window._metadata_scrape_provider_combo is not None
    assert window._danmaku_source_search_provider_combo is not None
    assert window._metadata_scrape_category_combo.property("flat_combo_border_color") == tokens.input_border
    assert window._metadata_scrape_provider_combo.property("flat_combo_border_color") == tokens.input_border
    assert window._danmaku_source_search_provider_combo.property("flat_combo_border_color") == tokens.input_border
    assert window._metadata_scrape_category_combo.property("flat_combo_disabled_border_color") == tokens.border_subtle
    assert window._metadata_scrape_provider_combo.property("flat_combo_disabled_border_color") == tokens.border_subtle
    assert window._danmaku_source_search_provider_combo.property("flat_combo_disabled_border_color") == tokens.border_subtle
    assert window._metadata_scrape_title_edit.height() == 42
    assert window._metadata_scrape_year_edit.height() == 42
    assert window._metadata_scrape_category_combo.property("flat_combo_height") == 42
    assert window._metadata_scrape_provider_combo.property("flat_combo_height") == 42
    assert window._metadata_scrape_title_edit.y() == window._metadata_scrape_category_combo.y()
    assert window._metadata_scrape_year_edit.y() == window._metadata_scrape_category_combo.y()
    assert window._metadata_scrape_category_combo.y() == window._metadata_scrape_provider_combo.y()
    assert window._danmaku_source_title_edit.height() == 42
    assert window._danmaku_source_episode_edit.height() == 42
    assert window._danmaku_source_search_provider_combo.height() == 42
    assert window._danmaku_source_title_edit.y() == window._danmaku_source_search_provider_combo.y()
    assert window._danmaku_source_episode_edit.y() == window._danmaku_source_search_provider_combo.y()


def test_player_window_keeps_sidebar_route_combos_readable_on_light_surfaces(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window._apply_theme()

    tokens = player_window_module.current_theme_manager().tokens_for(player_window_module.current_resolved_theme())

    combo_qss = window.playlist_group_combo.styleSheet()
    assert "height: 34px;" in combo_qss
    assert "min-height: 34px;" in combo_qss
    assert "max-height: 34px;" in combo_qss
    assert "padding: 0 40px 0 12px;" in combo_qss
    assert "border: none;" in combo_qss
    assert "border-radius: 14px;" in combo_qss
    assert f"background: {tokens.input_bg};" in combo_qss
    assert f"color: {tokens.text_primary};" in combo_qss
    popup_qss = window.playlist_group_combo.view().styleSheet()
    assert f"background: {tokens.menu_bg};" in popup_qss
    assert f"color: {tokens.text_primary};" in popup_qss


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
    assert [window.playlist_group_combo.itemText(i) for i in range(window.playlist_group_combo.count())] == ["备用线(2)", "极速线(2)"]
    assert [window.playlist.item(i).text() for i in range(window.playlist.count())] == ["第1集", "第2集"]
    assert window.playlist.currentRow() == 1

    window.playlist_group_combo.setCurrentIndex(1)

    assert window.session is not None
    assert window.session.playlist_index == 1
    assert [item.title for item in window.session.playlist] == ["第1集", "第2集"]
    assert window.playlist.currentRow() == 1
    assert video.load_calls[-1][0] == "http://b/2.m3u8"


def test_player_window_renders_two_level_source_selectors_and_switches_group(qtbot) -> None:
    parse1 = [PlayItem(title="第1集", url="http://parse/1.m3u8", play_source="解析1")]
    baidu1 = [
        PlayItem(title="第1集", url="http://baidu1/1.m3u8", play_source="百度1"),
        PlayItem(title="第2集", url="http://baidu1/2.m3u8", play_source="百度1"),
    ]
    baidu2 = [
        PlayItem(title="第1集", url="http://baidu2/1.m3u8", play_source="百度2"),
        PlayItem(title="第2集", url="http://baidu2/2.m3u8", play_source="百度2"),
    ]
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="红果短剧"),
        playlist=baidu2,
        playlists=[parse1, baidu1, baidu2],
        playlist_index=2,
        source_groups=[
            PlaybackSourceGroup(label="解析", sources=[PlaybackSource(label="解析1", playlist=parse1)]),
            PlaybackSourceGroup(
                label="百度",
                sources=[
                    PlaybackSource(label="百度1", playlist=baidu1),
                    PlaybackSource(label="百度2", playlist=baidu2),
                ],
            ),
        ],
        source_group_index=1,
        source_index=1,
        start_index=1,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    assert [window.playlist_group_combo.itemText(i) for i in range(window.playlist_group_combo.count())] == ["解析", "百度"]
    assert [window.playlist_source_combo.itemText(i) for i in range(window.playlist_source_combo.count())] == ["百度1", "百度2"]
    assert window.playlist.currentRow() == 1

    window.playlist_group_combo.setCurrentIndex(0)

    assert window.session is not None
    assert window.session.source_group_index == 0
    assert window.session.source_index == 0
    assert window.current_index == 0
    assert window.video.load_calls[-1][0] == "http://parse/1.m3u8"


def test_player_window_preserves_plugin_groups_when_drive_resource_adds_subdirectories(qtbot) -> None:
    baidu = [PlayItem(title="百度", url="", vod_id="https://pan.baidu.com/s/demo", play_source="百度")]
    quark = [PlayItem(title="夸克", url="", vod_id="https://pan.quark.cn/s/demo", play_source="夸克")]
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘合集"),
        playlist=baidu,
        playlists=[baidu, quark],
        source_groups=[
            PlaybackSourceGroup(label="百度", sources=[PlaybackSource(label="百度资源", playlist=baidu)]),
            PlaybackSourceGroup(label="夸克", sources=[PlaybackSource(label="夸克资源", playlist=quark)]),
        ],
        source_group_index=0,
        source_index=0,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    first_dir = [PlayItem(title="01.mp4", url="http://baidu/1.mp4", play_source="第一季")]
    second_dir: list[PlayItem] = []
    load_result = PlaybackLoadResult(
        replacement_playlist=first_dir,
        source_groups=[
            PlaybackSourceGroup(label="第一季", sources=[PlaybackSource(label="第一季", playlist=first_dir)]),
            PlaybackSourceGroup(
                label="第二季",
                sources=[PlaybackSource(label="第二季", playlist=second_dir)],
                drive_dir_id="dir-2",
            ),
        ],
        playlists=[first_dir, second_dir],
        drive_resource_id="resource-1",
        drive_files_loader=lambda resource_id, dir_id: [
            {"name": "02.mp4", "url": "http://baidu/2.mp4", "path": "/第二季/02.mp4"}
        ],
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)

    window._apply_playback_loader_result(load_result)

    assert [window.playlist_group_combo.itemText(i) for i in range(window.playlist_group_combo.count())] == ["百度", "夸克"]
    assert [window.playlist_source_combo.itemText(i) for i in range(window.playlist_source_combo.count())] == ["百度资源"]
    assert [window.playlist_subgroup_combo.itemText(i) for i in range(window.playlist_subgroup_combo.count())] == ["第一季", "第二季"]
    assert [item.title for item in window.session.playlist] == ["01.mp4"]

    window.playlist_subgroup_combo.setCurrentIndex(1)

    assert window.session.source_group_index == 0
    assert window.session.source_index == 0
    assert [item.title for item in window.session.playlist] == ["02.mp4"]
    assert window.video.load_calls[-1][0] == "http://baidu/2.mp4"


def test_player_window_restores_nested_drive_subdirectory_from_history_url(qtbot) -> None:
    drive_entry = [PlayItem(title="百度", url="", vod_id="https://pan.baidu.com/s/demo", play_source="百度")]
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘合集"),
        playlist=drive_entry,
        playlists=[drive_entry],
        source_groups=[PlaybackSourceGroup(label="百度", sources=[PlaybackSource(label="百度资源", playlist=drive_entry)])],
        start_index=0,
        start_position_seconds=45,
        speed=1.25,
        resume_history=HistoryRecord(
            id=0,
            key="plugin-1",
            vod_name="网盘合集",
            vod_pic="",
            vod_remarks="02.mp4",
            episode=0,
            episode_url="http://baidu/2.mp4",
            position=45000,
            opening=0,
            ending=0,
            speed=1.25,
            create_time=0,
        ),
    )
    first_dir = [PlayItem(title="01.mp4", url="http://baidu/1.mp4", play_source="第一季")]
    second_dir: list[PlayItem] = []
    load_calls: list[str] = []
    load_result = PlaybackLoadResult(
        replacement_playlist=first_dir,
        source_groups=[
            PlaybackSourceGroup(label="第一季", sources=[PlaybackSource(label="第一季", playlist=first_dir)]),
            PlaybackSourceGroup(
                label="第二季",
                sources=[PlaybackSource(label="第二季", playlist=second_dir)],
                drive_dir_id="dir-2",
            ),
        ],
        playlists=[first_dir, second_dir],
        drive_resource_id="resource-1",
        drive_files_loader=lambda resource_id, dir_id: load_calls.append(dir_id) or [
            {"name": "02.mp4", "url": "http://baidu/2.mp4", "path": "/第二季/02.mp4"}
        ],
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)

    window._apply_playback_loader_result(load_result)

    assert window.session is not None
    assert window.session.source_groups[0].sources[0].subgroup_index == 1
    assert window.current_index == 0
    assert [item.title for item in window.session.playlist] == ["02.mp4"]
    assert load_calls == ["dir-2"]


def test_player_window_restores_cross_device_drive_history_by_route_and_play_id(qtbot) -> None:
    drive_entry = [PlayItem(title="百度", url="", vod_id="https://pan.baidu.com/s/demo")]
    session = PlayerSession(
        vod=VodItem(vod_id="173", vod_name="凡人修仙传"),
        playlist=drive_entry,
        playlists=[drive_entry],
        source_groups=[
            PlaybackSourceGroup(
                label="百度",
                sources=[PlaybackSource(label="百度资源", playlist=drive_entry)],
            )
        ],
        start_index=0,
        start_position_seconds=45,
        speed=1.0,
        resume_history=HistoryRecord(
            id=0,
            key="173",
            vod_name="凡人修仙传",
            vod_pic="",
            vod_remarks="S01E126",
            episode=1,
            episode_url="1@185535@6@1",
            position=45000,
            opening=0,
            ending=0,
            speed=1.0,
            create_time=0,
            source_subgroup_index=6,
            source_subgroup_name="07外海风云",
            drive_dir_id="base64-from-another-device",
        ),
    )
    first_route = [
        PlayItem(
            title="S01E01.mp4",
            url="http://atb/p/token/1@185401",
            play_id="1@185401",
        )
    ]
    groups = [
        PlaybackSourceGroup(
            label=f"{index + 1:02d}线路",
            sources=[
                PlaybackSource(
                    label=f"{index + 1:02d}线路",
                    playlist=first_route if index == 0 else [],
                )
            ],
            drive_dir_id=f"local-dir-{index}",
        )
        for index in range(10)
    ]
    load_result = PlaybackLoadResult(
        replacement_playlist=first_route,
        source_groups=groups,
        playlists=[first_route] + [[] for _ in groups[1:]],
        drive_resource_id="resource-1",
        drive_files_loader=lambda resource_id, dir_id: [
            {
                "name": "S01E125.mp4",
                "url": "http://atb/p/token/1@185534",
                "playId": "1@185534",
            },
            {
                "name": "S01E126.mp4",
                "url": "http://atb/p/token/1@185535",
                "playId": "1@185535",
            },
        ] if dir_id == "local-dir-6" else [],
    )
    groups[6].label = "07外海风云"
    groups[6].sources[0].label = "07外海风云"
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)

    window._apply_playback_loader_result(load_result)

    assert window.session is not None
    assert window.session.source_groups[0].sources[0].subgroup_index == 6
    assert window.current_index == 1
    assert window.session.playlist[1].play_id == "1@185535"


def test_player_window_auto_advance_loads_next_drive_subdirectory(qtbot) -> None:
    first_dir = [PlayItem(title="01.mp4", url="http://baidu/1.mp4", play_source="第一季")]
    second_dir: list[PlayItem] = []
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘合集"),
        playlist=first_dir,
        playlists=[first_dir, second_dir],
        source_groups=[
            PlaybackSourceGroup(label="第一季", sources=[PlaybackSource(label="第一季", playlist=first_dir)]),
            PlaybackSourceGroup(
                label="第二季",
                sources=[PlaybackSource(label="第二季", playlist=second_dir)],
                drive_dir_id="dir-2",
            ),
        ],
        source_group_index=0,
        source_index=0,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        drive_resource_id="resource-1",
        drive_files_loader=lambda resource_id, dir_id: [
            {"name": "02.mp4", "url": "http://baidu/2.mp4", "path": "/第二季/02.mp4"}
        ],
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)

    window._update_playback_observation(position=119, duration=120)
    window._handle_playback_finished()

    assert window.session is not None
    assert window.session.source_group_index == 1
    assert window.current_index == 0
    assert [item.title for item in window.session.playlist] == ["02.mp4"]
    assert window.video.load_calls[-1][0] == "http://baidu/2.mp4"


def test_player_window_switch_drive_group_reanchors_metadata_scrape_binding_title(qtbot) -> None:
    # 网盘合集里每个目录是一部剧：切到某目录后再刮削，绑定应挂在"重开该目录时
    # 用于查询的标题"（目录清洗名）下，而不是打开合集时的分享标题。
    first_dir = [
        PlayItem(
            title="01.mp4",
            url="http://baidu/1.mp4",
            play_source="等不到说我爱你（82集）崔秀子",
            media_title="等不到说我爱你（82集）崔秀子",
        )
    ]
    second_dir: list[PlayItem] = []
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="等不到说我爱你（82集）崔秀子"),
        playlist=first_dir,
        playlists=[first_dir, second_dir],
        source_groups=[
            PlaybackSourceGroup(
                label="等不到说我爱你（82集）崔秀子",
                sources=[PlaybackSource(label="等不到说我爱你（82集）崔秀子", playlist=first_dir)],
            ),
            PlaybackSourceGroup(
                label="凡人修仙传（126集）",
                sources=[PlaybackSource(label="凡人修仙传（126集）", playlist=second_dir)],
                drive_dir_id="dir-2",
            ),
        ],
        source_group_index=0,
        source_index=0,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        drive_resource_id="resource-1",
        drive_files_loader=lambda resource_id, dir_id: [
            {"name": "S01E126.mp4", "url": "http://atb/p/token/1@185535", "path": "/凡人修仙传/S01E126.mp4"}
        ],
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)

    assert window._metadata_scrape_binding_title == "等不到说我爱你（82集）崔秀子"

    window._change_playlist_group(1)

    assert window.session is not None
    assert window.session.source_group_index == 1
    assert window._metadata_scrape_binding_title == "凡人修仙传"


def test_player_window_auto_advance_loads_next_nested_drive_subdirectory(qtbot) -> None:
    drive_entry = [PlayItem(title="百度", url="", vod_id="https://pan.baidu.com/s/demo", play_source="百度")]
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘合集"),
        playlist=drive_entry,
        playlists=[drive_entry],
        source_groups=[
            PlaybackSourceGroup(label="百度", sources=[PlaybackSource(label="百度资源", playlist=drive_entry)])
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    first_dir = [PlayItem(title="01.mp4", url="http://baidu/1.mp4", play_source="第一季")]
    second_dir: list[PlayItem] = []
    load_result = PlaybackLoadResult(
        replacement_playlist=first_dir,
        source_groups=[
            PlaybackSourceGroup(label="第一季", sources=[PlaybackSource(label="第一季", playlist=first_dir)]),
            PlaybackSourceGroup(
                label="第二季",
                sources=[PlaybackSource(label="第二季", playlist=second_dir)],
                drive_dir_id="dir-2",
            ),
        ],
        playlists=[first_dir, second_dir],
        drive_resource_id="resource-1",
        drive_files_loader=lambda resource_id, dir_id: [
            {"name": "02.mp4", "url": "http://baidu/2.mp4", "path": "/第二季/02.mp4"}
        ],
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)
    window._apply_playback_loader_result(load_result)

    window._update_playback_observation(position=119, duration=120)
    window._handle_playback_finished()

    assert window.session is not None
    source = window.session.source_groups[0].sources[0]
    assert source.subgroup_index == 1
    assert window.current_index == 0
    assert [item.title for item in window.session.playlist] == ["02.mp4"]
    assert window.video.load_calls[-1][0] == "http://baidu/2.mp4"


def test_player_window_switches_leaf_source_and_keeps_episode_index_when_possible(qtbot) -> None:
    first = [
        PlayItem(title="第1集", url="http://q1/1.m3u8", play_source="夸克1"),
        PlayItem(title="第2集", url="http://q1/2.m3u8", play_source="夸克1"),
    ]
    second = [
        PlayItem(title="第1集", url="http://q2/1.m3u8", play_source="夸克2"),
        PlayItem(title="第2集", url="http://q2/2.m3u8", play_source="夸克2"),
    ]
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘剧集"),
        playlist=first,
        playlists=[first, second],
        playlist_index=0,
        source_groups=[
            PlaybackSourceGroup(
                label="夸克",
                sources=[
                    PlaybackSource(label="夸克1", playlist=first),
                    PlaybackSource(label="夸克2", playlist=second),
                ],
            )
        ],
        source_group_index=0,
        source_index=0,
        start_index=1,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    window.playlist_source_combo.setCurrentIndex(1)

    assert window.session is not None
    assert window.session.source_index == 1
    assert window.current_index == 1
    assert window.video.load_calls[-1][0] == "http://q2/2.m3u8"


def test_player_window_switching_leaf_source_resets_danmaku_prefetch_state(qtbot) -> None:
    first = [
        PlayItem(title="第1集", url="http://q1/1.m3u8", play_source="夸克1"),
        PlayItem(title="第2集", url="http://q1/2.m3u8", play_source="夸克1"),
    ]
    second = [
        PlayItem(title="第1集", url="http://q2/1.m3u8", play_source="夸克2"),
        PlayItem(title="第2集", url="http://q2/2.m3u8", play_source="夸克2"),
    ]
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘剧集"),
        playlist=first,
        playlists=[first, second],
        playlist_index=0,
        source_groups=[
            PlaybackSourceGroup(
                label="夸克",
                sources=[
                    PlaybackSource(label="夸克1", playlist=first),
                    PlaybackSource(label="夸克2", playlist=second),
                ],
            )
        ],
        source_group_index=0,
        source_index=0,
        start_index=1,
        start_position_seconds=0,
        speed=1.0,
    )
    controller = PrefetchResetRecordingPlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    window.playlist_source_combo.setCurrentIndex(1)

    assert controller.reset_calls == [session]


def test_player_window_auto_switches_to_next_source_when_first_open_fails(qtbot) -> None:
    first = [PlayItem(title="第1集", url="", vod_id="line-1", play_source="线路1")]
    second = [PlayItem(title="第1集", url="http://line2/1.m3u8", play_source="线路2")]
    session = PlayerSession(
        vod=VodItem(vod_id="vod-1", vod_name="短剧"),
        playlist=first,
        playlists=[first, second],
        playlist_index=0,
        source_groups=[
            PlaybackSourceGroup(
                label="默认组",
                sources=[
                    PlaybackSource(label="线路1", playlist=first),
                    PlaybackSource(label="线路2", playlist=second),
                ],
            )
        ],
        source_group_index=0,
        source_index=0,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(playback_auto_switch_source_on_failure=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    assert window.session is not None
    assert window.session.source_index == 1
    assert window.video.load_calls[-1][0] == "http://line2/1.m3u8"
    assert "播放失败，自动切换线路" in window.log_view.toPlainText()


def test_player_window_auto_switches_to_first_source_of_next_group_when_current_group_is_exhausted(qtbot) -> None:
    first = [PlayItem(title="第1集", url="", vod_id="line-1", play_source="组1线路1")]
    second = [PlayItem(title="第1集", url="http://group2/1.m3u8", play_source="组2线路1")]
    session = PlayerSession(
        vod=VodItem(vod_id="vod-2", vod_name="剧集"),
        playlist=first,
        playlists=[first, second],
        playlist_index=0,
        source_groups=[
            PlaybackSourceGroup(label="组1", sources=[PlaybackSource(label="组1线路1", playlist=first)]),
            PlaybackSourceGroup(label="组2", sources=[PlaybackSource(label="组2线路1", playlist=second)]),
        ],
        source_group_index=0,
        source_index=0,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(playback_auto_switch_source_on_failure=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    assert window.session is not None
    assert window.session.source_group_index == 1
    assert window.session.source_index == 0
    assert window.video.load_calls[-1][0] == "http://group2/1.m3u8"


def test_player_window_stops_on_failed_startup_when_auto_switch_has_no_remaining_sources(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(vod_id="vod-3", vod_name="单线路"),
        playlist=[PlayItem(title="第1集", url="", vod_id="line-1", play_source="线路1")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(playback_auto_switch_source_on_failure=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    assert window.session is not None
    assert window.session.source_index == 0
    assert window.video.load_calls == []
    assert window._startup_state.stage.value == "failed"


def test_player_window_does_not_auto_switch_when_playback_has_already_started(qtbot) -> None:
    first = [PlayItem(title="第1集", url="http://line1/1.m3u8", play_source="线路1")]
    second = [PlayItem(title="第1集", url="http://line2/1.m3u8", play_source="线路2")]
    session = PlayerSession(
        vod=VodItem(vod_id="vod-4", vod_name="已开播"),
        playlist=first,
        playlists=[first, second],
        playlist_index=0,
        source_groups=[
            PlaybackSourceGroup(
                label="默认组",
                sources=[
                    PlaybackSource(label="线路1", playlist=first),
                    PlaybackSource(label="线路2", playlist=second),
                ],
            )
        ],
        source_group_index=0,
        source_index=0,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(playback_auto_switch_source_on_failure=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    window._handle_video_picture_state_changed("visible")
    window._handle_playback_failed("播放失败: HTTP 403 Forbidden")

    assert window.session is not None
    assert window.session.source_index == 0
    assert "播放失败: HTTP 403 Forbidden" in window.log_view.toPlainText()


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


def test_player_window_playlist_items_show_full_title_in_tooltip(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    video = RecordingVideo()
    window.video = video
    long_title = "和AI玩猜历史人物游戏，又被它给耍了 - 超长标题完整版"
    session = PlayerSession(
        vod=VodItem(vod_id="BV1ebREBmEha", vod_name="历史人物"),
        playlist=[PlayItem(title=long_title, url="http://b/1.m3u8", play_source="BiliBili")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    assert window.playlist.count() == 1
    assert window.playlist.item(0).text() == long_title
    assert window.playlist.item(0).toolTip() == long_title


def test_player_window_rewritten_episode_title_uses_original_filename_for_tooltip(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    video = RecordingVideo()
    window.video = video
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="示例剧集"),
        playlist=[
            PlayItem(
                title="S01E01.mkv",
                original_title="S01E01.mkv",
                episode_display_title="第1集 星门初启",
                url="http://b/1.m3u8",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    assert window.playlist.item(0).text() == "第1集 星门初启"
    assert window.playlist.item(0).toolTip() == "S01E01.mkv"


def test_player_window_playlist_keeps_file_size_when_episode_title_is_rewritten(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="示例剧集"),
        playlist=[
            PlayItem(
                title="S01E01.mkv(1.25 GB)",
                original_title="S01E01.mkv(1.25 GB)",
                episode_display_title="第1集 星门初启",
                url="http://b/1.m3u8",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    assert window.playlist.item(0).text() == "第1集 星门初启 (1.25 GB)"


def test_player_window_places_poster_widget_above_metadata_and_log_views(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    details_layout = window.details.layout()
    metadata_layout = window.metadata_section.layout()

    assert window.poster_label is not None
    assert details_layout.indexOf(window.metadata_section) < details_layout.indexOf(window.log_section)
    assert metadata_layout.indexOf(window._poster_row_widget) != -1
    assert metadata_layout.indexOf(window._poster_row_widget) < metadata_layout.indexOf(window.metadata_view)
    assert window._poster_row_layout.indexOf(window.poster_label) != -1
    assert window.poster_label.alignment() == Qt.AlignmentFlag.AlignCenter
    assert window.poster_label.minimumHeight() > 0


def test_player_window_sidebar_actions_include_poster_toggle(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    actions_layout = window.sidebar_actions_widget.layout()

    assert actions_layout.count() == 4
    assert actions_layout.indexOf(window.toggle_poster_button) != -1
    assert window.toggle_poster_button.text() == ""
    assert window.toggle_poster_button.toolTip() == "海报"
    assert window.toggle_poster_button.isCheckable() is True
    assert window.toggle_poster_button.isChecked() is True
    assert window._poster_row_widget.isHidden() is False

    qtbot.mouseClick(window.toggle_poster_button, Qt.MouseButton.LeftButton)

    assert window.toggle_poster_button.isChecked() is False
    assert window._poster_row_widget.isHidden() is True
    assert window.metadata_section.isHidden() is False
    assert window.log_section.isHidden() is False

    qtbot.mouseClick(window.toggle_poster_button, Qt.MouseButton.LeftButton)

    assert window.toggle_poster_button.isChecked() is True
    assert window._poster_row_widget.isHidden() is False


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


def test_player_window_clicking_detail_poster_opens_large_preview(qtbot, tmp_path) -> None:
    poster_path = tmp_path / "poster.png"
    pixmap = QPixmap(240, 360)
    pixmap.fill(QColor("blue"))
    assert pixmap.save(str(poster_path)) is True

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="九寨沟", vod_pic=str(poster_path)),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    qtbot.mouseClick(window.poster_label, Qt.MouseButton.LeftButton)

    assert window._poster_preview_dialog is not None
    assert window._poster_preview_dialog.windowTitle() == "封面预览"
    assert window._poster_preview_dialog.isVisible() is True
    assert window._poster_preview_dialog.title_bar().maximize_button.isHidden() is True
    margins = window._poster_preview_dialog.content_layout().contentsMargins()
    assert margins.left() <= 4
    assert margins.top() <= 4
    assert margins.right() <= 4
    assert margins.bottom() <= 4
    assert window._poster_preview_label is not None
    preview = window._poster_preview_label.pixmap()
    assert preview is not None
    assert preview.isNull() is False
    assert preview.size().width() >= window.poster_label.pixmap().size().width() * 4
    assert preview.size().height() >= window.poster_label.pixmap().size().height() * 4


def test_player_window_large_preview_can_switch_between_poster_candidates(qtbot, tmp_path) -> None:
    main_poster = tmp_path / "main.png"
    alt_poster = tmp_path / "alt.png"
    main_pixmap = QPixmap(240, 360)
    main_pixmap.fill(QColor("blue"))
    alt_pixmap = QPixmap(240, 360)
    alt_pixmap.fill(QColor("green"))
    assert main_pixmap.save(str(main_poster)) is True
    assert alt_pixmap.save(str(alt_poster)) is True

    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="Movie",
            vod_pic=str(main_poster),
            poster_candidates=[str(main_poster), str(alt_poster)],
        ),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    qtbot.mouseClick(window.poster_label, Qt.MouseButton.LeftButton)
    assert window._poster_preview_next_button is not None
    first_preview = window._poster_preview_label.pixmap()
    assert first_preview is not None

    qtbot.mouseClick(window._poster_preview_next_button, Qt.MouseButton.LeftButton)

    assert session.current_metadata_poster_index == 1
    assert window._preferred_detail_poster_source() == str(alt_poster)
    switched_preview = window._poster_preview_label.pixmap()
    assert switched_preview is not None
    assert switched_preview.cacheKey() != first_preview.cacheKey()
    assert window.poster_label.pixmap().cacheKey() != first_preview.cacheKey()


def test_player_window_prefers_session_poster_before_default_video_cover(qtbot, tmp_path) -> None:
    session_poster = tmp_path / "session.png"
    pixmap = QPixmap(24, 36)
    pixmap.fill(QColor("red"))
    assert pixmap.save(str(session_poster)) is True

    loader_calls: list[str] = []
    window = PlayerWindow(
        FakePlayerController(),
        default_video_cover_loader=lambda: loader_calls.append("called") or "https://img.example/fallback.jpg",
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-1", vod_name="Movie", vod_pic=str(session_poster)),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )

    assert loader_calls == []
    assert window.poster_label.pixmap() is not None
    assert window.poster_label.pixmap().isNull() is False


def test_player_window_uses_default_video_cover_when_session_poster_is_empty(qtbot, monkeypatch) -> None:
    started: list[str] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        started.append(f"{target}:{source}")

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    window = PlayerWindow(
        FakePlayerController(),
        default_video_cover_loader=lambda: "https://img.example/fallback.jpg",
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-1", vod_name="Movie", vod_pic=""),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )

    assert started == ["video:https://img.example/fallback.jpg"]


def test_player_window_ignores_default_video_cover_when_value_is_youtube_watch_page(qtbot, monkeypatch) -> None:
    started: list[str] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        started.append(f"{target}:{source}")

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    window = PlayerWindow(
        FakePlayerController(),
        default_video_cover_loader=lambda: "https://www.youtube.com/watch?v=demo",
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-1", vod_name="Movie", vod_pic=""),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )

    assert started == []


def test_player_window_prefers_video_cover_override_before_session_poster_and_default_video_cover(qtbot, monkeypatch) -> None:
    detail_started: list[str] = []
    video_started: list[str] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        if target == "detail":
            detail_started.append(source)
        else:
            video_started.append(source)

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="https://img.example/detail.jpg"),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        video_cover_override="https://img.example/video.jpg",
    )
    window = PlayerWindow(
        FakePlayerController(),
        default_video_cover_loader=lambda: "https://img.example/fallback.jpg",
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    assert detail_started == ["https://img.example/detail.jpg"]
    assert video_started == ["https://img.example/video.jpg"]


def test_player_window_shows_poster_navigation_for_multiple_candidates(qtbot, monkeypatch) -> None:
    detail_started: list[str] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        del request_id, on_loaded
        if target == "detail":
            detail_started.append(source)

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="Movie",
            vod_pic="https://img.example/main.jpg",
            poster_candidates=[
                "https://img.example/main.jpg",
                "https://img.example/alt.jpg",
            ],
        ),
        playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    assert window._poster_previous_button.isHidden() is False
    assert window._poster_next_button.isHidden() is False
    assert window._poster_previous_button.autoRaise() is True
    assert window._poster_next_button.autoRaise() is True

    qtbot.mouseClick(window._poster_next_button, Qt.MouseButton.LeftButton)

    assert detail_started[-1] == "https://img.example/alt.jpg"


def test_player_window_places_builtin_arrow_buttons_on_both_sides_of_poster(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert isinstance(window._poster_previous_button, QToolButton)
    assert isinstance(window._poster_next_button, QToolButton)
    assert window._poster_previous_button.arrowType() == Qt.ArrowType.LeftArrow
    assert window._poster_next_button.arrowType() == Qt.ArrowType.RightArrow

    layout = window._poster_row_layout
    assert layout.indexOf(window._poster_previous_button) < layout.indexOf(window.poster_label)
    assert layout.indexOf(window.poster_label) < layout.indexOf(window._poster_next_button)


def test_player_window_hides_poster_navigation_for_single_candidate(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="https://img.example/main.jpg"),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )

    assert window._poster_previous_button.isHidden() is True
    assert window._poster_next_button.isHidden() is True


def test_player_window_poster_navigation_loops_at_boundaries(qtbot, monkeypatch) -> None:
    detail_started: list[str] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        del request_id, on_loaded
        if target == "detail":
            detail_started.append(source)

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(
                vod_id="movie-1",
                vod_name="Movie",
                vod_pic="https://img.example/main.jpg",
                poster_candidates=[
                    "https://img.example/main.jpg",
                    "https://img.example/alt.jpg",
                ],
            ),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )

    qtbot.mouseClick(window._poster_previous_button, Qt.MouseButton.LeftButton)

    assert detail_started[-1] == "https://img.example/alt.jpg"


def test_player_window_toggling_original_metadata_resets_detail_poster_to_first_candidate(qtbot, monkeypatch) -> None:
    detail_started: list[str] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        del request_id, on_loaded
        if target == "detail":
            detail_started.append(source)

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    session = PlayerSession(
        vod=VodItem(
            vod_id="v1",
            vod_name="原始标题",
            vod_pic="https://img.example/original-1.jpg",
            poster_candidates=[
                "https://img.example/original-1.jpg",
                "https://img.example/original-2.jpg",
            ],
            vod_content="原始简介",
        ),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda _session: VodItem(
            vod_id="v1",
            vod_name="增强标题",
            vod_pic="https://img.example/enhanced-1.jpg",
            poster_candidates=[
                "https://img.example/enhanced-1.jpg",
                "https://img.example/enhanced-2.jpg",
            ],
            vod_content="增强简介",
        ),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: "增强简介" in window.metadata_view.toPlainText(), timeout=1000)

    qtbot.mouseClick(window._poster_next_button, Qt.MouseButton.LeftButton)
    assert detail_started[-1] == "https://img.example/enhanced-2.jpg"

    qtbot.mouseClick(window._metadata_original_toggle, Qt.MouseButton.LeftButton)

    assert detail_started[-1] == "https://img.example/original-1.jpg"
    assert window.session.current_metadata_poster_index == 0


def test_player_window_metadata_scrape_apply_resets_detail_poster_index(qtbot, monkeypatch) -> None:
    detail_started: list[str] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        del request_id, on_loaded
        if target == "detail":
            detail_started.append(source)

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(
            vod_id="v1",
            vod_name="深空彼岸",
            vod_pic="https://img.example/original-1.jpg",
            poster_candidates=[
                "https://img.example/original-1.jpg",
                "https://img.example/original-2.jpg",
            ],
            vod_content="原始简介",
        ),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    qtbot.mouseClick(window._poster_next_button, Qt.MouseButton.LeftButton)
    assert window.session.current_metadata_poster_index == 1

    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)
    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: "豆瓣简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert detail_started[-1] == "https://img.example/poster.jpg"
    assert window.session.current_metadata_poster_index == 0


def test_player_window_refreshes_only_video_poster_after_async_playback_loader_updates_cover_override(qtbot, monkeypatch) -> None:
    detail_started: list[str] = []
    video_started: list[str] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        if target == "detail":
            detail_started.append(source)
        else:
            video_started.append(source)

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers=None) -> None:
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
            return False

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def supports_secondary_subtitle_position(self) -> bool:
            return False

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(
            vod_id="plugin-vod-1",
            vod_name="占位电影",
            vod_pic="https://img.example/detail.jpg",
        ),
        playlist=[PlayItem(title="第1集", url="", vod_id="/play/1", play_source="备用线")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        async_playback_loader=True,
    )

    def playback_loader(item: PlayItem) -> None:
        session.video_cover_override = "https://img.example/video.jpg"
        item.url = "http://m/1.m3u8"
        return None

    session.playback_loader = playback_loader

    window = PlayerWindow(FakePlayerController(), default_video_cover_loader=lambda: "")
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(
        lambda: detail_started[:1] == ["https://img.example/detail.jpg"]
        and video_started == ["https://img.example/video.jpg"]
    )
    assert session.vod.vod_pic == "https://img.example/detail.jpg"


def test_player_window_restores_previous_item_video_cover_when_switching_back(qtbot, monkeypatch) -> None:
    video_started: list[str] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        if target == "video":
            video_started.append(source)

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    session = PlayerSession(
        vod=VodItem(vod_id="plugin-vod-1", vod_name="占位电影", vod_pic=""),
        playlist=[
            PlayItem(title="第1首", url="", vod_id="/play/1", play_source="默认线"),
            PlayItem(title="第2首", url="", vod_id="/play/2", play_source="默认线"),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        async_playback_loader=True,
    )

    def playback_loader(item: PlayItem) -> None:
        if item.title == "第1首":
            session.video_cover_override = "https://img.example/song-1.jpg"
            item.url = "http://m/1.mp3"
            return None
        session.video_cover_override = "https://img.example/song-2.jpg"
        item.url = "http://m/2.mp3"
        return None

    session.playback_loader = playback_loader

    window = PlayerWindow(FakePlayerController(), default_video_cover_loader=lambda: "")
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: video_started == ["https://img.example/song-1.jpg"])

    window.play_next()
    qtbot.waitUntil(lambda: video_started == ["https://img.example/song-1.jpg", "https://img.example/song-2.jpg"])

    window.play_previous()
    qtbot.waitUntil(
        lambda: video_started
        == [
            "https://img.example/song-1.jpg",
            "https://img.example/song-2.jpg",
            "https://img.example/song-1.jpg",
        ]
    )


def test_player_window_prefers_current_item_cover_when_session_override_is_stale(qtbot, monkeypatch) -> None:
    video_started: list[str] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        if target == "video":
            video_started.append(source)

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    window = PlayerWindow(FakePlayerController(), default_video_cover_loader=lambda: "")
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="plugin-vod-1", vod_name="占位电影", vod_pic="poster-detail"),
            playlist=[
                PlayItem(
                    title="第1首",
                    url="http://m/1.mp3",
                    vod_id="/play/1",
                    play_source="默认线",
                    video_cover_override="https://img.example/song-1.jpg",
                ),
                PlayItem(
                    title="第2首",
                    url="http://m/2.mp3",
                    vod_id="/play/2",
                    play_source="默认线",
                    video_cover_override="https://img.example/song-2.jpg",
                ),
            ],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
            video_cover_override="https://img.example/song-2.jpg",
        )
    )

    assert video_started == ["https://img.example/song-1.jpg"]


def test_player_window_refreshes_youtube_channel_cover_when_switching_items(qtbot, monkeypatch) -> None:
    detail_started: list[str] = []
    video_started: list[str] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        if target == "detail":
            detail_started.append(source)
        if target == "video":
            video_started.append(source)

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    window = PlayerWindow(FakePlayerController(), default_video_cover_loader=lambda: "")
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(
            vod_id="yt:channel:UCdemo",
            vod_name="频道",
            detail_style="youtube",
            vod_pic="https://img.example/video-1.jpg",
        ),
        playlist=[
            PlayItem(
                title="视频 1",
                url="http://m/1.m3u8",
                vod_id="yt:video:one",
                video_cover_override="https://img.example/video-1.jpg",
            ),
            PlayItem(
                title="视频 2",
                url="http://m/2.m3u8",
                vod_id="yt:video:two",
                video_cover_override="https://img.example/video-2.jpg",
            ),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)
    assert detail_started == ["https://img.example/video-1.jpg"]
    assert video_started == ["https://img.example/video-1.jpg"]

    window.play_next()

    assert detail_started[-1] == "https://img.example/video-2.jpg"
    assert video_started[-1] == "https://img.example/video-2.jpg"


def test_player_window_refreshes_youtube_channel_detail_poster_after_async_playlist_replacement(qtbot, monkeypatch) -> None:
    detail_started: list[str] = []
    video_started: list[str] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        if target == "detail":
            detail_started.append(source)
        if target == "video":
            video_started.append(source)

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    session = PlayerSession(
        vod=VodItem(
            vod_id="channel@UCdemo",
            vod_name="频道",
            detail_style="youtube",
            vod_pic="https://img.example/channel.jpg",
        ),
        playlist=[
            PlayItem(
                title="频道",
                url="",
                vod_id="channel@UCdemo",
                media_title="频道",
                video_cover_override="https://img.example/channel.jpg",
                play_source="YouTube",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        async_playback_loader=True,
    )

    def playback_loader(item: PlayItem) -> PlaybackLoadResult:
        del item
        return PlaybackLoadResult(
            replacement_playlist=[
                PlayItem(
                    title="视频 1",
                    url="http://m/1.m3u8",
                    vod_id="yt:video:one",
                    video_cover_override="https://img.example/video-1.jpg",
                    play_source="YouTube",
                ),
                PlayItem(
                    title="视频 2",
                    url="http://m/2.m3u8",
                    vod_id="yt:video:two",
                    video_cover_override="https://img.example/video-2.jpg",
                    play_source="YouTube",
                ),
            ],
            replacement_start_index=0,
        )

    session.playback_loader = playback_loader

    window = PlayerWindow(FakePlayerController(), default_video_cover_loader=lambda: "")
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: video_started[-1:] == ["https://img.example/video-1.jpg"], timeout=1000)
    assert detail_started == [
        "https://img.example/channel.jpg",
        "https://img.example/video-1.jpg",
    ]
    assert video_started[-1] == "https://img.example/video-1.jpg"


def test_player_window_passes_local_audio_cover_for_audio_only_media(qtbot, tmp_path) -> None:
    poster_path = tmp_path / "cover.png"
    pixmap = QPixmap(20, 30)
    pixmap.fill(QColor("yellow"))
    assert pixmap.save(str(poster_path)) is True

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int, str | None]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
        ) -> None:
            self.load_calls.append((url, start_seconds, poster_image_path))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="song-1", vod_name="Song", vod_pic=str(poster_path)),
            playlist=[PlayItem(title="试听", url="http://m/1.mp3")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )

    assert window.video.load_calls == [("http://m/1.mp3", 0, str(poster_path))]


def test_player_window_uses_default_audio_cover_when_session_poster_is_missing(qtbot, tmp_path) -> None:
    poster_path = tmp_path / "default-cover.png"
    pixmap = QPixmap(20, 30)
    pixmap.fill(QColor("cyan"))
    assert pixmap.save(str(poster_path)) is True

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int, str | None]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
        ) -> None:
            self.load_calls.append((url, start_seconds, poster_image_path))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController(), default_video_cover_loader=lambda: str(poster_path))
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="song-1", vod_name="Song", vod_pic=""),
            playlist=[PlayItem(title="试听", url="http://m/1.mp3")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )

    assert window.video.load_calls == [("http://m/1.mp3", 0, str(poster_path))]


def test_player_window_does_not_pass_audio_cover_for_normal_video_media(qtbot, tmp_path) -> None:
    poster_path = tmp_path / "cover.png"
    pixmap = QPixmap(20, 30)
    pixmap.fill(QColor("magenta"))
    assert pixmap.save(str(poster_path)) is True

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int, str | None]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
        ) -> None:
            self.load_calls.append((url, start_seconds, poster_image_path))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-1", vod_name="Movie", vod_pic=str(poster_path)),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )

    assert window.video.load_calls == [("http://m/1.m3u8", 0, None)]


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
    started: list[tuple[str, str]] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        started.append((target, source))

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
    assert started == [
        ("detail", "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123.jpg"),
        ("video", "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123.jpg"),
    ]
    assert rendered is None or rendered.isNull() is True
    assert window.video.load_calls == [("http://m/1.m3u8", False, 0)]


def test_player_window_handles_javascript_escaped_remote_poster_url_without_crashing(qtbot, monkeypatch) -> None:
    started: list[tuple[str, str]] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        started.append((target, source))

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
        vod=VodItem(
            vod_id="live-1",
            vod_name="Live",
            detail_style="live",
            vod_pic="https:\\u002F\\u002Fimg.example\\u002Fposter.jpg?foo=1\\u0026bar=2",
        ),
        playlist=[PlayItem(title="线路 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert started == [
        ("detail", "https:\\u002F\\u002Fimg.example\\u002Fposter.jpg?foo=1\\u0026bar=2"),
        ("video", "https:\\u002F\\u002Fimg.example\\u002Fposter.jpg?foo=1\\u0026bar=2"),
    ]
    assert window.video.load_calls == [("http://m/1.m3u8", False, 0)]


def test_player_window_load_poster_pixmap_ignores_long_javascript_escaped_remote_url(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    source = "https:\\u002F\\u002Fimg.example\\u002F" + ("a" * 400) + ".jpg"

    pixmap = window._load_poster_pixmap(source)

    assert pixmap.isNull() is True


def test_player_window_uses_larger_remote_load_size_for_video_poster_than_detail_poster(qtbot, monkeypatch) -> None:
    requests: list[tuple[str, int, int]] = []

    def fake_load_remote_poster_image(source: str, target_size, timeout=0, get=None):
        del timeout, get
        requests.append((source, target_size.width(), target_size.height()))
        image = QImage(40, 60, QImage.Format.Format_RGB32)
        image.fill(QColor("blue"))
        return image

    monkeypatch.setattr(player_window_module, "load_remote_poster_image", fake_load_remote_poster_image)

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    qtbot.wait(50)

    window._start_poster_load("https://img.example/poster.jpg", 1, target="detail")
    window._start_poster_load("https://img.example/poster.jpg", 2, target="video")

    qtbot.waitUntil(lambda: len(requests) == 2, timeout=1000)

    assert requests[0] == ("https://img.example/poster.jpg", PlayerWindow._POSTER_SIZE.width(), PlayerWindow._POSTER_SIZE.height())
    assert requests[1][0] == "https://img.example/poster.jpg"
    assert requests[1][1] > PlayerWindow._POSTER_SIZE.width()
    assert requests[1][2] > PlayerWindow._POSTER_SIZE.height()


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

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
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


def test_player_window_shows_loaded_poster_over_video_until_visible_picture_signal_arrives(qtbot) -> None:
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

    window._handle_video_picture_state_changed("visible")

    assert window.video_poster_overlay.isHidden() is True


def test_player_window_attaches_audio_cover_after_remote_poster_load_finishes(qtbot, monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "poster-cache.img"
    remote_source = "https://img.example/song-cover.jpg"

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        return None

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)
    monkeypatch.setattr(player_window_module, "poster_cache_path", lambda _url: cache_path)

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int, str | None]] = []
            self.attach_calls: list[str] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
        ) -> None:
            self.load_calls.append((url, start_seconds, poster_image_path))

        def attach_audio_cover(self, poster_image_path: str) -> None:
            self.attach_calls.append(poster_image_path)

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def duration_seconds(self) -> int:
            return 0

        def position_seconds(self) -> int:
            return 0

    image = QImage(20, 30, QImage.Format.Format_RGB32)
    image.fill(QColor("green"))

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="song-1", vod_name="Song", vod_pic=remote_source),
            playlist=[PlayItem(title="试听", url="http://m/1.mp3")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )

    cache_path.write_bytes(b"cover")
    window._handle_poster_load_finished(window._poster_request_id, image)

    assert window.video.load_calls == [("http://m/1.mp3", 0, None)]
    assert window.video.attach_calls == [str(cache_path)]


def test_player_window_hides_video_poster_overlay_after_visible_picture_signal(qtbot) -> None:
    image = QImage(24, 36, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)

    window = PlayerWindow(FakePlayerController(), default_video_cover_loader=lambda: "")
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="https://img.example/poster.jpg"),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )
    window._handle_video_poster_load_finished(window._video_poster_request_id, image)
    window._handle_video_picture_state_changed("loading")

    assert window.video_poster_overlay.isHidden() is False

    window._handle_video_picture_state_changed("visible")

    assert window.video_poster_overlay.isHidden() is True


def test_player_window_hides_video_poster_overlay_after_audio_cover_signal(qtbot) -> None:
    image = QImage(24, 36, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)

    window = PlayerWindow(FakePlayerController(), default_video_cover_loader=lambda: "")
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="song-1", vod_name="Song", vod_pic="https://img.example/poster.jpg"),
            playlist=[PlayItem(title="试听", url="http://m/1.mp3")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )
    window._handle_video_poster_load_finished(window._video_poster_request_id, image)
    window._handle_video_picture_state_changed("loading")

    assert window.video_poster_overlay.isHidden() is False

    window._handle_video_picture_state_changed("audio-cover")

    assert window.video_poster_overlay.isHidden() is True


def test_player_window_shows_video_poster_overlay_again_after_playback_failure(qtbot) -> None:
    image = QImage(24, 36, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)

    window = PlayerWindow(FakePlayerController(), default_video_cover_loader=lambda: "")
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="https://img.example/poster.jpg"),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )
    window._handle_video_poster_load_finished(window._video_poster_request_id, image)
    window._handle_video_picture_state_changed("visible")

    assert window.video_poster_overlay.isHidden() is True

    window._handle_playback_failed("播放失败: HTTP 403 Forbidden")

    assert window.video_poster_overlay.isHidden() is False
    assert "播放失败: HTTP 403 Forbidden" in window.log_view.toPlainText()


def test_player_window_shows_video_poster_overlay_when_picture_becomes_unavailable(qtbot) -> None:
    image = QImage(24, 36, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)

    window = PlayerWindow(FakePlayerController(), default_video_cover_loader=lambda: "")
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(
        PlayerSession(
            vod=VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="https://img.example/poster.jpg"),
            playlist=[PlayItem(title="正片", url="http://m/1.m3u8")],
            start_index=0,
            start_position_seconds=0,
            speed=1.0,
        )
    )
    window._handle_video_poster_load_finished(window._video_poster_request_id, image)

    window._handle_video_picture_state_changed("unavailable")

    assert window.video_poster_overlay.isHidden() is False
    assert "当前媒体没有可用视频画面，已显示封面" not in window.log_view.toPlainText()


def test_player_window_hides_video_poster_overlay_when_picture_is_unavailable_but_primary_subtitle_is_active(qtbot) -> None:
    image = QImage(24, 36, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)

    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频", vod_pic="https://img.example/poster.jpg"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.mp3",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url="http://sub/1.srt",
                        format="application/x-subrip",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), default_video_cover_loader=lambda: "")
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)
    window._primary_external_subtitle_selection = ExternalSubtitleSelection(source="spider", option_url="http://sub/1.srt")
    window._primary_external_subtitle_track_id = 91
    window._handle_video_poster_load_finished(window._video_poster_request_id, image)

    window._handle_video_picture_state_changed("unavailable")

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
    qtbot.waitUntil(lambda: len(requests) >= 1)
    qtbot.waitUntil(lambda: (window.poster_label.pixmap() is not None and not window.poster_label.pixmap().isNull()))

    rendered = window.poster_label.pixmap()
    assert rendered is not None
    assert rendered.isNull() is False
    assert requests[0] == (
        "https://img3.doubanio.com/view/photo/m/public/p123.jpg",
        {
            "Referer": "https://movie.douban.com/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        },
        10.0,
    )
    assert len(requests) in {1, 2}


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
    qtbot.waitUntil(lambda: len(requested_timeouts) >= 1)

    assert requested_timeouts
    assert all(timeout == 10.0 for timeout in requested_timeouts)


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
    qtbot.waitUntil(lambda: len(requested_headers) >= 1)

    expected_headers = {
        "Referer": "https://www.youtube.com/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    }
    assert requested_headers
    assert all(headers == expected_headers for headers in requested_headers)


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
    qtbot.waitUntil(lambda: len(requested_headers) >= 1)

    expected_headers = {
        "Referer": "https://cc.163.com/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    }
    assert requested_headers
    assert all(headers == expected_headers for headers in requested_headers)


def test_player_window_uses_vertical_shell_with_bottom_controls(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    root_layout = window.content_layout()

    assert window.layout() is not None
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


def test_player_window_omits_empty_bilibili_metadata_rows(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    session = PlayerSession(
        vod=VodItem(
            vod_id="BV1ebREBmEha",
            vod_name="和AI玩猜历史人物游戏，又被它给耍了",
            detail_style="bilibili",
            type_name=" / ",
            vod_year="",
            vod_area="",
            vod_lang="",
            vod_remarks="339万播放 04:20",
            vod_director="混饭达人",
            vod_actor="",
            vod_content="发布于2026-05-02 17:26:35",
            dbid=0,
        ),
        playlist=[PlayItem(title="视频", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.metadata_view.toPlainText() == (
        "名称: 和AI玩猜历史人物游戏，又被它给耍了\n"
        "评分: 339万播放 04:20\n"
        "导演: 混饭达人\n"
        "\n"
        "简介:\n"
        "发布于2026-05-02 17:26:35"
    )


def test_player_window_renders_youtube_metadata_with_custom_rows(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

    session = PlayerSession(
        vod=VodItem(
            vod_id="yt:video:abc123",
            vod_name="Harness Engineering 到底是什么？概念、实战与争议，一次全部讲清楚",
            detail_style="youtube",
            vod_remarks="频道",
            vod_content="不应显示为简介",
            detail_fields=[
                PlaybackDetailField("频道", "马克的技术工作坊"),
                PlaybackDetailField("发布", "2026-05-05"),
                PlaybackDetailField("时长", "37:24"),
                PlaybackDetailField("播放", "5.2万"),
                PlaybackDetailField("点赞", "1501"),
                PlaybackDetailField("评论", "73"),
            ],
        ),
        playlist=[PlayItem(title="视频", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.metadata_view.toPlainText() == (
        "标题: Harness Engineering 到底是什么？概念、实战与争议，一次全部讲清楚\n"
        "频道: 马克的技术工作坊\n"
        "发布: 2026-05-05\n"
        "时长: 37:24\n"
        "播放: 5.2万\n"
        "点赞: 1501\n"
        "评论: 73"
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


def test_player_window_hides_detail_actions_when_current_item_has_none(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(make_player_session(start_index=0))

    assert window.detail_actions_widget.isHidden() is True
    assert window.detail_actions_layout.count() == 0


def test_player_window_inlines_collection_level_detail_fields_into_metadata_text(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="Movie",
            type_name="剧情",
            vod_content="简介文本",
            detail_fields=[PlaybackDetailField(label="播放", value="12万")],
        ),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    assert "播放: 12万" in window.metadata_view.toPlainText()


def test_player_window_hides_internal_episode_count_detail_fields_from_cached_metadata(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    session = PlayerSession(
        vod=VodItem(
            vod_id="series-1",
            vod_name="入侵 第一季",
            type_name="剧情",
            vod_content="简介文本",
            detail_fields=[
                PlaybackDetailField(label="TMDB ID", value="34541"),
                PlaybackDetailField(label="number_of_episodes", value="12"),
                PlaybackDetailField(label="number_of_seasons", value="2"),
            ],
        ),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    metadata_text = window.metadata_view.toPlainText()
    assert "TMDB ID: 34541" in metadata_text
    assert "number_of_episodes" not in metadata_text
    assert "number_of_seasons" not in metadata_text


def test_player_window_omits_inline_detail_field_lines_when_no_fields_exist(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie", type_name="剧情", vod_content="简介"),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    assert "播放:" not in window.metadata_view.toPlainText()
    assert window.metadata_view.toPlainText().endswith("简介:\n简介")


def test_player_window_prefers_current_item_detail_fields_inside_metadata_text(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    session = PlayerSession(
        vod=VodItem(
            vod_id="series-1",
            vod_name="Series",
            type_name="剧情",
            vod_content="简介文本",
            detail_fields=[PlaybackDetailField(label="播放", value="12万")],
        ),
        playlist=[
            PlayItem(
                title="Episode 1",
                url="http://m/1.m3u8",
                detail_fields=[PlaybackDetailField(label="播放", value="18万")],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    assert "播放: 18万" in window.metadata_view.toPlainText()
    assert "播放: 12万" not in window.metadata_view.toPlainText()


def test_player_window_falls_back_to_vod_detail_fields_inside_metadata_text(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    session = PlayerSession(
        vod=VodItem(
            vod_id="series-1",
            vod_name="Series",
            type_name="剧情",
            vod_content="简介文本",
            detail_fields=[PlaybackDetailField(label="播放", value="12万")],
        ),
        playlist=[
            PlayItem(
                title="Episode 1",
                url="http://m/1.m3u8",
                detail_fields=[PlaybackDetailField(label="播放", value="18万")],
            ),
            PlayItem(title="Episode 2", url="http://m/2.m3u8"),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)
    window._play_item_at_index(1)

    assert "播放: 12万" in window.metadata_view.toPlainText()
    assert "播放: 18万" not in window.metadata_view.toPlainText()


def test_player_window_replaces_collection_detail_fields_inside_metadata_after_spider_playback_loader(qtbot) -> None:
    controller = SpiderPluginController(DetailFieldPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")
    session = PlayerController(type("Api", (), {"get_history": lambda self, _key: None})()).create_session(
        request.vod,
        request.playlist,
        request.clicked_index,
        playlists=request.playlists,
        playlist_index=request.playlist_index,
        playback_loader=request.playback_loader,
        async_playback_loader=request.async_playback_loader,
        detail_action_runner=request.detail_action_runner,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    assert "播放: 12万" in window.metadata_view.toPlainText()
    assert "更新: 2026-05-08" in window.metadata_view.toPlainText()

    assert session.playback_loader is not None
    session.playback_loader(session.playlist[0])
    window._render_metadata()

    assert "播放: 18万" in window.metadata_view.toPlainText()
    assert "热度: 95" in window.metadata_view.toPlainText()
    assert "更新: 2026-05-08" not in window.metadata_view.toPlainText()


def test_player_window_renders_clickable_detail_field_value_parts_inside_metadata(qtbot) -> None:
    clicked: list[PlaybackDetailFieldAction] = []
    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="Movie",
            detail_fields=[
                PlaybackDetailField(
                    label="演员",
                    value_parts=[
                        PlaybackDetailValuePart(
                            label="演员1",
                            action=PlaybackDetailFieldAction(type="search", value="演员1"),
                        ),
                        PlaybackDetailValuePart(label="演员2"),
                    ],
                )
            ],
        ),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        detail_field_runner=lambda _item, action: clicked.append(action),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    assert "演员: 演员1 / 演员2" in window.metadata_view.toPlainText()
    window._handle_metadata_link(QUrl("atv-player://detail-field?action_type=search&action_value=%E6%BC%94%E5%91%981"))
    assert clicked == [PlaybackDetailFieldAction(type="search", value="演员1")]


def test_player_window_clicking_normal_detail_name_requests_global_search(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="刮削后的标题", vod_content="简介文本"),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    searched: list[str] = []
    closed = {"count": 0}
    window.global_search_requested.connect(searched.append)
    window.closed_to_main.connect(lambda: closed.__setitem__("count", closed["count"] + 1))

    window.open_session(session)
    window.show()

    assert "名称: 刮削后的标题" in window.metadata_view.toPlainText()
    assert "atv-player://global-search" in window.metadata_view.toHtml()
    window._handle_metadata_link(
        QUrl("atv-player://global-search?keyword=%E5%88%AE%E5%89%8A%E5%90%8E%E7%9A%84%E6%A0%87%E9%A2%98")
    )
    assert searched == ["刮削后的标题"]
    assert closed["count"] == 1
    assert window.isHidden() is True


@pytest.mark.parametrize("detail_style", ["youtube", "live"])
def test_player_window_does_not_make_youtube_or_live_title_global_search_link(qtbot, detail_style: str) -> None:
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="标题文本", vod_content="简介文本", detail_style=detail_style),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    assert "atv-player://global-search" not in window.metadata_view.toHtml()


def test_player_window_renders_bilibili_cr_link_inside_metadata_value(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="Movie",
            vod_director='[a=cr:{"target":"bilibili","type":"category","value":"up:378885845"}/]Harold[/a]',
            vod_content="简介文本",
        ),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    html = window.metadata_view.toHtml()
    plain_text = window.metadata_view.toPlainText()
    assert "导演: Harold" in plain_text
    assert "action_target=bilibili" in html
    assert "action_type=category" in html
    assert "action_value=up:378885845" in html


def test_player_window_renders_multiple_cr_links_with_plain_separators(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="Movie",
            vod_actor=(
                '[a=cr:{"type":"search","value":"演员1"}/]演员1[/a]'
                " / "
                '[a=cr:{"type":"search","value":"演员2"}/]演员2[/a]'
            ),
            vod_content="简介文本",
        ),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    html = window.metadata_view.toHtml()
    plain_text = window.metadata_view.toPlainText()
    assert "演员: 演员1 / 演员2" in plain_text
    assert html.count("action_type=search") == 2


def test_player_window_degrades_invalid_cr_markup_to_plain_text(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="Movie",
            vod_director='[a=cr:{"target":"bilibili","type":"category"}/]Harold[/a]',
            vod_content="简介文本",
        ),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    html = window.metadata_view.toHtml()
    plain_text = window.metadata_view.toPlainText()
    assert '[a=cr:{"target":"bilibili","type":"category"}/]Harold[/a]' in plain_text
    assert "action_target=bilibili" not in html


def test_player_window_metadata_link_dispatches_action_target(qtbot) -> None:
    clicked: list[PlaybackDetailFieldAction] = []
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        detail_field_runner=lambda _item, action: clicked.append(action),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    window._handle_metadata_link(
        QUrl(
            "atv-player://detail-field?"
            "action_target=bilibili&action_type=category&action_value=up%3A378885845"
        )
    )

    assert clicked == [
        PlaybackDetailFieldAction(target="bilibili", type="category", value="up:378885845")
    ]


def test_player_window_renders_external_metadata_links_for_known_ids(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="Movie",
            category_name="动漫",
            dbid=30318230,
            detail_fields=[
                PlaybackDetailField(label="TMDB ID", value="76479"),
                PlaybackDetailField(label="Bangumi ID", value="526975"),
                PlaybackDetailField(label="IMDb ID", value="tt28489780"),
            ],
        ),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    html = window.metadata_view.toHtml()
    accent = player_window_module.current_theme_manager().tokens_for(
        player_window_module.current_resolved_theme()
    ).accent
    assert "https://movie.douban.com/subject/30318230/" in html
    assert "https://www.themoviedb.org/tv/76479" in html
    assert "https://bgm.tv/subject/526975" in html
    assert "https://www.imdb.com/title/tt28489780" in html
    assert "font-weight:600" in html
    assert f"color:{accent}" in html
    assert "text-decoration: underline" not in html


def test_player_window_renders_official_link_without_raw_playback_link_label(qtbot) -> None:
    vod = VodItem(
        vod_id="movie-1",
        vod_name="主角",
        detail_fields=[PlaybackDetailField(label="播放链接", value="https://v.qq.com/x/cover/mzc003ubb5py7hr.html")],
    )
    merge_metadata_record(
        vod,
        MetadataRecord(
            provider="tmdb",
            provider_id="tv:241166",
            aliases=["我成为BL剧的主角了"],
            imdb_id="tt30768610",
            tmdb_id="241166",
        ),
        provider_priority=["tmdb"],
    )
    session = PlayerSession(
        vod=vod,
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    plain_text = window.metadata_view.toPlainText()
    html = window.metadata_view.toHtml()
    assert "官方链接: 腾讯视频" in plain_text
    assert "播放链接:" not in plain_text
    assert "https://v.qq.com/x/cover/mzc003ubb5py7hr.html" in html


def test_player_window_opens_external_metadata_link(qtbot, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(player_window_module.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.open_session(session)

    window._handle_metadata_link(QUrl("https://movie.douban.com/subject/30318230/"))

    assert opened == ["https://movie.douban.com/subject/30318230/"]
    assert window.metadata_view.focusPolicy() == Qt.FocusPolicy.ClickFocus


@pytest.mark.parametrize(
    ("target", "expected_url"),
    [
        ("movie", "https://www.themoviedb.org/movie/76479"),
        ("tv", "https://www.themoviedb.org/tv/76479"),
    ],
)
def test_player_window_renders_link_action_id_as_external_url(qtbot, target: str, expected_url: str) -> None:
    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="Movie",
            detail_fields=[
                PlaybackDetailField(
                    label="TMDB ID",
                    value_parts=[
                        PlaybackDetailValuePart(
                            label="76479",
                            action=PlaybackDetailFieldAction(type="link", value="76479", target=target),
                        )
                    ],
                )
            ],
        ),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    html = window.metadata_view.toHtml()
    accent = player_window_module.current_theme_manager().tokens_for(
        player_window_module.current_resolved_theme()
    ).accent
    assert expected_url in html
    assert "font-weight:600" in html
    assert f"color:{accent}" in html
    assert "text-decoration: underline" not in html


def test_player_window_renders_bilibili_link_actions_as_external_urls(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(
            vod_id="BV14rd3BJEDV",
            vod_name="B站视频",
            detail_fields=[
                PlaybackDetailField(
                    label="BVID",
                    value_parts=[
                        PlaybackDetailValuePart(
                            label="BV14rd3BJEDV",
                            action=PlaybackDetailFieldAction(type="link", value="BV14rd3BJEDV", target="bilibili"),
                        )
                    ],
                ),
                PlaybackDetailField(
                    label="Season ID",
                    value_parts=[
                        PlaybackDetailValuePart(
                            label="142986",
                            action=PlaybackDetailFieldAction(type="link", value="season$142986", target="bilibili"),
                        )
                    ],
                ),
            ],
        ),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    html = window.metadata_view.toHtml()
    assert "https://www.bilibili.com/video/BV14rd3BJEDV" in html
    assert "https://www.bilibili.com/bangumi/play/ss142986" in html


def test_player_window_renders_bilibili_ss_link_action_as_external_url(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(
            vod_id="ss142986",
            vod_name="B站番剧",
            detail_fields=[
                PlaybackDetailField(
                    label="Season ID",
                    value_parts=[
                        PlaybackDetailValuePart(
                            label="142986",
                            action=PlaybackDetailFieldAction(type="link", value="ss142986", target="bilibili"),
                        )
                    ],
                )
            ],
        ),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    assert "https://www.bilibili.com/bangumi/play/ss142986" in window.metadata_view.toHtml()


def test_player_window_keeps_bilibili_identity_links_when_item_detail_fields_exist(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(
            vod_id="ss142986",
            vod_name="B站番剧",
            detail_style="bilibili",
            detail_fields=[
                PlaybackDetailField(
                    label="Season ID",
                    value_parts=[
                        PlaybackDetailValuePart(
                            label="142986",
                            action=PlaybackDetailFieldAction(type="link", value="ss142986", target="bilibili"),
                        )
                    ],
                )
            ],
        ),
        playlist=[
            PlayItem(
                title="第1话",
                url="http://m/1.m3u8",
                detail_fields=[PlaybackDetailField(label="更新状态", value="连载中")],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    html = window.metadata_view.toHtml()
    plain_text = window.metadata_view.toPlainText()
    assert "https://www.bilibili.com/bangumi/play/ss142986" in html
    assert "更新状态: 连载中" in plain_text


def test_player_window_prefers_bilibili_collection_detail_fields_over_item_fields(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(
            vod_id="ss45969",
            vod_name="牧神记",
            detail_style="bilibili",
            detail_fields=[
                PlaybackDetailField(
                    label="Season ID",
                    value_parts=[
                        PlaybackDetailValuePart(
                            label="45969",
                            action=PlaybackDetailFieldAction(type="link", value="ss45969", target="bilibili"),
                        )
                    ],
                ),
                PlaybackDetailField(label="更新状态", value="连载中, 每周日 11:00更新"),
                PlaybackDetailField(label="声优", value="少年秦牧：张若瑜/姚铭舜"),
            ],
        ),
        playlist=[
            PlayItem(
                title="第3话",
                url="http://m/3.m3u8",
                detail_fields=[
                    PlaybackDetailField(label="TMDB ID", value="236534"),
                    PlaybackDetailField(label="别名", value="Tales of Herding Gods"),
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        source_kind="bilibili",
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    plain_text = window.metadata_view.toPlainText()
    assert "更新状态: 连载中, 每周日 11:00更新" in plain_text
    assert "声优: 少年秦牧：张若瑜/姚铭舜" in plain_text
    assert "TMDB ID: 236534" in plain_text


def test_player_window_renders_plain_multi_value_detail_fields_inside_metadata(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(
            vod_id="movie-1",
            vod_name="Movie",
            detail_fields=[
                PlaybackDetailField(
                    label="标签",
                    value_parts=[PlaybackDetailValuePart(label="动作"), PlaybackDetailValuePart(label="冒险")],
                )
            ],
        ),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)

    assert "标签: 动作 / 冒险" in window.metadata_view.toPlainText()


def test_player_window_renders_current_item_detail_actions_in_order(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="song-1", vod_name="Song"),
        playlist=[
            PlayItem(
                title="Track 1",
                url="http://m/1.m3u8",
                detail_actions=[
                    PlaybackDetailAction(id="favorite_collection", label="收藏歌单", active=True, tooltip="已收藏"),
                    PlaybackDetailAction(id="favorite_track", label="收藏歌曲", enabled=False),
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    assert window.detail_actions_widget.isHidden() is False
    assert [window.detail_actions_layout.itemAt(i).widget().text() for i in range(window.detail_actions_layout.count())] == [
        "收藏歌单",
        "收藏歌曲",
    ]
    assert window.detail_actions_layout.itemAt(0).widget().toolTip() == "已收藏"
    assert window.detail_actions_layout.itemAt(1).widget().isEnabled() is False


def test_player_window_renders_detail_favorite_icon_button(qtbot) -> None:
    toggled: list[str] = []
    favorite_ids = {"detail-1"}

    def toggle_favorite(current_item: PlayItem) -> None:
        toggled.append(current_item.vod_id)
        if current_item.vod_id in favorite_ids:
            favorite_ids.remove(current_item.vod_id)
        else:
            favorite_ids.add(current_item.vod_id)

    item = PlayItem(title="庆余年", url="https://media/1.m3u8", vod_id="detail-1")
    session = PlayerSession(
        vod=VodItem(vod_id="detail-1", vod_name="庆余年"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(),
        save_config=lambda: None,
        favorite_is_active=lambda current_item: current_item.vod_id in favorite_ids,
        favorite_toggle=toggle_favorite,
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    heading_index = window._metadata_heading_row.indexOf(window.metadata_heading)
    assert heading_index >= 0
    assert window._metadata_heading_row.indexOf(window.favorite_button) == heading_index + 1
    assert "border: none" in window.favorite_button.styleSheet()
    assert window.favorite_button.property("favorite_active") is True
    assert window.favorite_button.toolTip() == "取消收藏"
    assert window.favorite_button.property("icon_name") == "favorite-filled.svg"
    window.favorite_button.click()

    assert toggled == ["detail-1"]
    assert window.favorite_button.property("favorite_active") is False
    assert window.favorite_button.toolTip() == "加入收藏"
    assert window.favorite_button.property("icon_name") == "favorite.svg"


def test_player_window_following_button_sits_next_to_favorite(
    qtbot,
) -> None:
    followed_ids: set[str] = {"vod-1"}
    toggled: list[str] = []

    def toggle_following(current_item: PlayItem) -> None:
        toggled.append(current_item.vod_id)
        if current_item.vod_id in followed_ids:
            followed_ids.remove(current_item.vod_id)
        else:
            followed_ids.add(current_item.vod_id)

    item = PlayItem(title="第1集", url="https://media/1.m3u8", vod_id="vod-1")
    session = PlayerSession(
        vod=VodItem(vod_id="vod-1", vod_name="凡人修仙传"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        following_is_active=lambda current_item: current_item.vod_id in followed_ids,
        following_toggle=toggle_following,
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    assert (
        window._metadata_heading_row.indexOf(window.following_button)
        == window._metadata_heading_row.indexOf(window.favorite_button) + 1
    )
    assert window.following_button.property("following_active") is True
    assert window.following_button.toolTip() == "取消追更"

    window.following_button.click()

    assert toggled == ["vod-1"]
    assert window.following_button.property("following_active") is False
    assert window.following_button.toolTip() == "加入追更"


def test_player_window_reports_following_progress_with_current_item(qtbot) -> None:
    reports: list[tuple[str, int, int]] = []
    item = PlayItem(title="第1集", url="https://media/1.m3u8", vod_id="vod-1")
    session = PlayerSession(
        vod=VodItem(vod_id="vod-1", vod_name="凡人修仙传"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(
        FakePlayerController(),
        following_progress_reporter=lambda current_item, **kwargs: reports.append(
            (
                current_item.vod_id,
                kwargs["position_seconds"],
                kwargs["duration_seconds"],
            )
        ),
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    window.report_progress()

    assert reports == [("vod-1", 30, 120)]


def test_player_window_executes_detail_action_and_refreshes_current_item(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    calls: list[tuple[str, str]] = []
    item = PlayItem(
        title="Track 1",
        url="http://m/1.m3u8",
        detail_actions=[PlaybackDetailAction(id="favorite_track", label="收藏歌曲")],
    )
    session = PlayerSession(
        vod=VodItem(vod_id="song-1", vod_name="Song"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        detail_action_runner=lambda current_item, action_id: calls.append((current_item.title, action_id)) or [
            PlaybackDetailAction(id="favorite_track", label="已收藏歌曲", active=True)
        ],
    )

    window.open_session(session)
    button = window.detail_actions_layout.itemAt(0).widget()
    button.click()
    qtbot.waitUntil(lambda: item.detail_actions[0].label == "已收藏歌曲")

    assert calls == [("Track 1", "favorite_track")]
    assert window.detail_actions_layout.itemAt(0).widget().text() == "已收藏歌曲"


def test_player_window_preserves_other_detail_actions_when_refresh_returns_partial_update(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    item = PlayItem(
        title="Track 1",
        url="http://m/1.m3u8",
        detail_actions=[
            PlaybackDetailAction(id="favorite_playlist", label="收藏歌单"),
            PlaybackDetailAction(id="favorite_track", label="收藏歌曲"),
        ],
    )
    session = PlayerSession(
        vod=VodItem(vod_id="song-1", vod_name="Song"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        detail_action_runner=lambda current_item, action_id: [
            PlaybackDetailAction(id="favorite_playlist", label="已收藏歌单", active=True),
            current_item.detail_actions[1],
        ]
        if action_id == "favorite_playlist"
        else list(current_item.detail_actions),
    )

    window.open_session(session)
    button = window.detail_actions_layout.itemAt(0).widget()
    button.click()
    qtbot.waitUntil(lambda: item.detail_actions[0].label == "已收藏歌单")

    assert [item.detail_actions[index].label for index in range(len(item.detail_actions))] == ["已收藏歌单", "收藏歌曲"]
    assert [window.detail_actions_layout.itemAt(i).widget().text() for i in range(window.detail_actions_layout.count())] == [
        "已收藏歌单",
        "收藏歌曲",
    ]


def test_player_window_detail_action_failure_logs_error_without_stopping_playback(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="song-1", vod_name="Song"),
        playlist=[
            PlayItem(
                title="Track 1",
                url="http://m/1.m3u8",
                detail_actions=[PlaybackDetailAction(id="favorite_track", label="收藏歌曲")],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        detail_action_runner=lambda _item, _action_id: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    window.open_session(session)
    window.detail_actions_layout.itemAt(0).widget().click()
    qtbot.waitUntil(lambda: "详情动作执行失败[favorite_track]: boom" in window.log_view.toPlainText())

    assert window.video.load_calls == [("http://m/1.m3u8", 0)]


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


def test_player_window_hides_details_and_docks_log_to_sidebar_bottom(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    window.toggle_details_button.click()

    assert window.playlist.isHidden() is False
    assert window.details.isHidden() is True
    assert window.log_section.isHidden() is False
    assert window.details.layout().indexOf(window.log_section) == -1
    assert window.sidebar_layout.indexOf(window.log_section) == window.sidebar_layout.count() - 1

    window.toggle_details_button.click()

    assert window.details.isHidden() is False
    assert window.details.layout().indexOf(window.log_section) != -1
    assert window.sidebar_layout.indexOf(window.log_section) == -1


def test_player_window_can_hide_only_playback_log_section(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    window.toggle_log_button.click()

    assert window.details.isHidden() is False
    assert window.metadata_view.isHidden() is False
    assert window.log_section.isHidden() is True

    window.toggle_log_button.click()

    assert window.details.isHidden() is False
    assert window.log_section.isHidden() is False


def test_player_window_uses_custom_title_bar(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.title_bar().title_label.text() == "alist-tvbox 播放器"


def test_player_window_title_bar_exposes_return_to_main_button(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.title_bar_return_button.text() == ""
    assert window.title_bar_return_button.property("icon_name") == "home.svg"
    assert window.title_bar_return_button.toolTip() == "返回主窗口 (Ctrl+P)"


def test_player_window_playback_always_on_top_defaults_to_off(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert _player_window_is_always_on_top(window) is False
    assert window.always_on_top_button.isCheckable() is True
    assert window.always_on_top_button.isChecked() is False
    assert window.always_on_top_button.toolTip() == "播放时置顶"


def test_player_window_xcb_maximize_toggle_uses_work_area_and_restores_geometry(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    normal_geometry = QRect(120, 90, 900, 650)
    work_area = QRect(0, 28, 1920, 1052)

    class FakeScreen:
        def availableGeometry(self) -> QRect:
            return QRect(work_area)

    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(window, "screen", lambda: FakeScreen())
    window.setGeometry(normal_geometry)

    window._toggle_maximized()

    assert window._pseudo_maximized is True
    assert window.isMaximized() is False
    assert window.geometry() == work_area
    assert window._is_effectively_maximized() is True
    assert window._can_resize_window() is False

    window._toggle_maximized()

    assert window._pseudo_maximized is False
    assert window.geometry() == normal_geometry
    assert window._is_effectively_maximized() is False
    assert window._can_resize_window() is True


def test_player_window_non_xcb_maximize_toggle_remains_native(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    calls: list[str] = []
    monkeypatch.setattr(QApplication, "platformName", lambda: "wayland")
    monkeypatch.setattr(window, "isMaximized", lambda: False)
    monkeypatch.setattr(window, "showMaximized", lambda: calls.append("maximized"))

    window._toggle_maximized()

    assert calls == ["maximized"]


def test_player_window_xcb_topmost_converts_true_maximize_to_pseudo_maximize(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    normal_geometry = QRect(180, 120, 1000, 700)
    work_area = QRect(0, 30, 1920, 1050)
    calls: list[object] = []
    maximized = {"value": True}

    class FakeScreen:
        def availableGeometry(self) -> QRect:
            return QRect(work_area)

    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(window, "isVisible", lambda: True)
    monkeypatch.setattr(window, "isMaximized", lambda: maximized["value"])
    monkeypatch.setattr(window, "normalGeometry", lambda: QRect(normal_geometry))
    monkeypatch.setattr(window, "screen", lambda: FakeScreen())

    def show_normal() -> None:
        calls.append("normal")
        maximized["value"] = False

    monkeypatch.setattr(window, "showNormal", show_normal)
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: calls.append(("native", enabled)),
    )

    window._set_always_on_top(True)

    assert calls == ["normal", ("native", True)]
    assert window._pseudo_maximized is True
    assert window.geometry() == work_area
    assert window._always_on_top_applied is True


def test_player_window_persists_and_restores_xcb_pseudo_maximize(
    qtbot,
    monkeypatch,
) -> None:
    config = AppConfig()
    normal_geometry = QRect(20, 20, 800, 600)
    work_area = QRect(0, 24, 1280, 696)

    class FakeScreen:
        def availableGeometry(self) -> QRect:
            return QRect(work_area)

    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(PlayerWindow, "screen", lambda _self: FakeScreen())
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.setGeometry(normal_geometry)
    window._toggle_maximized()

    window._persist_geometry()

    assert config.player_window_geometry is not None
    assert config.player_window_geometry.startswith(PlayerWindow._PSEUDO_MAXIMIZED_GEOMETRY_PREFIX)

    restored = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(restored)
    restored.show()
    qtbot.waitUntil(lambda: restored._pseudo_maximized)

    assert restored.isMaximized() is False
    assert restored.geometry() == work_area

    restored._toggle_maximized()

    assert restored._pseudo_maximized is False
    assert restored.geometry() == normal_geometry


def test_player_window_fullscreen_round_trip_restores_xcb_pseudo_maximize(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    work_area = QRect(0, 30, 1280, 690)

    class FakeScreen:
        def availableGeometry(self) -> QRect:
            return QRect(work_area)

    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(window, "screen", lambda: FakeScreen())
    window.show()
    window._toggle_maximized()

    window.toggle_fullscreen()
    assert window.isFullScreen() is True

    window.toggle_fullscreen()

    assert window.isFullScreen() is False
    assert window._pseudo_maximized is True
    assert window.isMaximized() is False
    assert window.geometry() == work_area


def test_player_window_title_bar_button_toggles_playback_always_on_top(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.always_on_top_button.click()
    assert _player_window_is_always_on_top(window) is True
    assert window.always_on_top_button.isChecked() is True
    assert window.always_on_top_button.toolTip() == "取消播放时置顶"

    window.always_on_top_button.click()
    assert _player_window_is_always_on_top(window) is False
    assert window.always_on_top_button.isChecked() is False
    assert window.always_on_top_button.toolTip() == "播放时置顶"


def test_player_window_enabling_playback_topmost_while_paused_defers_native_state(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    native_calls: list[bool] = []
    window.is_playing = False
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )

    window.always_on_top_button.click()

    assert native_calls == []
    assert window._is_always_on_top() is True
    assert window._always_on_top_applied is False
    assert window.always_on_top_button.isChecked() is True
    assert window.always_on_top_button.toolTip() == "取消播放时置顶"


def test_player_window_always_on_top_preserves_mpv_native_surface(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(30)
    player_window_id = int(window.winId())
    video_window_id = int(window.video_widget.winId())
    qwidget_flag_calls: list[tuple[Qt.WindowType, bool]] = []
    video_shutdown_calls: list[bool] = []

    monkeypatch.setattr(
        window,
        "setWindowFlag",
        lambda flag, enabled: qwidget_flag_calls.append((flag, enabled)),
    )
    monkeypatch.setattr(
        window.video_widget,
        "shutdown",
        lambda: video_shutdown_calls.append(True),
    )

    window._set_always_on_top(True)
    qtbot.wait(30)

    assert qwidget_flag_calls == []
    assert video_shutdown_calls == []
    assert int(window.winId()) == player_window_id
    assert int(window.video_widget.winId()) == video_window_id
    assert window.isVisible() is True
    assert window._is_always_on_top() is True


def test_player_window_xcb_always_on_top_uses_ewmh_across_hide_and_show(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(30)
    ewmh_calls: list[tuple[int, bool]] = []
    qwindow_flag_calls: list[tuple[Qt.WindowType, bool]] = []

    class FakeWindowHandle:
        def flags(self) -> Qt.WindowType:
            return Qt.WindowType.Widget

        def setFlag(self, flag: Qt.WindowType, enabled: bool) -> None:
            qwindow_flag_calls.append((flag, enabled))

    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(window, "windowHandle", lambda: FakeWindowHandle())
    monkeypatch.setattr(
        player_window_module,
        "set_x11_window_above",
        lambda window_id, enabled: ewmh_calls.append((window_id, enabled)),
        raising=False,
    )
    window_id = int(window.winId())

    window._set_always_on_top(True)
    window.hide()
    window.show()
    window._set_always_on_top(False)

    assert ewmh_calls == [(window_id, True), (window_id, False)]
    assert qwindow_flag_calls == []
    assert int(window.winId()) == window_id
    assert window.isVisible() is True
    assert window._is_always_on_top() is False


def test_player_window_reapplies_xcb_always_on_top_after_show(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(30)
    ewmh_calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(
        player_window_module,
        "set_x11_window_above",
        lambda window_id, enabled: ewmh_calls.append((window_id, enabled)),
        raising=False,
    )
    window_id = int(window.winId())

    window._set_always_on_top(True)
    ewmh_calls.clear()
    window.hide()
    window.show()
    qtbot.wait(30)

    assert ewmh_calls == [(window_id, True)]
    assert window._is_always_on_top() is True


def test_player_window_does_not_reapply_playback_topmost_after_show_while_paused(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.show()
    qtbot.wait(30)
    native_calls: list[bool] = []
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )
    window._set_always_on_top(True)
    window.toggle_playback()
    native_calls.clear()

    window.hide()
    window.show()
    qtbot.wait(30)

    assert window._is_always_on_top() is True
    assert window._always_on_top_applied is False
    assert native_calls == []


def test_player_window_xcb_topmost_does_not_remap_minimized_maximized_window(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    calls: list[str] = []
    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(window, "isVisible", lambda: True)
    monkeypatch.setattr(window, "isMaximized", lambda: True)
    monkeypatch.setattr(window, "isMinimized", lambda: True)
    monkeypatch.setattr(window, "hide", lambda: calls.append("hide"))
    monkeypatch.setattr(window, "showMaximized", lambda: calls.append("showMaximized"))
    monkeypatch.setattr(window, "_set_native_always_on_top", lambda _enabled: None)

    window._set_always_on_top(True)

    assert calls == []


def test_player_window_logs_failed_topmost_reapply_without_stealing_activation(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    calls: list[str] = []
    messages: list[str] = []
    window._always_on_top_enabled = True
    window._always_on_top_applied = True
    window.is_playing = True
    monkeypatch.setattr(window, "isVisible", lambda: True)
    monkeypatch.setattr(window, "isMinimized", lambda: False)

    def fail_native_state(_enabled: bool) -> None:
        raise RuntimeError("unsupported")

    monkeypatch.setattr(window, "_set_native_always_on_top", fail_native_state)
    monkeypatch.setattr(window, "raise_", lambda: calls.append("raise"))
    monkeypatch.setattr(window, "activateWindow", lambda: calls.append("activate"))
    monkeypatch.setattr(window, "_append_log", messages.append)

    window._reapply_always_on_top_after_show()

    assert calls == []
    assert messages == ["恢复置顶失败: unsupported"]
    assert window._always_on_top_applied is False


def test_player_window_always_on_top_does_not_persist_to_config(qtbot) -> None:
    saved: list[bool] = []
    config = AppConfig()
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.append(True),
    )
    qtbot.addWidget(window)

    window.always_on_top_button.click()

    assert saved == []
    assert not hasattr(config, "player_always_on_top")


def test_player_window_always_on_top_failure_restores_actual_state(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    messages: list[str] = []

    def fail_to_set_native_always_on_top(*_args, **_kwargs) -> None:
        raise RuntimeError("unsupported")

    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        fail_to_set_native_always_on_top,
    )
    monkeypatch.setattr(window, "_append_log", messages.append)

    window.always_on_top_button.click()

    assert _player_window_is_always_on_top(window) is False
    assert window.always_on_top_button.isChecked() is False
    assert window.always_on_top_button.toolTip() == "播放时置顶"
    assert messages == ["置顶切换失败: unsupported"]


def test_player_window_xcb_topmost_failure_restores_true_maximized_state(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    maximized = {"value": True}
    calls: list[str] = []

    class FakeScreen:
        def availableGeometry(self) -> QRect:
            return QRect(0, 24, 1280, 696)

    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(window, "isVisible", lambda: True)
    monkeypatch.setattr(window, "isMaximized", lambda: maximized["value"])
    monkeypatch.setattr(window, "isMinimized", lambda: False)
    monkeypatch.setattr(window, "normalGeometry", lambda: QRect(100, 80, 900, 650))
    monkeypatch.setattr(window, "screen", lambda: FakeScreen())

    def show_normal() -> None:
        calls.append("normal")
        maximized["value"] = False

    def show_maximized() -> None:
        calls.append("maximized")
        maximized["value"] = True

    def fail_native_state(_enabled: bool) -> None:
        calls.append("native")
        raise RuntimeError("unsupported")

    monkeypatch.setattr(window, "showNormal", show_normal)
    monkeypatch.setattr(window, "showMaximized", show_maximized)
    monkeypatch.setattr(window, "_set_native_always_on_top", fail_native_state)
    monkeypatch.setattr(window, "_append_log", lambda _message: None)

    window._set_always_on_top(True)

    assert calls == ["normal", "native", "maximized"]
    assert window._pseudo_maximized is False
    assert window.isMaximized() is True
    assert window._is_always_on_top() is False


def test_player_window_context_menu_playback_topmost_syncs_with_title_bar(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    menu = window._build_video_context_menu()
    action = next(item for item in menu.actions() if item.text() == "播放时置顶")

    assert action.isCheckable() is True
    assert action.isChecked() is False

    window.always_on_top_button.click()
    assert action.isChecked() is True

    action.trigger()
    assert _player_window_is_always_on_top(window) is False
    assert window.always_on_top_button.isChecked() is False


def test_player_window_always_on_top_survives_hide_but_not_new_instance(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.always_on_top_button.click()
    window.hide()

    assert _player_window_is_always_on_top(window) is True
    window.show()
    assert _player_window_is_always_on_top(window) is True

    new_window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(new_window)
    assert _player_window_is_always_on_top(new_window) is False


@pytest.mark.parametrize("state", ["normal", "maximized", "fullscreen"])
def test_player_window_always_on_top_preserves_window_state(qtbot, state: str) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    if state == "maximized":
        window.showMaximized()
    elif state == "fullscreen":
        window.showFullScreen()
    qtbot.wait(30)

    before = (
        window.isVisible(),
        window.isMinimized(),
        window.isMaximized(),
        window.isFullScreen(),
    )
    window._set_always_on_top(True)
    qtbot.wait(30)

    assert (
        window.isVisible(),
        window.isMinimized(),
        window.isMaximized(),
        window.isFullScreen(),
    ) == before


def test_player_window_xcb_always_on_top_does_not_show_hidden_maximized_window(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(window, "isVisible", lambda: False)
    monkeypatch.setattr(window, "isMaximized", lambda: True)
    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(window, "showNormal", lambda: calls.append(("normal", None)))
    monkeypatch.setattr(
        window,
        "showMaximized",
        lambda: calls.append(("maximized", None)),
    )
    monkeypatch.setattr(
        window,
        "setWindowState",
        lambda state: calls.append(("state", state)),
    )
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: calls.append(("native", enabled)),
    )

    window._set_always_on_top(True)

    assert calls == [("native", True)]


def test_player_window_xcb_always_on_top_does_not_remap_real_maximized_window(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(window, "isVisible", lambda: True)
    monkeypatch.setattr(window, "isMaximized", lambda: True)
    monkeypatch.setattr(window, "isMinimized", lambda: False)
    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(window, "hide", lambda: calls.append(("hide", None)))
    monkeypatch.setattr(
        window,
        "setWindowState",
        lambda state: calls.append(("state", state)),
    )
    monkeypatch.setattr(
        window,
        "showMaximized",
        lambda: calls.append(("maximized", window._is_always_on_top())),
    )
    monkeypatch.setattr(window, "raise_", lambda: calls.append(("raise", None)))
    monkeypatch.setattr(
        window,
        "activateWindow",
        lambda: calls.append(("activate", None)),
    )
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: calls.append(("native", enabled)),
    )

    window._set_always_on_top(True)

    assert calls == [("native", True), ("raise", None)]
    assert window._pseudo_maximized is True

    window._reapply_always_on_top_after_show()

    assert calls[-1:] == [("native", True)]


def test_player_window_xcb_pseudo_maximized_resume_only_reapplies_topmost(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(window, "isVisible", lambda: True)
    monkeypatch.setattr(window, "isMaximized", lambda: True)
    monkeypatch.setattr(window, "isMinimized", lambda: False)
    monkeypatch.setattr(window, "isActiveWindow", lambda: True)
    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(window, "hide", lambda: calls.append(("hide", None)))
    monkeypatch.setattr(
        window,
        "showMaximized",
        lambda: calls.append(("maximized", window._is_always_on_top())),
    )
    monkeypatch.setattr(window, "raise_", lambda: calls.append(("raise", None)))
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: calls.append(("native", enabled)),
    )
    window._set_always_on_top(True)
    calls.clear()

    window.toggle_playback()
    window.toggle_playback()

    assert calls == [
        ("native", False),
        ("native", True),
        ("raise", None),
    ]


def test_player_window_xcb_true_to_pseudo_conversion_preserves_native_surface_ids(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible)
    player_window_id = int(window.winId())
    video_window_id = int(window.video_widget.winId())
    maximized = {"value": True}
    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    monkeypatch.setattr(window, "isMaximized", lambda: maximized["value"])
    monkeypatch.setattr(window, "normalGeometry", lambda: QRect(100, 80, 900, 650))

    def show_normal() -> None:
        maximized["value"] = False

    monkeypatch.setattr(window, "showNormal", show_normal)
    monkeypatch.setattr(window, "_set_native_always_on_top", lambda _enabled: None)

    window._set_always_on_top(True)
    qtbot.wait(30)

    assert window.isVisible() is True
    assert window.isMaximized() is False
    assert window._pseudo_maximized is True
    assert int(window.winId()) == player_window_id
    assert int(window.video_widget.winId()) == video_window_id


def test_player_window_enables_resize_support(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.is_window_resizable() is True


def test_player_window_runtime_dialogs_use_custom_title_bars(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    danmaku_settings = window._ensure_danmaku_settings_dialog()
    metadata_scrape = window._ensure_metadata_scrape_dialog()
    danmaku_source = window._ensure_danmaku_source_dialog()

    assert danmaku_settings.title_bar().title_label.text() == "弹幕设置"
    assert metadata_scrape.title_bar().title_label.text() == "刮削"
    assert danmaku_source.title_bar().title_label.text() == "弹幕源"


def test_player_window_runtime_dialogs_hide_maximize_buttons(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    danmaku_settings = window._ensure_danmaku_settings_dialog()
    metadata_scrape = window._ensure_metadata_scrape_dialog()
    danmaku_source = window._ensure_danmaku_source_dialog()

    assert danmaku_settings.title_bar().maximize_button.isHidden() is True
    assert metadata_scrape.title_bar().maximize_button.isHidden() is True
    assert danmaku_source.title_bar().maximize_button.isHidden() is True


def test_player_window_toggle_fullscreen_changes_window_state(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    window.toggle_details_button.click()

    window.toggle_fullscreen()
    assert window.isFullScreen() is True
    assert window.title_bar().isHidden() is True
    assert window.bottom_area.isHidden() is True
    assert window.sidebar_actions_widget.isHidden() is True
    assert window.playlist.isHidden() is True
    assert window.details.isHidden() is True

    window.toggle_fullscreen()
    assert window.isFullScreen() is False
    assert window.title_bar().isVisible() is True
    assert window.bottom_area.isHidden() is False
    assert window.sidebar_actions_widget.isHidden() is False
    assert window.playlist.isHidden() is False
    assert window.details.isHidden() is True
    assert window.log_section.isHidden() is False
    assert window.details.layout().indexOf(window.log_section) == -1
    assert window.sidebar_layout.indexOf(window.log_section) == window.sidebar_layout.count() - 1


def test_player_window_hides_details_in_fullscreen_even_when_log_toggle_is_on(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    assert window.toggle_log_button.isChecked() is True

    window.toggle_fullscreen()

    assert window.isFullScreen() is True
    assert window.sidebar_actions_widget.isHidden() is True
    assert window.details.isHidden() is True
    assert window.log_section.isHidden() is True


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


def test_player_window_window_state_change_reapplies_visibility_state(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    window.bottom_area.hide()
    window.sidebar_actions_widget.hide()
    window.title_bar().hide()

    QApplication.sendEvent(window, QEvent(QEvent.Type.WindowStateChange))

    assert window.isFullScreen() is False
    assert window.bottom_area.isHidden() is False
    assert window.sidebar_actions_widget.isHidden() is False
    assert window.title_bar().isVisible() is True


def test_player_window_window_state_change_reapplies_playback_topmost(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    native_calls: list[bool] = []
    window._always_on_top_enabled = True
    window._always_on_top_applied = True
    window.is_playing = True
    monkeypatch.setattr(window, "isVisible", lambda: True)
    monkeypatch.setattr(window, "isMinimized", lambda: False)
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )
    monkeypatch.setattr(
        QTimer,
        "singleShot",
        staticmethod(lambda _delay, callback: callback()),
    )

    QApplication.sendEvent(window, QEvent(QEvent.Type.WindowStateChange))

    assert native_calls == [True]
    assert window._is_always_on_top() is True
    assert window._always_on_top_applied is True


def test_player_window_coalesces_playback_topmost_reapply_requests(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    callbacks: list[object] = []
    native_calls: list[bool] = []
    window._always_on_top_enabled = True
    window._always_on_top_applied = True
    window.is_playing = True
    monkeypatch.setattr(window, "isVisible", lambda: True)
    monkeypatch.setattr(window, "isMinimized", lambda: False)
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )
    monkeypatch.setattr(
        QTimer,
        "singleShot",
        staticmethod(lambda _delay, callback: callbacks.append(callback)),
    )

    window._schedule_always_on_top_reapply()
    window._schedule_always_on_top_reapply()

    assert len(callbacks) == 1
    callback = callbacks.pop()
    assert callable(callback)
    callback()
    assert native_calls == [True]


@pytest.mark.parametrize(
    ("enabled", "is_playing", "is_minimized"),
    [
        (False, True, False),
        (True, False, False),
        (True, True, True),
    ],
)
def test_player_window_window_state_change_skips_inactive_topmost_mode(
    qtbot,
    monkeypatch,
    enabled: bool,
    is_playing: bool,
    is_minimized: bool,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    callbacks: list[object] = []
    window._always_on_top_enabled = enabled
    window.is_playing = is_playing
    monkeypatch.setattr(window, "isMinimized", lambda: is_minimized)
    monkeypatch.setattr(
        QTimer,
        "singleShot",
        staticmethod(lambda _delay, callback: callbacks.append(callback)),
    )

    QApplication.sendEvent(window, QEvent(QEvent.Type.WindowStateChange))

    assert callbacks == []


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


def test_player_window_ignores_playback_finished_immediately_after_progress_seek(qtbot) -> None:
    class SeekableRecordingVideo(RecordingVideo):
        def __init__(self) -> None:
            super().__init__()
            self.seek_calls: list[int] = []
            self._duration = 120

        def seek(self, seconds: int) -> None:
            self.seek_calls.append(seconds)

        def duration_seconds(self) -> int:
            return self._duration

    controller = RecordingPlayerController()
    video = SeekableRecordingVideo()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))

    video.load_calls.clear()
    window.progress.setValue(75)

    window._seek_from_slider()
    window.video_widget.playback_finished.emit()

    assert window.current_index == 0
    assert window.playlist.currentRow() == 0
    assert video.load_calls == []
    assert video.seek_calls == [75]


def test_player_window_uses_max_observed_duration_after_proxy_duration_shrinks(qtbot) -> None:
    class ShrinkingDurationVideo(RecordingVideo):
        def __init__(self) -> None:
            super().__init__()
            self.duration = 2681
            self.position = 1000

        def duration_seconds(self) -> int:
            return self.duration

        def position_seconds(self) -> int:
            return self.position

    video = ShrinkingDurationVideo()
    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))

    window._sync_progress_slider()
    video.duration = 1100
    video.position = 1003
    window._sync_progress_slider()

    assert window.current_index == 0
    assert window.progress.maximum() == 2681
    assert window.duration_label.text() == "44:41"


def test_player_window_reloads_current_item_after_premature_eof(qtbot) -> None:
    class PrematureEofVideo(RecordingVideo):
        def duration_seconds(self) -> int:
            return 2681

        def position_seconds(self) -> int:
            return 1003

    video = PrematureEofVideo()
    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))
    video.load_calls.clear()
    window._sync_progress_slider()

    window.video_widget.playback_finished.emit()

    assert window.current_index == 0
    assert video.load_calls == [("http://m/1.m3u8", 1003)]
    assert "播放提前结束，正在恢复" in window.log_view.toPlainText()


def test_player_window_advances_when_mpv_resets_position_before_normal_eof(qtbot) -> None:
    class ResettingNearEndVideo(RecordingVideo):
        def __init__(self) -> None:
            super().__init__()
            self.position = 2679

        def duration_seconds(self) -> int:
            return 2681

        def position_seconds(self) -> int:
            return self.position

    video = ResettingNearEndVideo()
    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))
    video.load_calls.clear()
    window._sync_progress_slider()

    video.position = 0
    window._sync_progress_slider()
    window.video_widget.playback_finished.emit()

    assert window.current_index == 1
    assert video.load_calls == [("http://m/2.m3u8", 0)]
    assert "播放提前结束，正在恢复" not in window.log_view.toPlainText()


def test_player_window_recovers_last_position_when_mpv_resets_before_premature_eof(qtbot) -> None:
    class ResettingPrematureEofVideo(RecordingVideo):
        def __init__(self) -> None:
            super().__init__()
            self.position = 1003

        def duration_seconds(self) -> int:
            return 2681

        def position_seconds(self) -> int:
            return self.position

    video = ResettingPrematureEofVideo()
    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))
    video.load_calls.clear()
    window._sync_progress_slider()

    video.position = 0
    window._sync_progress_slider()
    window.video_widget.playback_finished.emit()

    assert window.current_index == 0
    assert video.load_calls == [("http://m/1.m3u8", 1003)]
    assert "播放提前结束，正在恢复: 16:43 / 44:41" in window.log_view.toPlainText()


def test_player_window_stops_after_repeated_premature_eof(qtbot, monkeypatch) -> None:
    class PrematureEofVideo(RecordingVideo):
        def duration_seconds(self) -> int:
            return 2681

        def position_seconds(self) -> int:
            return 1003

    controller = RecordingPlayerController()
    video = PrematureEofVideo()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))
    video.load_calls.clear()
    window._sync_progress_slider()
    native_calls: list[bool] = []
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )
    window._set_always_on_top(True)
    native_calls.clear()

    window.video_widget.playback_finished.emit()
    video.load_calls.clear()
    window.video_widget.playback_finished.emit()

    assert window.current_index == 0
    assert video.load_calls == []
    assert window.is_playing is False
    assert native_calls == [False]
    assert "播放提前结束，恢复失败" in window.log_view.toPlainText()


def test_player_window_stops_when_premature_eof_reload_fails(qtbot, monkeypatch) -> None:
    class ReloadFailingVideo(RecordingVideo):
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            super().load(url, pause=pause, start_seconds=start_seconds)
            if start_seconds > 0:
                raise RuntimeError("reload failed")

        def duration_seconds(self) -> int:
            return 2681

        def position_seconds(self) -> int:
            return 1003

    video = ReloadFailingVideo()
    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))
    window._sync_progress_slider()
    native_calls: list[bool] = []
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )
    window._set_always_on_top(True)
    native_calls.clear()

    window.video_widget.playback_finished.emit()

    assert window.current_index == 0
    assert window.is_playing is False
    assert native_calls == [False]
    assert "播放提前结束，恢复失败: reload failed" in window.log_view.toPlainText()


def test_player_window_advances_when_eof_is_near_observed_end(qtbot) -> None:
    class NearEndVideo(RecordingVideo):
        def duration_seconds(self) -> int:
            return 2681

        def position_seconds(self) -> int:
            return 2679

    video = NearEndVideo()
    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))
    video.load_calls.clear()
    window._sync_progress_slider()

    window.video_widget.playback_finished.emit()

    assert window.current_index == 1
    assert video.load_calls == [("http://m/2.m3u8", 0)]


def test_player_window_keeps_existing_eof_behavior_when_duration_is_unknown(qtbot) -> None:
    class UnknownDurationVideo(RecordingVideo):
        def duration_seconds(self) -> int:
            return 0

        def position_seconds(self) -> int:
            return 1003

    video = UnknownDurationVideo()
    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))
    video.load_calls.clear()
    window._sync_progress_slider()

    window.video_widget.playback_finished.emit()

    assert window.current_index == 1


def test_player_window_marks_source_unplayable_when_eof_without_progress(qtbot) -> None:
    class DeadSourceVideo(RecordingVideo):
        def duration_seconds(self) -> int:
            return 134

        def position_seconds(self) -> int:
            return 0

    video = DeadSourceVideo()
    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))
    video.load_calls.clear()
    window._sync_progress_slider()

    window.video_widget.playback_finished.emit()

    assert window.current_index == 0
    assert video.load_calls == []
    assert window.is_playing is False
    assert (
        window._startup_state.stage
        is player_window_module.PlaybackStartupStage.FAILED
    )
    log_text = window.log_view.toPlainText()
    assert "播放失败: 无法解码任何内容" in log_text
    assert "播放提前结束" not in log_text


def test_player_window_does_not_auto_advance_when_eof_without_progress_and_duration_unknown(
    qtbot,
) -> None:
    class DeadSourceUnknownDurationVideo(RecordingVideo):
        def duration_seconds(self) -> int:
            return 0

        def position_seconds(self) -> int:
            return 0

    video = DeadSourceUnknownDurationVideo()
    window = PlayerWindow(RecordingPlayerController())
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))
    video.load_calls.clear()
    window._sync_progress_slider()

    window.video_widget.playback_finished.emit()

    assert window.current_index == 0
    assert video.load_calls == []
    assert window.is_playing is False
    assert "播放失败: 无法解码任何内容" in window.log_view.toPlainText()


def test_player_window_recovers_current_item_when_playback_fails_after_progress_seek(
    qtbot,
    monkeypatch,
) -> None:
    class SeekableRecordingVideo(RecordingVideo):
        def __init__(self) -> None:
            super().__init__()
            self.seek_calls: list[int] = []

        def seek(self, seconds: int) -> None:
            self.seek_calls.append(seconds)

        def duration_seconds(self) -> int:
            return 120

    controller = RecordingPlayerController()
    video = SeekableRecordingVideo()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))
    auto_switch_calls: list[bool] = []
    monkeypatch.setattr(
        window,
        "_try_auto_switch_source_after_failure",
        lambda: auto_switch_calls.append(True) or False,
    )

    video.load_calls.clear()
    window.progress.setMaximum(120)
    window.progress.setValue(75)

    window._seek_from_slider()
    window._handle_playback_failed("播放失败: 没有可播放的音视频流 (-16)")

    assert window.current_index == 0
    assert window.playlist.currentRow() == 0
    assert video.seek_calls == [75]
    assert video.load_calls == [("http://m/1.m3u8", 75)]
    assert auto_switch_calls == []
    assert "没有可播放的音视频流 (-16)" in window.log_view.toPlainText()
    assert "正在恢复播放进度: 01:15" in window.log_view.toPlainText()


def test_player_window_reloads_current_item_when_seek_finished_unloads_media(qtbot) -> None:
    class SeekableRecordingVideo(RecordingVideo):
        def __init__(self) -> None:
            super().__init__()
            self.seek_calls: list[int] = []
            self._duration = 120

        def seek(self, seconds: int) -> None:
            self.seek_calls.append(seconds)
            self._duration = 0

        def duration_seconds(self) -> int:
            return self._duration

    controller = RecordingPlayerController()
    video = SeekableRecordingVideo()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))

    video.load_calls.clear()
    window.progress.setMaximum(120)
    window.progress.setValue(75)

    window._seek_from_slider()
    window.video_widget.playback_finished.emit()

    assert window.current_index == 0
    assert window.playlist.currentRow() == 0
    assert video.seek_calls == [75]
    assert video.load_calls == [("http://m/1.m3u8", 75)]


def test_player_window_reloads_current_item_when_progress_seek_fails_after_unloading_media(qtbot) -> None:
    class SeekFailingVideo(RecordingVideo):
        def __init__(self) -> None:
            super().__init__()
            self.seek_calls: list[int] = []
            self._duration = 120

        def seek(self, seconds: int) -> None:
            self.seek_calls.append(seconds)
            self._duration = 0
            raise RuntimeError("('Error running mpv command', -12)")

        def duration_seconds(self) -> int:
            return self._duration

    controller = RecordingPlayerController()
    video = SeekFailingVideo()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))

    video.load_calls.clear()
    window.progress.setMaximum(120)
    window.progress.setValue(75)

    window._seek_from_slider()

    assert window.current_index == 0
    assert window.playlist.currentRow() == 0
    assert video.seek_calls == [75]
    assert video.load_calls == [("http://m/1.m3u8", 75)]
    assert "跳转失败" in window.log_view.toPlainText()
    assert "正在恢复播放进度: 01:15" in window.log_view.toPlainText()


def test_player_window_advances_after_progress_seek_near_end(qtbot) -> None:
    class SeekableRecordingVideo(RecordingVideo):
        def __init__(self) -> None:
            super().__init__()
            self.seek_calls: list[int] = []
            self._position = 30

        def seek(self, seconds: int) -> None:
            self.seek_calls.append(seconds)
            self._position = seconds

        def position_seconds(self) -> int:
            return self._position

    controller = RecordingPlayerController()
    video = SeekableRecordingVideo()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = video
    window.open_session(make_player_session(start_index=0))

    video.load_calls.clear()
    window.progress.setMaximum(120)
    window.progress.setValue(119)

    window._seek_from_slider()
    window._sync_progress_slider()
    window.video_widget.playback_finished.emit()

    assert window.current_index == 1
    assert window.playlist.currentRow() == 1
    assert video.load_calls == [("http://m/2.m3u8", 0)]
    assert video.seek_calls == [119]


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
    assert window.toggle_log_button.text() == ""
    assert window.play_button.toolTip() == "播放/暂停 (Space)"
    assert window.mute_button.toolTip() == "静音 (M)"
    assert window.fullscreen_button.toolTip() == "全屏 (Enter)"
    assert window.toggle_log_button.toolTip() == "播放日志"
    assert window.toggle_log_button.isCheckable() is True
    assert window.toggle_log_button.isChecked() is True
    assert isinstance(window.speed_combo, QComboBox)
    assert window.volume_slider.maximum() == 100


def test_player_window_uses_primary_style_for_play_button_and_tinted_icons(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.play_button.property("control_role") == "primary"
    assert window.prev_button.property("control_role") == "secondary"
    assert window.fullscreen_button.icon().pixmap(24, 24).toImage() != QIcon(
        str(window._icons_dir / "maximize.svg")
    ).pixmap(24, 24).toImage()


def test_player_window_applies_strong_feedback_slider_styles(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert "QSlider {\n        background: transparent;\n        border: none;" in window.progress.styleSheet()
    assert "QSlider::sub-page:horizontal" in window.progress.styleSheet()
    assert "QSlider::handle:horizontal:hover" in window.progress.styleSheet()
    assert "QSlider::groove:horizontal {\n        height: 4px;\n        border: none;\n        border-radius: 2px;\n        background: transparent;" in window.progress.styleSheet()
    assert "height: 4px" in window.progress.styleSheet()
    assert "width: 12px" in window.progress.styleSheet()
    assert "height: 4px" in window.volume_slider.styleSheet()


def test_player_window_progress_row_background_matches_immersive_panel(qtbot) -> None:
    from atv_player.ui.theme import ThemeManager

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.resize(1600, 120)
    window.show()
    qtbot.waitExposed(window)
    QApplication.processEvents()

    sample = window.progress.mapTo(window, QPoint(window.progress.width() // 2, 2))
    color = window.grab().toImage().pixelColor(sample)
    tokens = ThemeManager(system_theme_getter=lambda: "light").player_tokens_for("light")

    assert color.name() == QColor(tokens.player_overlay_bg).name()


def test_player_window_progress_slider_renders_remaining_track(qtbot) -> None:
    from atv_player.ui.theme import ThemeManager

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.progress.setRange(0, 100)
    window.progress.setValue(25)
    window.progress.setFixedSize(200, 24)
    window.show()
    qtbot.waitExposed(window)
    QApplication.processEvents()

    image = window.progress.grab().toImage()
    center_y = window.progress.height() // 2
    remaining_x = window.progress.width() - 24
    tokens = ThemeManager(system_theme_getter=lambda: "light").player_tokens_for("light")

    assert image.pixelColor(20, center_y).name() == QColor(tokens.accent).name()
    assert image.pixelColor(remaining_x, center_y).name() == QColor(tokens.player_button_border).name()


def test_player_window_volume_slider_renders_remaining_track(qtbot) -> None:
    from atv_player.ui.theme import ThemeManager

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.volume_slider.setValue(35)
    window.volume_slider.setFixedSize(180, 24)
    window.show()
    qtbot.waitExposed(window)
    QApplication.processEvents()

    image = window.volume_slider.grab().toImage()
    center_y = window.volume_slider.height() // 2
    remaining_x = window.volume_slider.width() - 24
    tokens = ThemeManager(system_theme_getter=lambda: "light").player_tokens_for("light")

    assert image.pixelColor(20, center_y).name() == QColor(tokens.accent).name()
    assert image.pixelColor(remaining_x, center_y).name() == QColor(tokens.player_button_border).name()


def test_player_window_uses_readable_density_for_control_combos(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.playlist_group_combo.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    assert window.playlist_group_combo.minimumContentsLength() == 10
    assert window.playlist_source_combo.minimumContentsLength() == 12
    assert window.speed_combo.minimumContentsLength() == 3
    assert window.subtitle_combo.minimumContentsLength() == 2
    assert window.danmaku_combo.minimumContentsLength() == 2
    assert window.video_quality_combo.minimumContentsLength() == 3
    assert window.audio_combo.minimumContentsLength() == 2
    assert window.parse_combo.minimumContentsLength() == 2
    assert window.speed_combo.maximumWidth() == 72
    assert window.subtitle_combo.maximumWidth() == 74
    assert window.danmaku_combo.maximumWidth() == 72
    assert window.video_quality_combo.maximumWidth() == 84
    assert window.audio_combo.maximumWidth() == 74
    assert window.parse_combo.maximumWidth() == 72
    assert "min-height: 28px" in window.speed_combo.styleSheet()


def test_player_window_control_combo_text_fits_rendered_edit_field(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible)

    for name in (
        "speed_combo",
        "subtitle_combo",
        "danmaku_combo",
        "video_quality_combo",
        "audio_combo",
        "parse_combo",
    ):
        combo = getattr(window, name)
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        edit_rect = combo.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            combo,
        )
        assert edit_rect.width() >= combo.fontMetrics().horizontalAdvance(combo.currentText()), name


def test_player_window_exposes_subtitle_combo_with_default_auto_entry(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert isinstance(window.subtitle_combo, QComboBox)
    assert window.subtitle_combo.count() == 1
    assert window.subtitle_combo.itemText(0) == "字幕"
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
        "6行",
        "7行",
        "8行",
        "9行",
        "10行",
    ]
    assert window.danmaku_combo.isEnabled() is False
    assert control_layout.indexOf(window.danmaku_combo) == control_layout.indexOf(window.subtitle_combo) + 1


def test_player_window_exposes_audio_combo_with_default_auto_entry(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert isinstance(window.audio_combo, QComboBox)
    assert window.audio_combo.count() == 1
    assert window.audio_combo.itemText(0) == "音轨"
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
    assert window.parse_combo.isEnabled() is False


def test_player_window_enables_parse_combo_for_current_parse_required_item(qtbot) -> None:
    class FakeParserService:
        def parsers(self):
            return [type("Parser", (), {"key": "jx1", "label": "jx1"})()]

    window = PlayerWindow(FakePlayerController(), config=AppConfig(), playback_parser_service=FakeParserService())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="https://media.example/1.m3u8", parse_required=True)],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)

    assert window.parse_combo.isEnabled() is True


def test_player_window_disables_parse_combo_when_switching_to_non_parse_item(qtbot) -> None:
    class FakeParserService:
        def parsers(self):
            return [type("Parser", (), {"key": "jx1", "label": "jx1"})()]

    window = PlayerWindow(FakePlayerController(), config=AppConfig(), playback_parser_service=FakeParserService())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(title="Episode 1", url="https://media.example/1.m3u8", parse_required=True),
            PlayItem(title="Episode 2", url="https://media.example/2.m3u8"),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)
    assert window.parse_combo.isEnabled() is True

    window._play_item_at_index(1)

    assert window.parse_combo.isEnabled() is False


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
    window.parse_combo.setEnabled(True)

    window.parse_combo.setCurrentIndex(2)

    assert config.preferred_parse_key == "jx1"
    assert saved["called"] == 1


def test_player_window_replays_parse_required_item_when_user_switches_parser(qtbot) -> None:
    config = AppConfig()
    replayed: list[bool] = []

    class FakeParserService:
        def parsers(self):
            return [
                type("Parser", (), {"key": "fish", "label": "fish"})(),
                type("Parser", (), {"key": "jx1", "label": "jx1"})(),
            ]

    window = PlayerWindow(FakePlayerController(), config=config, playback_parser_service=FakeParserService())
    qtbot.addWidget(window)
    window.session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="https://media.example/1.m3u8", parse_required=True)],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        playback_loader=lambda item: None,
    )
    window.current_index = 0
    window._replay_current_item = lambda: replayed.append(True)
    window.parse_combo.setEnabled(True)

    window.parse_combo.setCurrentIndex(2)

    assert config.preferred_parse_key == "jx1"
    assert replayed == [True]


def test_player_window_renders_failed_startup_actions_for_parse_item_with_multiple_lines(qtbot) -> None:
    class FakeParserService:
        def parsers(self):
            return [type("Parser", (), {"key": "jx1", "label": "jx1"})()]

    window = PlayerWindow(FakePlayerController(), config=AppConfig(), playback_parser_service=FakeParserService())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="vod-1", vod_name="测试剧"),
        playlist=[PlayItem(title="第1集", url="https://stream.example/1.m3u8", parse_required=True)],
        playlists=[
            [PlayItem(title="第1集", url="https://stream.example/1.m3u8", parse_required=True)],
            [PlayItem(title="第1集", url="https://backup.example/1.m3u8", parse_required=True)],
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        source_groups=[
            PlaybackSourceGroup(
                label="默认",
                sources=[
                    PlaybackSource(
                        label="线路1",
                        playlist=[PlayItem(title="第1集", url="https://stream.example/1.m3u8", parse_required=True)],
                    ),
                    PlaybackSource(
                        label="线路2",
                        playlist=[PlayItem(title="第1集", url="https://backup.example/1.m3u8", parse_required=True)],
                    ),
                ],
            )
        ],
    )

    window.open_session(session)
    window._show_failed_startup_state("当前线路响应超时")

    assert window._startup_state.message == "当前线路响应超时"
    assert window.playback_retry_button.isHidden() is False
    assert window.playback_switch_line_button.isHidden() is False
    assert window.playback_switch_parser_button.isHidden() is False


def test_player_window_retry_action_replays_current_item(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig())
    qtbot.addWidget(window)

    replay_calls: list[str] = []
    monkeypatch.setattr(window, "_replay_current_item", lambda: replay_calls.append("replayed"))

    window._show_failed_startup_state("解析器未返回可播放地址")
    window.playback_retry_button.click()

    assert replay_calls == ["replayed"]


def test_player_window_hides_failure_actions_when_video_becomes_visible(qtbot) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig())
    qtbot.addWidget(window)

    window._show_failed_startup_state("播放失败")
    assert window.playback_retry_button.isHidden() is False

    window._handle_video_picture_state_changed("visible")

    assert window._startup_state.message == "播放中"
    assert window.playback_retry_button.isHidden() is True
    assert window.playback_switch_line_button.isHidden() is True
    assert window.playback_switch_parser_button.isHidden() is True


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


def test_player_window_populates_audio_combo_from_ytdlp_audio_tracks(qtbot) -> None:
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
            return []

        def apply_audio_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.audio_apply_calls.append((mode, track_id))
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://www.youtube.com/watch?v=test123",
                original_url="https://www.youtube.com/watch?v=test123",
                audio_tracks=[
                    YtdlpAudioTrackOption(id="ytdlp_audio_en_140", label="English Original", lang="en", is_original=True),
                    YtdlpAudioTrackOption(id="ytdlp_audio_zh_141", label="中文配音", lang="zh"),
                ],
                selected_audio_track_id="ytdlp_audio_en_140",
                playback_qualities=[VideoQualityOption(id="ytdlp_1080", label="1080p", ytdl_format="299+140")],
                selected_playback_quality_id="ytdlp_1080",
                ytdl_format="299+140",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    session.playback_loader = lambda item: None

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert [window.audio_combo.itemText(index) for index in range(window.audio_combo.count())] == [
        "音轨",
        "English Original",
        "中文配音",
    ]
    assert window.audio_combo.currentText() == "English Original"
    assert window.audio_combo.isEnabled() is True
    assert window.video.audio_apply_calls == []


def test_player_window_switches_ytdlp_audio_via_reload_preserving_quality_position_and_pause(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int, str]] = []
            self.audio_apply_calls: list[tuple[str, int | None]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            poster_image_path: str | None = None,
            ytdl_format: str = "",
        ) -> None:
            del headers, poster_image_path
            self.load_calls.append((url, pause, start_seconds, ytdl_format))

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
            self.audio_apply_calls.append((mode, track_id))
            return None

        def position_seconds(self) -> int:
            return 93

    loader_calls: list[tuple[str, str]] = []

    def playback_loader(item: PlayItem) -> None:
        loader_calls.append((item.selected_playback_quality_id, item.selected_audio_track_id))
        item.url = "https://www.youtube.com/watch?v=test123"
        item.audio_url = ""
        item.ytdl_format = "299+141" if item.selected_audio_track_id == "ytdlp_audio_zh_141" else "299+140"

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://www.youtube.com/watch?v=test123",
                original_url="https://www.youtube.com/watch?v=test123",
                audio_tracks=[
                    YtdlpAudioTrackOption(id="ytdlp_audio_en_140", label="English Original", lang="en", is_original=True),
                    YtdlpAudioTrackOption(id="ytdlp_audio_zh_141", label="中文配音", lang="zh"),
                ],
                selected_audio_track_id="ytdlp_audio_en_140",
                playback_qualities=[VideoQualityOption(id="ytdlp_1080", label="1080p", ytdl_format="299+140")],
                selected_playback_quality_id="ytdlp_1080",
                ytdl_format="299+140",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    session.playback_loader = playback_loader

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video

    window.open_session(session)

    assert loader_calls == [("ytdlp_1080", "ytdlp_audio_en_140")]
    assert video.load_calls == [("https://www.youtube.com/watch?v=test123", False, 0, "299+140")]

    window.is_playing = False
    window.audio_combo.setCurrentIndex(2)

    assert loader_calls == [
        ("ytdlp_1080", "ytdlp_audio_en_140"),
        ("ytdlp_1080", "ytdlp_audio_zh_141"),
    ]
    assert video.load_calls[-1] == ("https://www.youtube.com/watch?v=test123", True, 93, "299+141")
    assert video.audio_apply_calls == []
    assert session.playlist[0].selected_audio_track_id == "ytdlp_audio_zh_141"


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
    window._refresh_audio_state()

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
        "字幕延迟",
        "音频延迟",
        "画面调节",
        "弹幕配置",
        "刮削",
        "重写剧集标题",
        "搜索字幕",
        "弹幕源",
        "弹幕设置",
        "视频信息",
        "播放时置顶",
        "退出播放",
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
    assert [action.text() for action in _submenu_actions(menu, "字幕延迟")] == [
        "提前 0.5秒",
        "默认",
        "延后 0.5秒",
        "",
        "提前 0.1秒",
        "延后 0.1秒",
        "重置",
    ]
    assert [action.text() for action in _submenu_actions(menu, "画面调节")] == [
        "需启用 hwdec=auto-copy",
    ]


def test_player_window_picture_adjustment_gated_by_effective_hwdec(qtbot) -> None:
    class FakeVideo:
        def __init__(self, supports_picture_adjustments: bool) -> None:
            self._supports_picture_adjustments = supports_picture_adjustments

        def supports_picture_adjustments(self) -> bool:
            return self._supports_picture_adjustments

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.video = FakeVideo(False)
    assert window._picture_adjustment_supported() is False

    window.video = FakeVideo(True)
    assert window._picture_adjustment_supported() is True


def test_player_window_delay_and_picture_menu_handlers_drive_video(qtbot) -> None:
    class P0Video:
        def __init__(self) -> None:
            self.subtitle_delay_calls: list[float] = []
            self.audio_delay_calls: list[float] = []
            self.picture_calls: list[tuple[str, int]] = []

        def set_subtitle_delay(self, seconds: float) -> None:
            self.subtitle_delay_calls.append(seconds)

        def set_audio_delay(self, seconds: float) -> None:
            self.audio_delay_calls.append(seconds)

        def supports_picture_adjustments(self) -> bool:
            return True

        def set_brightness(self, value: int) -> None:
            self.picture_calls.append(("brightness", value))

        def set_contrast(self, value: int) -> None:
            self.picture_calls.append(("contrast", value))

        def set_saturation(self, value: int) -> None:
            self.picture_calls.append(("saturation", value))

        def set_hue(self, value: int) -> None:
            self.picture_calls.append(("hue", value))

        def set_gamma(self, value: int) -> None:
            self.picture_calls.append(("gamma", value))

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = P0Video()

    window._set_subtitle_delay_from_menu(1.5)
    assert window.video.subtitle_delay_calls == [1.5]
    assert window._subtitle_delay == 1.5

    window._step_subtitle_delay(0.1)
    assert window.video.subtitle_delay_calls == [1.5, 1.6]
    assert window._subtitle_delay == 1.6

    window._set_picture_from_menu("brightness", 30)
    assert window.video.picture_calls == [("brightness", 30)]
    assert window._picture_adjustments["brightness"] == 30

    window._step_picture("brightness", -5)
    assert window.video.picture_calls == [("brightness", 30), ("brightness", 25)]
    assert window._picture_adjustments["brightness"] == 25


def test_player_window_delay_shortcuts_drive_video(qtbot) -> None:
    class DelayVideo:
        def __init__(self) -> None:
            self.subtitle_delay_calls: list[float] = []
            self.audio_delay_calls: list[float] = []

        def set_subtitle_delay(self, seconds: float) -> None:
            self.subtitle_delay_calls.append(seconds)

        def set_audio_delay(self, seconds: float) -> None:
            self.audio_delay_calls.append(seconds)

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = DelayVideo()

    send_key(window, Qt.Key.Key_Z, text="z")
    assert window.video.subtitle_delay_calls == [-0.1]

    send_key(window, Qt.Key.Key_X, text="x")
    assert window.video.subtitle_delay_calls == [-0.1, 0.0]

    send_key(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ShiftModifier, text="Z")
    assert window.video.audio_delay_calls == [-0.1]


def test_player_window_builds_video_context_menu_with_dash_quality_submenu(qtbot) -> None:
    class FakeM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return url.startswith("data:application/dash+xml;base64,")

        def prepare(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            dash_video_id: str | None = None,
        ) -> str:
            selected = dash_video_id or "v1080"
            return f"http://127.0.0.1:2323/dash/{selected}.mpd"

        def dash_video_qualities(self, prepared_url: str) -> list[VideoQualityOption]:
            return [
                VideoQualityOption(id="v1080", label="1080P AVC 2.8 Mbps", width=1920, height=1080, bandwidth=2800000),
                VideoQualityOption(id="v720", label="720P AVC 1.2 Mbps", width=1280, height=720, bandwidth=1200000),
            ]

        def selected_dash_video_quality(self, prepared_url: str) -> str | None:
            return "v1080" if prepared_url.endswith("v1080.mpd") else "v720"

    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=FakeM3U8AdFilter())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="data:application/dash+xml;base64,PE1QRD48L01QRD4=",
                headers={"Referer": "https://www.bilibili.com/"},
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window.open_session(session)
    qtbot.waitUntil(lambda: window.video.load_calls == [("http://127.0.0.1:2323/dash/v1080.mpd", 0)])

    menu = window._build_video_context_menu()

    assert "清晰度" in [action.text() for action in menu.actions()]
    assert [action.text() for action in _submenu_actions(menu, "清晰度")] == [
        "1080P AVC 2.8 Mbps",
        "720P AVC 1.2 Mbps",
    ]


def test_player_window_builds_video_context_menu_with_spider_quality_submenu(qtbot) -> None:
    class PassThroughM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return False

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://media.example/video-1080.m3u8",
                playback_qualities=[
                    VideoQualityOption(id="1080p", label="1080P", url="https://media.example/video-1080.m3u8"),
                    VideoQualityOption(id="720p", label="720P", url="https://media.example/video-720.m3u8"),
                ],
                selected_playback_quality_id="1080p",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=PassThroughM3U8AdFilter())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    menu = window._build_video_context_menu()

    assert "清晰度" in [action.text() for action in menu.actions()]
    assert [action.text() for action in _submenu_actions(menu, "清晰度")] == ["1080P", "720P"]


def test_player_window_restores_previous_spider_quality_after_prepare_failure(qtbot) -> None:
    class FlakyM3U8AdFilter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def should_prepare(self, url: str) -> bool:
            return url.endswith(".m3u8")

        def prepare(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            dash_video_id: str | None = None,
        ) -> str:
            self.calls.append((url, dash_video_id))
            if len(self.calls) == 1:
                return "http://127.0.0.1:2323/m3u?v=spider-1080"
            raise RuntimeError("proxy busy")

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://media.example/video-1080.m3u8",
                playback_qualities=[
                    VideoQualityOption(id="1080p", label="1080P", url="https://media.example/video-1080.m3u8"),
                    VideoQualityOption(id="720p", label="720P", url="https://media.example/video-720.m3u8"),
                ],
                selected_playback_quality_id="1080p",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=FlakyM3U8AdFilter())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: window.video.load_calls == [("http://127.0.0.1:2323/m3u?v=spider-1080", 0)])

    item = session.playlist[0]
    assert item.original_url == "https://media.example/video-1080.m3u8"

    window.video_quality_combo.setCurrentIndex(1)

    qtbot.waitUntil(lambda: "清晰度切换失败: proxy busy" in window.log_view.toPlainText())
    assert item.url == "http://127.0.0.1:2323/m3u?v=spider-1080"
    assert item.original_url == "https://media.example/video-1080.m3u8"
    assert item.selected_playback_quality_id == "1080p"
    assert window.video.load_calls == [("http://127.0.0.1:2323/m3u?v=spider-1080", 0)]


def test_player_window_restores_previous_spider_quality_after_direct_load_failure(qtbot) -> None:
    class PassThroughM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return False

    class FlakyVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, pause, start_seconds))
            if len(self.load_calls) == 2:
                raise RuntimeError("device busy")

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 41

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="https://media.example/video-1080.mp4",
                playback_qualities=[
                    VideoQualityOption(id="1080p", label="1080P", url="https://media.example/video-1080.mp4"),
                    VideoQualityOption(id="720p", label="720P", url="https://media.example/video-720.mp4"),
                ],
                selected_playback_quality_id="1080p",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=PassThroughM3U8AdFilter())
    qtbot.addWidget(window)
    video = FlakyVideo()
    window.video = video

    window.open_session(session)
    assert video.load_calls == [("https://media.example/video-1080.mp4", False, 0)]

    window.video_quality_combo.setCurrentIndex(1)

    qtbot.waitUntil(lambda: "清晰度切换失败: device busy" in window.log_view.toPlainText())
    assert session.playlist[0].url == "https://media.example/video-1080.mp4"
    assert session.playlist[0].selected_playback_quality_id == "1080p"


def test_player_window_prefers_spider_quality_options_over_dash_quality_options(qtbot) -> None:
    class FakeM3U8AdFilter:
        def dash_video_qualities(self, prepared_url: str) -> list[VideoQualityOption]:
            return [
                VideoQualityOption(id="v1080", label="1080P AVC 2.8 Mbps"),
                VideoQualityOption(id="v720", label="720P AVC 1.2 Mbps"),
            ]

        def selected_dash_video_quality(self, prepared_url: str) -> str | None:
            return "v1080"

    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=FakeM3U8AdFilter())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="正片",
                url="http://127.0.0.1:2323/dash/v1080.mpd",
                original_url="data:application/dash+xml;base64,PE1QRD48L01QRD4=",
                playback_qualities=[
                    VideoQualityOption(id="1080p", label="1080P", url="https://media.example/video-1080.m3u8"),
                    VideoQualityOption(id="720p", label="720P", url="https://media.example/video-720.m3u8"),
                ],
                selected_playback_quality_id="720p",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window.current_index = 0

    window._refresh_video_quality_state("http://127.0.0.1:2323/dash/v1080.mpd")

    assert [window.video_quality_combo.itemData(index) for index in range(window.video_quality_combo.count())] == [
        "1080p",
        "720p",
    ]
    assert window.video_quality_combo.currentData() == "720p"


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


def test_player_window_context_menu_danmaku_actions_sync_bottom_combo(qtbot) -> None:
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
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(session)

    initial_loaded_count = len(window.video.loaded_danmaku_paths)
    menu = window._build_video_context_menu()
    next(action for action in _submenu_actions(menu, "弹幕配置") if action.text() == "3行").trigger()

    assert window.danmaku_combo.currentText() == "3行"
    assert len(window.video.loaded_danmaku_paths) == initial_loaded_count + 1
    assert window.video.removed_danmaku_track_ids == [70]
    assert [action.text() for action in _submenu_actions(menu, "弹幕配置")] == [
        "默认",
        "关闭",
        "1行",
        "2行",
        "3行",
        "4行",
        "5行",
        "6行",
        "7行",
        "8行",
        "9行",
        "10行",
    ]


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
        "字幕延迟",
        "音频延迟",
        "画面调节",
        "弹幕配置",
        "刮削",
        "重写剧集标题",
        "搜索字幕",
        "弹幕源",
        "弹幕设置",
        "视频信息",
        "播放时置顶",
        "退出播放",
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


def test_player_window_lists_bilibili_external_subtitles_after_embedded_tracks(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)")]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 11 if mode == "auto" else track_id

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="BV1", vod_name="B站视频"),
        playlist=[
            PlayItem(
                title="第1话",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="中文 [B站]",
                        lang="ai-zh",
                        url="http://sub/zh.srt",
                        format="application/x-subrip",
                        source="bilibili",
                    ),
                    ExternalSubtitleOption(
                        name="English [B站]",
                        lang="ai-en",
                        url="http://sub/en.srt",
                        format="application/x-subrip",
                        source="bilibili",
                    ),
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert [window.subtitle_combo.itemText(index) for index in range(window.subtitle_combo.count())] == [
        "字幕",
        "关闭字幕",
        "中文 (默认)",
        "中文 [B站]",
        "English [B站]",
    ]


def test_player_window_lists_spider_external_subtitle_in_primary_combo(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)")]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return 11 if mode == "auto" else track_id

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url="http://127.0.0.1:4567/sub/1.srt",
                        format="application/x-subrip",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert [window.subtitle_combo.itemText(index) for index in range(window.subtitle_combo.count())] == [
        "字幕",
        "关闭字幕",
        "中文 (默认)",
        "外挂字幕 [插件]",
    ]


def test_player_window_auto_loads_spider_subtitle_when_no_embedded_tracks(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                headers={"Referer": "https://site.example"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url="http://127.0.0.1:4567/sub/1.srt",
                        format="application/x-subrip",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == [("track", 91)]
    assert window.subtitle_combo.currentText() == "外挂字幕 [插件]"


def test_player_window_retries_auto_loaded_spider_subtitle_when_mpv_is_not_ready(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._fail_first_apply = True

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            if mode == "track" and self._fail_first_apply:
                self._fail_first_apply = False
                raise RuntimeError(("Error running mpv command", -12, (object(), object(), object())))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                headers={"Referer": "https://site.example"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url="http://127.0.0.1:4567/sub/1.srt",
                        format="application/x-subrip",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.subtitle_apply_calls) == 2)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == [("track", 91), ("track", 91)]
    assert "字幕切换失败" not in window.log_view.toPlainText()
    assert window.subtitle_combo.currentText() == "外挂字幕 [插件]"


def test_player_window_retries_auto_loaded_spider_subtitle_when_sub_add_fails_initially(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._fail_first_load = True

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            if self._fail_first_load:
                self._fail_first_load = False
                raise RuntimeError(("Error running mpv command", -12, (object(), object(), object())))
            return 91

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                headers={"Referer": "https://site.example"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url="http://127.0.0.1:4567/sub/1.srt",
                        format="application/x-subrip",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 2)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False, False]
    assert window.video.subtitle_apply_calls == [("track", 91)]
    assert "字幕切换失败" not in window.log_view.toPlainText()
    assert window.subtitle_combo.currentText() == "外挂字幕 [插件]"


def test_player_window_retries_auto_loaded_spider_subtitle_when_first_song_track_id_is_delayed(
    qtbot, monkeypatch
) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._return_pending_track_once = True

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            if self._return_pending_track_once:
                self._return_pending_track_once = False
                return None
            return 91

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                headers={"Referer": "https://site.example"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url="http://127.0.0.1:4567/sub/1.srt",
                        format="application/x-subrip",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 2)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False, False]
    assert window.video.subtitle_apply_calls == [("track", 91)]
    assert "字幕切换失败" not in window.log_view.toPlainText()
    assert window.subtitle_combo.currentText() == "外挂字幕 [插件]"


def test_player_window_reapplies_auto_spider_subtitle_when_player_sid_drifts_to_auto(
    qtbot, monkeypatch
) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._current_sid: int | str | None = "auto"

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def current_subtitle_track_id(self) -> int | None:
            return self._current_sid if isinstance(self._current_sid, int) else None

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            if mode == "track":
                self._current_sid = track_id
                return track_id
            self._current_sid = "auto"
            return None

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                headers={"Referer": "https://site.example"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url="http://127.0.0.1:4567/sub/1.srt",
                        format="application/x-subrip",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: window.video.subtitle_apply_calls == [("track", 91)])
    window.video.subtitle_apply_calls.clear()

    window.video._current_sid = "auto"
    window._refresh_subtitle_state()

    assert window.video.subtitle_apply_calls == [("track", 91)]
    assert window.subtitle_combo.currentText() == "外挂字幕 [插件]"


def test_player_window_reloads_manual_ytdlp_subtitle_when_saved_external_track_is_stale(
    qtbot, monkeypatch
) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._next_track_id = 91
            self._current_sid: int | None = None
            self._active_track_ids: set[int] = set()

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def current_subtitle_track_id(self) -> int | None:
            return self._current_sid

        def has_subtitle_track(self, track_id: int) -> bool:
            return track_id in self._active_track_ids

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            if mode == "track" and track_id not in self._active_track_ids:
                raise RuntimeError(("Error running mpv command", -12, (object(), object(), object())))
            self._current_sid = track_id
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            track_id = self._next_track_id
            self._next_track_id += 1
            self._active_track_ids = {track_id}
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            if track_id is not None:
                self._active_track_ids.discard(track_id)
                if self._current_sid == track_id:
                    self._current_sid = None

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="yt1", vod_name="YouTube 视频"),
        playlist=[
            PlayItem(
                title="Video 1",
                url="https://media.example/youtube.mp4",
                headers={"Referer": "https://www.youtube.com/"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="English [yt-dlp]",
                        lang="en",
                        url="https://sub.example/en.vtt",
                        format="vtt",
                        source="ytdlp",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.subtitle_combo.setCurrentIndex(2)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert window.video.loaded_external_subtitles == [(window.video.loaded_external_subtitles[0][0], False)]
    assert window.video.subtitle_apply_calls == [("track", 91)]

    window.video.loaded_external_subtitles.clear()
    window.video.subtitle_apply_calls.clear()
    window.video._active_track_ids.clear()
    window.video._current_sid = None

    window._refresh_subtitle_state()

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert window.video.loaded_external_subtitles == [(window.video.loaded_external_subtitles[0][0], False)]
    assert window.video.subtitle_apply_calls == [("track", 92)]
    assert "字幕切换失败" not in window.log_view.toPlainText()
    assert window.subtitle_combo.currentText() == "English [yt-dlp]"


def test_player_window_auto_loads_configured_ytdlp_default_subtitle(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._current_sid: int | None = None

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def current_subtitle_track_id(self) -> int | None:
            return self._current_sid

        def has_subtitle_track(self, track_id: int) -> bool:
            return self._current_sid == track_id

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            self._current_sid = track_id
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="yt1", vod_name="YouTube 视频"),
        playlist=[
            PlayItem(
                title="Video 1",
                url="https://media.example/youtube.mp4",
                headers={"Referer": "https://www.youtube.com/"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="English [yt-dlp]",
                        lang="en",
                        url="https://sub.example/en.vtt",
                        format="vtt",
                        source="ytdlp",
                    ),
                    ExternalSubtitleOption(
                        name="简体中文 [yt-dlp]",
                        lang="zh-CN",
                        url="https://sub.example/zh.vtt",
                        format="vtt",
                        source="ytdlp",
                    ),
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), config=AppConfig(youtube_default_subtitle_lang="zh-CN"))
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert window.video.loaded_external_subtitles == [(window.video.loaded_external_subtitles[0][0], False)]
    assert window.video.subtitle_apply_calls == [("track", 91)]
    assert "字幕切换失败" not in window.log_view.toPlainText()
    assert window.subtitle_combo.currentText() == "简体中文 [yt-dlp]"


def test_player_window_play_next_reloads_auto_ytdlp_default_subtitle_for_channel_item(
    qtbot, tmp_path
) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.removed_subtitle_tracks: list[int | None] = []
            self._next_track_id = 91

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            self.removed_subtitle_tracks.append(track_id)

        def position_seconds(self) -> int:
            return 0

    first_subtitle_path = tmp_path / "video-1.zh.vtt"
    second_subtitle_path = tmp_path / "video-2.zh.vtt"
    first_subtitle_path.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n第一条\n", encoding="utf-8")
    second_subtitle_path.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n第二条\n", encoding="utf-8")
    session = PlayerSession(
        vod=VodItem(vod_id="yt:channel:UCX6OQ3DkcsbYNE6H8uQQuVA", vod_name="YouTube 频道"),
        playlist=[
            PlayItem(
                title="Video 1",
                url="https://media.example/video-1.mp4",
                original_url="https://www.youtube.com/watch?v=video1",
                headers={"Referer": "https://www.youtube.com/"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="简体中文 [yt-dlp]",
                        lang="zh-CN",
                        url=str(first_subtitle_path),
                        format="vtt",
                        source="ytdlp",
                    )
                ],
            ),
            PlayItem(
                title="Video 2",
                url="https://media.example/video-2.mp4",
                original_url="https://www.youtube.com/watch?v=video2",
                headers={"Referer": "https://www.youtube.com/"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="简体中文 [yt-dlp]",
                        lang="zh-CN",
                        url=str(second_subtitle_path),
                        format="vtt",
                        source="ytdlp",
                    )
                ],
            ),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), config=AppConfig(youtube_default_subtitle_lang="zh-CN"))
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert [Path(path).read_text(encoding="utf-8") for path, _ in window.video.loaded_external_subtitles] == [
        first_subtitle_path.read_text(encoding="utf-8")
    ]

    window.video.loaded_external_subtitles.clear()
    window.video.subtitle_apply_calls.clear()

    window.play_next()

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert [Path(path).read_text(encoding="utf-8") for path, _ in window.video.loaded_external_subtitles] == [
        second_subtitle_path.read_text(encoding="utf-8")
    ]
    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == [("track", 92)]
    assert window.subtitle_combo.currentText() == "简体中文 [yt-dlp]"


def test_player_window_auto_loads_configured_zh_cn_when_ytdlp_returns_zh_hans(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="yt1", vod_name="YouTube 视频"),
        playlist=[
            PlayItem(
                title="Video 1",
                url="https://media.example/youtube.mp4",
                headers={"Referer": "https://www.youtube.com/"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="English [yt-dlp]",
                        lang="en",
                        url="https://sub.example/en.vtt",
                        format="vtt",
                        source="ytdlp",
                    ),
                    ExternalSubtitleOption(
                        name="简体中文 [yt-dlp]",
                        lang="zh-Hans",
                        url="https://sub.example/zh.vtt",
                        format="vtt",
                        source="ytdlp",
                    ),
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController(), config=AppConfig(youtube_default_subtitle_lang="zh-CN"))
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.video.loaded_external_subtitles == [(window.video.loaded_external_subtitles[0][0], False)]
    assert window.video.subtitle_apply_calls == [("track", 91)]
    assert window.subtitle_combo.currentText() == "简体中文 [yt-dlp]"


def test_player_window_loads_ytdlp_webvtt_with_vtt_suffix(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n"),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    subtitle = ExternalSubtitleOption(
        name="English [yt-dlp]",
        lang="en",
        url="https://sub.example/en.vtt",
        format="vtt",
        source="ytdlp",
    )
    text = window._fetch_external_subtitle_text(subtitle)

    track_id, subtitle_path = window._load_external_subtitle_from_text(subtitle, text, secondary=False)

    assert track_id == 91
    assert subtitle_path.suffix == ".vtt"
    assert window.video.loaded_external_subtitles == [(str(subtitle_path), False)]


def test_player_window_rejects_blocked_ytdlp_translated_vtt_html_response(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            raise AssertionError("mpv should not receive an HTML error page as a subtitle file")

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("<html><title>Sorry...</title><body>We're sorry...</body></html>"),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    subtitle = ExternalSubtitleOption(
        name="简体中文 [yt-dlp]",
        lang="zh-Hans",
        url="https://www.youtube.com/api/timedtext?lang=en&fmt=vtt&tlang=zh-Hans",
        format="vtt",
        source="ytdlp",
    )
    text = window._fetch_external_subtitle_text(subtitle)

    with pytest.raises(ValueError, match="YouTube 翻译字幕"):
        window._load_external_subtitle_from_text(subtitle, text, secondary=False)


def test_player_window_retries_first_manual_external_subtitle_selection_when_track_id_is_delayed(
    qtbot, monkeypatch
) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._return_pending_track_once = True

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            if self._return_pending_track_once:
                self._return_pending_track_once = False
                return None
            return 91

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                headers={"Referer": "https://site.example"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="QQ歌词",
                        lang="",
                        url="http://127.0.0.1:4567/sub/1.srt",
                        format="application/x-subrip",
                        source="qqmusic",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.video.loaded_external_subtitles.clear()
    window.video.subtitle_apply_calls.clear()

    window.subtitle_combo.setCurrentIndex(2)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 2)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False, False]
    assert window.video.subtitle_apply_calls == [("track", 91)]
    assert "字幕切换失败" not in window.log_view.toPlainText()
    assert window.subtitle_combo.currentText() == "QQ歌词"


def test_player_window_auto_loads_spider_subtitle_from_local_path(qtbot, monkeypatch, tmp_path) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def position_seconds(self) -> int:
            return 0

    subtitle_path = tmp_path / "plugin.srt"
    subtitle_path.write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8")
    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda *args, **kwargs: pytest.fail("should not fetch local spider subtitle via http"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url=str(subtitle_path),
                        format="application/x-subrip",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert [Path(path).read_text(encoding="utf-8") for path, _ in window.video.loaded_external_subtitles] == [
        "1\n00:00:00,000 --> 00:00:01,000\n你好\n"
    ]
    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == [("track", 91)]
    assert window.subtitle_combo.currentText() == "外挂字幕 [插件]"


def test_player_window_auto_loads_generated_spider_karaoke_ass_from_local_path(qtbot, tmp_path) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def position_seconds(self) -> int:
            return 0

    subtitle_path = tmp_path / "plugin-karaoke.ass"
    subtitle_path.write_text(
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: KaraokeMain,Arial,46,&H00FFFFFF,&H0000D7FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,0,2,60,60,120,1\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        r"Dialogue: 0,0:00:00.00,0:00:01.80,KaraokeMain,,0,0,0,,{\kf45}轻{\kf45}舟{\kf45}已{\kf45}过\n",
        encoding="utf-8",
    )

    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="逐字歌词 [插件]",
                        lang="",
                        url=str(subtitle_path),
                        format="text/x-ass",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == [("track", 91)]
    assert window.subtitle_combo.currentText() == "逐字歌词 [插件]"


def test_player_window_does_not_auto_load_spider_subtitle_when_embedded_tracks_exist(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)")]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return 11 if mode == "auto" else track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def position_seconds(self) -> int:
            return 0

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda *args, **kwargs: pytest.fail("should not fetch spider subtitle when embedded tracks exist"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url="http://127.0.0.1:4567/sub/1.srt",
                        format="application/x-subrip",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.video.loaded_external_subtitles == []
    assert window.video.subtitle_apply_calls == [("auto", None)]
    assert window.subtitle_combo.currentText() == "字幕"


def test_player_window_does_not_auto_load_non_spider_external_subtitle_when_no_embedded_tracks(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def position_seconds(self) -> int:
            return 0

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda *args, **kwargs: pytest.fail("should not auto-fetch non-spider subtitle"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="bv1", vod_name="B站视频"),
        playlist=[
            PlayItem(
                title="第1话",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="中文 [B站]",
                        lang="ai-zh",
                        url="http://sub/zh.srt",
                        format="application/x-subrip",
                        source="bilibili",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.video.loaded_external_subtitles == []
    assert window.video.subtitle_apply_calls == []
    assert window.subtitle_combo.currentText() == "字幕"


def test_player_window_secondary_menu_excludes_spider_external_subtitles(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)")]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url="http://127.0.0.1:4567/sub/1.srt",
                        format="application/x-subrip",
                        source="spider",
                    ),
                    ExternalSubtitleOption(
                        name="English [B站]",
                        lang="ai-en",
                        url="http://sub/en.srt",
                        format="application/x-subrip",
                        source="bilibili",
                    ),
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    menu = window._build_secondary_subtitle_menu(window)

    assert [action.text() for action in menu.actions()] == [
        "关闭次字幕",
        "中文 (默认)",
        "English [B站]",
    ]


def test_player_window_does_not_auto_load_bilibili_external_subtitles_on_open(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[str] = []

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

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append(path)
            return 51

        def position_seconds(self) -> int:
            return 0

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda *args, **kwargs: pytest.fail("should not fetch subtitle"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="BV1", vod_name="B站视频"),
        playlist=[
            PlayItem(
                title="第1话",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="中文 [B站]",
                        lang="ai-zh",
                        url="http://sub/zh.srt",
                        format="application/x-subrip",
                        source="bilibili",
                    ),
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.video.loaded_external_subtitles == []
    assert window.subtitle_combo.currentText() == "字幕"


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


def test_player_window_user_selection_loads_bilibili_subtitle_as_primary(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="BV1", vod_name="B站视频"),
        playlist=[
            PlayItem(
                title="第1话",
                url="http://m/1.m3u8",
                headers={"Referer": "https://www.bilibili.com/"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="中文 [B站]",
                        lang="ai-zh",
                        url="http://sub/zh.srt",
                        format="application/x-subrip",
                        source="bilibili",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.subtitle_combo.setCurrentIndex(2)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == [("track", 91)]


def test_player_window_user_selection_loads_spider_subtitle_as_primary(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                headers={"Referer": "https://site.example"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url="http://127.0.0.1:4567/sub/1.srt",
                        format="application/x-subrip",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.subtitle_combo.setCurrentIndex(2)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == [("track", 91)]


def test_player_window_retries_manual_external_subtitle_when_mpv_is_not_ready(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._fail_first_apply = True

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            if mode == "track" and self._fail_first_apply:
                self._fail_first_apply = False
                raise RuntimeError(("Error running mpv command", -12, (object(), object(), object())))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="BV1", vod_name="B站视频"),
        playlist=[
            PlayItem(
                title="第1话",
                url="http://m/1.m3u8",
                headers={"Referer": "https://www.bilibili.com/"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="中文 [B站]",
                        lang="ai-zh",
                        url="http://sub/zh.srt",
                        format="application/x-subrip",
                        source="bilibili",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.video.subtitle_apply_calls.clear()
    window.video.loaded_external_subtitles.clear()

    window.subtitle_combo.setCurrentIndex(2)

    qtbot.waitUntil(lambda: len(window.video.subtitle_apply_calls) == 2)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == [("track", 91), ("track", 91)]
    assert "字幕切换失败" not in window.log_view.toPlainText()
    assert window.subtitle_combo.currentText() == "中文 [B站]"


def test_player_window_keeps_manual_external_subtitle_when_mpv_already_selected_loaded_track(
    qtbot, monkeypatch
) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._current_sid: int | None = None

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def current_subtitle_track_id(self) -> int | None:
            return self._current_sid

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            self._current_sid = 91
            return 91

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            raise RuntimeError(("Error running mpv command", -12, (object(), object(), object())))

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="BV1", vod_name="B站视频"),
        playlist=[
            PlayItem(
                title="第1话",
                url="http://m/1.m3u8",
                headers={"Referer": "https://www.bilibili.com/"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="English [yt-dlp]",
                        lang="en",
                        url="https://sub.example/en.vtt",
                        format="vtt",
                        source="ytdlp",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.video.loaded_external_subtitles.clear()

    window.subtitle_combo.setCurrentIndex(2)
    qtbot.wait(50)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == []
    assert "字幕切换失败" not in window.log_view.toPlainText()
    assert window.subtitle_combo.currentText() == "English [yt-dlp]"


def test_player_window_does_not_reapply_auto_spider_subtitle_after_user_turns_subtitles_off(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                headers={"Referer": "https://site.example"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url="http://127.0.0.1:4567/sub/1.srt",
                        format="application/x-subrip",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.video.subtitle_apply_calls.clear()
    window.video.loaded_external_subtitles.clear()

    window.subtitle_combo.setCurrentIndex(1)
    window.video_widget.subtitle_tracks_changed.emit()

    assert window.video.loaded_external_subtitles == []
    assert window.video.subtitle_apply_calls == [("off", None)]
    assert window.subtitle_combo.currentText() == "关闭字幕"


def test_player_window_context_menu_loads_bilibili_subtitle_as_secondary(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.secondary_subtitle_apply_calls: list[tuple[str, int | None]] = []

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

        def apply_secondary_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.secondary_subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 101

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\nhello\n"),
    )
    item = PlayItem(
        title="第1话",
        url="http://m/1.m3u8",
        headers={"Referer": "https://www.bilibili.com/"},
        external_subtitles=[
            ExternalSubtitleOption(
                name="English [B站]",
                lang="ai-en",
                url="http://sub/en.srt",
                format="application/x-subrip",
                source="bilibili",
            )
        ],
    )
    session = PlayerSession(
        vod=VodItem(vod_id="BV1", vod_name="B站视频"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window._set_secondary_subtitle_from_menu("external", "http://sub/en.srt")

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [True]
    assert window.video.secondary_subtitle_apply_calls == [("track", 101)]


def test_player_window_context_menu_lists_and_loads_bilibili_subtitle_as_primary(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return [SubtitleTrack(id=11, title="", lang="zh", is_default=True, is_forced=False, label="中文 (默认)")]

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="BV1", vod_name="B站视频"),
        playlist=[
            PlayItem(
                title="第1话",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="中文 [B站]",
                        lang="ai-zh",
                        url="http://sub/zh.srt",
                        format="application/x-subrip",
                        source="bilibili",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.video.subtitle_apply_calls.clear()
    menu = window._build_video_context_menu()

    assert [action.text() for action in _submenu_actions(menu, "主字幕")] == [
        "自动选择",
        "关闭字幕",
        "中文 (默认)",
        "中文 [B站]",
    ]

    next(action for action in _submenu_actions(menu, "主字幕") if action.text() == "中文 [B站]").trigger()

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == [("track", 91)]


def test_player_window_unloads_primary_bilibili_subtitle_when_switching_to_off(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.removed_track_ids: list[int] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def remove_subtitle_track(self, track_id: int | None) -> None:
            if track_id is not None:
                self.removed_track_ids.append(track_id)

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="BV1", vod_name="B站视频"),
        playlist=[
            PlayItem(
                title="第1话",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="中文 [B站]",
                        lang="ai-zh",
                        url="http://sub/zh.srt",
                        format="application/x-subrip",
                        source="bilibili",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.subtitle_combo.setCurrentIndex(2)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    window.video.removed_track_ids.clear()
    window.video.subtitle_apply_calls.clear()

    window.subtitle_combo.setCurrentIndex(1)

    assert window.video.removed_track_ids == [91]
    assert window.video.subtitle_apply_calls == [("off", None)]


def test_player_window_clears_bilibili_subtitle_tracks_without_removing_danmaku(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.removed_track_ids: list[int] = []

        def remove_subtitle_track(self, track_id: int | None) -> None:
            if track_id is not None:
                self.removed_track_ids.append(track_id)

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window._primary_external_subtitle_track_id = 91
    window._secondary_external_subtitle_track_id = 101
    window._danmaku_track_id = 77

    window._clear_external_subtitle_tracks()

    assert window.video.removed_track_ids == [91, 101]
    assert window._danmaku_track_id == 77


def test_player_window_logs_bilibili_subtitle_failure_without_interrupting_playback(qtbot, monkeypatch) -> None:
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

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="BV1", vod_name="B站视频"),
        playlist=[
            PlayItem(
                title="第1话",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="中文 [B站]",
                        lang="ai-zh",
                        url="http://sub/zh.srt",
                        format="application/x-subrip",
                        source="bilibili",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    window.open_session(session)

    window.subtitle_combo.setCurrentIndex(2)

    qtbot.waitUntil(lambda: "字幕切换失败: boom" in window.log_view.toPlainText(), timeout=2000)

    assert "字幕切换失败: boom" in window.log_view.toPlainText()


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
    window._refresh_subtitle_state()

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
            self.set_subtitle_ass_override_calls: list[str] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
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
            assert select_for_secondary is False
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

        def supports_subtitle_ass_override(self) -> bool:
            return True

        def subtitle_ass_override(self) -> str:
            return "scale"

        def set_subtitle_ass_override(self, value: str) -> None:
            self.set_subtitle_ass_override_calls.append(value)

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

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
    assert window.video.set_subtitle_ass_override_calls == ["no"]
    assert window.video.subtitle_apply_calls[-1] == ("track", 40)
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
        config=AppConfig(preferred_danmaku_enabled=True, preferred_danmaku_line_count=8),
    )
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert len(window.video.loaded_danmaku_paths) == 1
    assert window.danmaku_combo.currentText() == "8行"


def test_player_window_defaults_invalid_saved_danmaku_line_count_on_open_session(qtbot) -> None:
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
    config = AppConfig(preferred_danmaku_enabled=True)
    config.preferred_danmaku_line_count = "static"  # type: ignore[assignment]
    window = PlayerWindow(FakePlayerController(), config=config)
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert len(window.video.loaded_danmaku_paths) == 1
    assert window._danmaku_line_count == 1
    assert window.danmaku_combo.currentText() == "弹幕"


def test_player_window_changes_danmaku_mode_without_affecting_playback(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self.removed_danmaku_track_ids: list[int] = []
            self.set_secondary_subtitle_position_calls: list[int] = []
            self.set_subtitle_ass_override_calls: list[str] = []
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

        def supports_subtitle_ass_override(self) -> bool:
            return True

        def subtitle_ass_override(self) -> str:
            return "scale"

        def set_subtitle_ass_override(self, value: str) -> None:
            self.set_subtitle_ass_override_calls.append(value)

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
    assert window.video.set_subtitle_ass_override_calls == ["no", "scale", "no"]
    assert window.video.secondary_subtitle_apply_calls == []
    assert window.danmaku_combo.currentText() == "3行"


def test_player_window_reloads_active_danmaku_after_uniform_color_change(qtbot) -> None:
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
    window.config.preferred_danmaku_color_mode = "uniform"
    window.open_session(session)
    saved["called"] = 0
    initial_count = len(window.video.loaded_danmaku_paths)
    initial_path = window.video.loaded_danmaku_paths[-1]

    window._save_danmaku_uniform_color("#00FF00")

    assert saved["called"] == 1
    assert len(window.video.loaded_danmaku_paths) == initial_count + 1
    assert window.video.removed_danmaku_track_ids == [70]
    assert window.video.loaded_danmaku_paths[-1] != initial_path


def test_player_window_reloads_active_danmaku_after_color_mode_change(qtbot) -> None:
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
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16711680">第一条</d></i>',
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
    window.config.preferred_danmaku_color_mode = "uniform"
    window.open_session(session)
    saved["called"] = 0
    initial_count = len(window.video.loaded_danmaku_paths)
    initial_path = window.video.loaded_danmaku_paths[-1]

    window._save_danmaku_color_mode("source")

    assert saved["called"] == 1
    assert len(window.video.loaded_danmaku_paths) == initial_count + 1
    assert window.video.removed_danmaku_track_ids == [70]
    assert window.video.loaded_danmaku_paths[-1] != initial_path


def test_player_window_saves_settings_without_enabling_danmaku_when_currently_off(qtbot) -> None:
    saved = {"called": 0}
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(preferred_danmaku_enabled=False),
        save_config=lambda: saved.__setitem__("called", saved["called"] + 1),
    )
    qtbot.addWidget(window)

    window._save_danmaku_render_mode("mixed")

    assert window.config.preferred_danmaku_enabled is False
    assert window.config.preferred_danmaku_render_mode == "mixed"
    assert saved["called"] == 1


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


def test_player_window_loads_danmaku_with_primary_slot_even_when_secondary_ass_override_is_supported(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[tuple[str, bool]] = []
            self.set_subtitle_ass_override_calls: list[str] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
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

    assert window.danmaku_combo.isEnabled() is True
    assert window.danmaku_combo.currentText() == "弹幕"
    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_danmaku_paths] == [False]
    assert window.video.set_subtitle_ass_override_calls == ["no"]
    assert window.video.subtitle_apply_calls[-1] == ("track", 70)
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
    assert window.video.set_subtitle_ass_override_calls == ["no"]
    assert window.video.subtitle_apply_calls[-1] == ("track", 90)


def test_player_window_does_not_use_secondary_slot_when_secondary_ass_override_is_supported(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[tuple[str, bool]] = []
            self.set_secondary_subtitle_ass_override_calls: list[str] = []
            self.set_subtitle_ass_override_calls: list[str] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
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
    assert window.video.set_secondary_subtitle_ass_override_calls == []
    assert window.video.set_subtitle_ass_override_calls == ["no"]
    assert window.video.subtitle_apply_calls[-1] == ("track", 70)


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

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_danmaku_paths] == [False, False]
    assert "弹幕加载失败" not in window.log_view.toPlainText()


def test_player_window_keeps_retrying_danmaku_load_until_player_is_ready(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[tuple[str, bool]] = []
            self.set_secondary_subtitle_ass_override_calls: list[str] = []
            self._next_track_id = 110
            self._remaining_failures = 4

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
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
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

    qtbot.waitUntil(lambda: len(window.video.loaded_danmaku_paths) >= 5, timeout=3000)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_danmaku_paths] == [
        False,
        False,
        False,
        False,
        False,
    ]
    assert "弹幕加载失败" not in window.log_view.toPlainText()


def test_player_window_does_not_disable_danmaku_when_track_list_refresh_arrives_during_load(qtbot) -> None:
    class FakeVideo:
        def __init__(self, window: PlayerWindow) -> None:
            self.window = window
            self.loaded_danmaku_paths: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.set_secondary_subtitle_ass_override_calls: list[str] = []
            self.set_subtitle_ass_override_calls: list[str] = []
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

    assert window.video.subtitle_apply_calls[-1] == ("track", 120)
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

    _spin_until(lambda: len(window.video.loaded_danmaku_paths) == 1)
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

    _spin_until(lambda: len(window.video.loaded_danmaku_paths) == 1)
    assert window.danmaku_combo.currentText() == "3行"


def test_player_window_auto_loads_cached_danmaku_after_restart_with_manual_override(qtbot, monkeypatch, tmp_path) -> None:
    class FakeSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "玄界之门3D版",
                        "vod_pic": "poster-detail",
                        "vod_play_from": "默认线",
                        "vod_play_url": "第1集$/play/1",
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):
            return {"parse": 0, "url": f"https://stream.example{id}.m3u8", "header": {"Referer": "https://site.example"}}

        def danmaku(self):
            return True

    class FakeVideo:
        def __init__(self) -> None:
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers=None) -> None:
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

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(spider_controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(spider_controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        spider_controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        spider_controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'

    class FirstDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            return xml_text

    class SecondDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

        def search_danmu(self, name: str, reg_src: str = ""):
            return []

        def resolve_danmu(self, page_url: str) -> str:
            raise AssertionError("restart should use cached danmaku xml")

    first_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=FirstDanmakuService(),
    )
    first_request = first_controller.build_request("/detail/1")
    assert first_request.playback_loader is not None
    seeded_item = first_request.playlist[0]
    first_request.playback_loader(seeded_item)
    seeded_item.danmaku_search_query = "玄界之门 1集"
    seeded_item.danmaku_search_query_overridden = True
    first_controller.refresh_danmaku_sources(seeded_item, query_override="玄界之门 1集", force_refresh=True)
    first_controller.switch_danmaku_source(seeded_item, "https://v.qq.com/demo")

    second_controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=SecondDanmakuService(),
    )
    second_request = second_controller.build_request("/detail/1")
    session = PlayerSession(
        vod=second_request.vod,
        playlist=second_request.playlist,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        playlists=second_request.playlists,
        playlist_index=second_request.playlist_index,
        playback_loader=second_request.playback_loader,
        async_playback_loader=second_request.async_playback_loader,
        danmaku_controller=second_request.danmaku_controller,
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_danmaku_paths) == 1)
    assert window.danmaku_combo.currentText() == "弹幕"


def test_player_window_open_session_keeps_playback_loader_episode_danmaku_when_url_already_exists(qtbot) -> None:
    episode_one_xml = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一集弹幕</d></i>'
    episode_ten_xml = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第十集弹幕</d></i>'

    class FakeDanmakuController:
        def __init__(self) -> None:
            self.switch_calls: list[str] = []
            self.restore_attempted = threading.Event()

        def load_cached_danmaku_sources(
            self,
            item: PlayItem,
            playlist: list[PlayItem] | None = None,
            media_duration_seconds: int = 0,
        ) -> bool:
            del playlist, media_duration_seconds
            item.danmaku_candidates = [
                DanmakuSourceGroup(
                    provider="tencent",
                    provider_label="腾讯",
                    options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/ep1")],
                )
            ]
            item.selected_danmaku_provider = "tencent"
            item.selected_danmaku_url = "https://v.qq.com/ep1"
            item.selected_danmaku_title = "红果短剧 第1集"
            return True

        def switch_danmaku_source(self, item: PlayItem, page_url: str) -> str:
            self.switch_calls.append(page_url)
            item.danmaku_xml = episode_one_xml
            self.restore_attempted.set()
            return item.danmaku_xml

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

    controller = FakeDanmakuController()

    def playback_loader(item: PlayItem) -> None:
        controller.restore_attempted.wait(0.1)
        if not item.danmaku_xml:
            item.danmaku_xml = episode_ten_xml
        return None

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="红果短剧"),
        playlist=[
            PlayItem(
                title="第10集",
                url="https://stream.example/10.m3u8",
                media_title="红果短剧",
                danmaku_search_title="红果短剧",
                danmaku_search_episode="10集",
                danmaku_search_query="红果短剧 10集",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        playback_loader=playback_loader,
        async_playback_loader=True,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_danmaku_paths) == 1)
    assert controller.switch_calls == []
    assert "第十集弹幕" in session.playlist[0].danmaku_xml
    assert "第十集弹幕" in Path(window.video.loaded_danmaku_paths[0]).read_text(encoding="utf-8")


def test_player_window_open_session_restores_cached_danmaku_via_controller_when_item_starts_empty(qtbot) -> None:
    class FakeDanmakuController:
        def load_cached_danmaku_sources(
            self,
            item: PlayItem,
            playlist: list[PlayItem] | None = None,
            media_duration_seconds: int = 0,
        ) -> bool:
            item.danmaku_search_title = "红果短剧"
            item.danmaku_search_episode = "1集"
            item.danmaku_search_query = "红果短剧 1集"
            item.danmaku_candidates = [
                DanmakuSourceGroup(
                    provider="tencent",
                    provider_label="腾讯",
                    options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
                )
            ]
            item.selected_danmaku_provider = "tencent"
            item.selected_danmaku_url = "https://v.qq.com/demo"
            item.selected_danmaku_title = "红果短剧 第1集"
            return True

        def switch_danmaku_source(self, item: PlayItem, page_url: str) -> str:
            item.danmaku_xml = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>'
            return item.danmaku_xml

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
        playlist=[PlayItem(title="第1集", url="http://m/1.m3u8", media_title="红果短剧")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=FakeDanmakuController(),
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_danmaku_paths) == 1)
    assert window.danmaku_combo.currentText() == "弹幕"


def test_player_window_build_danmaku_subtitle_file_passes_current_episode_label(qtbot, monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_load_or_create_danmaku_ass_cache(xml_text: str, line_count: int, **kwargs) -> Path | None:
        captured["xml_text"] = xml_text
        captured["line_count"] = line_count
        captured.update(kwargs)
        return tmp_path / "demo.ass"

    monkeypatch.setattr(player_window_module, "load_or_create_danmaku_ass_cache", fake_load_or_create_danmaku_ass_cache)

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="红果短剧"),
        playlist=[
            PlayItem(
                title="随便起名",
                original_title="第1集",
                url="https://media.example/1.mp4",
                danmaku_offset_seconds=-3.0,
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window.current_index = 0

    path = window._build_danmaku_subtitle_file(
        '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
        2,
        render_mode="static",
        color_mode="uniform",
        uniform_color="#FFFFFF",
        position_preset="top",
        scroll_speed=1.0,
        font_size=32,
    )

    assert path == tmp_path / "demo.ass"
    assert captured["intro_episode_label"] == "第1集"
    assert captured["time_offset_seconds"] == -3.0


def test_player_window_build_danmaku_subtitle_file_passes_readability_settings(qtbot, monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_load_or_create_danmaku_ass_cache(xml_text: str, line_count: int, **kwargs) -> Path | None:
        captured.update(kwargs)
        return tmp_path / "demo.ass"

    monkeypatch.setattr(player_window_module, "load_or_create_danmaku_ass_cache", fake_load_or_create_danmaku_ass_cache)

    config = AppConfig(
        preferred_danmaku_opacity=60,
        preferred_danmaku_outline_strength="soft",
    )
    window = PlayerWindow(FakePlayerController(), config=config)
    qtbot.addWidget(window)

    path = window._build_danmaku_subtitle_file(
        '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
        1,
        render_mode="static",
        color_mode="uniform",
        uniform_color="#FFFFFF",
        position_preset="top",
        scroll_speed=1.0,
        font_size=32,
        opacity=60,
        outline_strength="soft",
    )

    assert path == tmp_path / "demo.ass"
    assert captured["opacity"] == 60
    assert captured["outline_strength"] == "soft"


def test_player_window_playback_loader_replacement_restores_cached_danmaku_for_current_item(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.load_calls: list[str] = []
            self.switch_calls: list[str] = []

        def load_cached_danmaku_sources(
            self,
            item: PlayItem,
            playlist: list[PlayItem] | None = None,
            media_duration_seconds: int = 0,
        ) -> bool:
            del playlist, media_duration_seconds
            self.load_calls.append(item.title)
            item.danmaku_search_title = "低智商犯罪"
            item.danmaku_search_episode = "11集"
            item.danmaku_search_query = "低智商犯罪 11集"
            item.danmaku_candidates = [
                DanmakuSourceGroup(
                    provider="tencent",
                    provider_label="腾讯",
                    options=[DanmakuSourceOption(provider="tencent", name="低智商犯罪 第11集", url="https://v.qq.com/ep11")],
                )
            ]
            item.selected_danmaku_provider = "tencent"
            item.selected_danmaku_url = "https://v.qq.com/ep11"
            item.selected_danmaku_title = "低智商犯罪 第11集"
            return True

        def switch_danmaku_source(self, item: PlayItem, page_url: str) -> str:
            self.switch_calls.append(page_url)
            item.danmaku_xml = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第十一集弹幕</d></i>'
            return item.danmaku_xml

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))

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

    controller = FakeDanmakuController()

    def playback_loader(item: PlayItem) -> PlaybackLoadResult:
        assert item.title == "百度"
        return PlaybackLoadResult(
            replacement_playlist=[
                PlayItem(
                    title="11.mp4(1.21 GB)",
                    url="http://m/11.mp4",
                    media_title="低智商犯罪",
                    play_source="百度",
                )
            ],
            replacement_start_index=0,
        )

    session = PlayerSession(
        vod=VodItem(vod_id="drive-1", vod_name="低智商犯罪"),
        playlist=[PlayItem(title="百度", url="", vod_id="https://pan.baidu.com/s/demo", media_title="低智商犯罪", play_source="百度")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        playback_loader=playback_loader,
        async_playback_loader=True,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_danmaku_paths) == 1)
    assert controller.load_calls == ["11.mp4(1.21 GB)"]
    assert controller.switch_calls == ["https://v.qq.com/ep11"]
    assert "第十一集弹幕" in session.playlist[0].danmaku_xml
    assert "当前播放: 11.mp4(1.21 GB)" in window.log_view.toPlainText()


def test_player_window_playlist_click_restores_cached_danmaku_for_target_item(qtbot) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.load_calls: list[str] = []
            self.switch_calls: list[str] = []

        def load_cached_danmaku_sources(
            self,
            item: PlayItem,
            playlist: list[PlayItem] | None = None,
            media_duration_seconds: int = 0,
        ) -> bool:
            del playlist, media_duration_seconds
            self.load_calls.append(item.title)
            item.danmaku_search_title = "红果短剧"
            item.danmaku_search_episode = "2集"
            item.danmaku_search_query = "红果短剧 2集"
            item.danmaku_candidates = [
                DanmakuSourceGroup(
                    provider="tencent",
                    provider_label="腾讯",
                    options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第2集", url="https://v.qq.com/ep2")],
                )
            ]
            item.selected_danmaku_provider = "tencent"
            item.selected_danmaku_url = "https://v.qq.com/ep2"
            item.selected_danmaku_title = "红果短剧 第2集"
            return item.title == "第2集"

        def switch_danmaku_source(self, item: PlayItem, page_url: str) -> str:
            self.switch_calls.append(page_url)
            item.danmaku_xml = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第二集弹幕</d></i>'
            return item.danmaku_xml

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.loaded_danmaku_paths: list[str] = []
            self._next_track_id = 150

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def pause(self) -> None:
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

    controller = FakeDanmakuController()
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="红果短剧"),
        playlist=[
            PlayItem(
                title="第1集",
                url="https://stream.example/1.m3u8",
                media_title="红果短剧",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一集弹幕</d></i>',
            ),
            PlayItem(
                title="第2集",
                url="https://stream.example/2.m3u8",
                media_title="红果短剧",
            ),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: len(window.video.loaded_danmaku_paths) == 1)

    target_item = window.playlist.item(1)
    assert target_item is not None

    window._play_clicked_item(target_item)

    _spin_until(
        lambda: (
            controller.switch_calls == ["https://v.qq.com/ep2"]
            and "第二集弹幕" in session.playlist[1].danmaku_xml
        )
    )
    assert controller.load_calls[-1] == "第2集"
    assert controller.switch_calls == ["https://v.qq.com/ep2"]
    assert "第二集弹幕" in session.playlist[1].danmaku_xml
    log_text = window.log_view.toPlainText()
    assert "当前播放: 第2集" in log_text


def test_player_window_auto_downloads_danmaku_when_target_cache_is_missing(qtbot, monkeypatch) -> None:
    class FakeDanmakuController:
        def __init__(self) -> None:
            self.auto_calls: list[tuple[str, list[PlayItem] | None]] = []

        def load_cached_danmaku_sources(
            self,
            item: PlayItem,
            playlist: list[PlayItem] | None = None,
            media_duration_seconds: int = 0,
        ) -> bool:
            del item, playlist, media_duration_seconds
            return False

        def switch_danmaku_source(self, item: PlayItem, page_url: str) -> str:
            raise AssertionError(f"unexpected cached source download: {item.title} {page_url}")

        def auto_resolve_danmaku(
            self,
            item: PlayItem,
            playlist: list[PlayItem] | None = None,
            *,
            media_duration_seconds: int = 0,
        ) -> bool:
            del media_duration_seconds
            self.auto_calls.append((item.title, playlist))
            item.danmaku_xml = '<i><d p="1,1,25,16777215">第十五集</d></i>'
            return True

    controller = FakeDanmakuController()
    item = PlayItem(
        title="15(5.41 GB)",
        url="https://media.example/15.mp4",
        media_title="百花杀",
        index=14,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.session = PlayerSession(
        vod=VodItem(vod_id="series-1", vod_name="百花杀"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        danmaku_controller=controller,
    )
    window.current_index = 0

    monkeypatch.setattr(
        window,
        "_start_danmaku_source_task",
        lambda _item, *, task, **_kwargs: task(),
    )

    window._maybe_restore_cached_danmaku_for_current_item()

    assert controller.auto_calls == [("15(5.41 GB)", window.session.playlist)]
    assert "第十五集" in item.danmaku_xml


def test_player_window_playlist_click_reloads_same_danmaku_xml_with_fresh_subtitle_path(qtbot) -> None:
    shared_xml = '<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">同一集弹幕</d></i>'

    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.loaded_danmaku_paths: list[str] = []
            self.removed_danmaku_track_ids: list[int] = []
            self._next_track_id = 70
            self._seen_paths: set[str] = set()

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def pause(self) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def audio_tracks(self) -> list[AudioTrack]:
            return []

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_danmaku_paths.append(path)
            if path in self._seen_paths:
                return None
            self._seen_paths.add(path)
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            if track_id is not None:
                self.removed_danmaku_track_ids.append(track_id)

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
            PlayItem(title="第1集 天屎驾到真案降临", url="http://m/1@128362", media_title="同一剧", danmaku_xml=shared_xml),
            PlayItem(title="第1集 天屎驾到真案降临", url="http://m/1@128386", media_title="同一剧", danmaku_xml=shared_xml),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: len(window.video.loaded_danmaku_paths) == 1)

    target_item = window.playlist.item(1)
    assert target_item is not None
    initial_path = window.video.loaded_danmaku_paths[-1]

    window._play_clicked_item(target_item)

    qtbot.waitUntil(lambda: len(window.video.loaded_danmaku_paths) == 2)
    assert window.video.removed_danmaku_track_ids == [70]
    assert window.video.loaded_danmaku_paths[-1] != initial_path
    assert "弹幕加载失败" not in window.log_view.toPlainText()
    assert window.danmaku_combo.currentText() == "弹幕"


def test_player_window_logs_rewritten_episode_title_for_current_playback(qtbot) -> None:
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="02.mp4",
                original_title="02.mp4",
                episode_display_title="第2集 星火初燃",
                url="http://m/2.m3u8",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)

    assert "当前播放: 第2集 星火初燃" in window.log_view.toPlainText()


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


def test_player_window_refresh_button_prefers_rewritten_episode_title(qtbot) -> None:
    session = make_player_session(start_index=1)
    session.playlist[1].episode_display_title = "第2集 星火初燃"
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)

    window.toggle_playback()

    assert window.windowTitle() == "alist-tvbox 播放器"

    window.refresh_button.click()

    assert window.windowTitle() == "Movie - 第2集 星火初燃"


def test_player_window_refresh_button_reloads_auto_spider_subtitle(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件视频"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕 [插件]",
                        lang="",
                        url="http://127.0.0.1:4567/sub/1.srt",
                        format="application/x-subrip",
                        source="spider",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    window.video.loaded_external_subtitles.clear()
    window.video.subtitle_apply_calls.clear()

    window.refresh_button.click()

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == [("track", 91)]
    assert window.subtitle_combo.currentText() == "外挂字幕 [插件]"


def test_player_window_refresh_button_reloads_selected_external_subtitle(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            return 91

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    class TextResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    monkeypatch.setattr(
        player_window_module.httpx,
        "get",
        lambda url, **kwargs: TextResponse("1\n00:00:00,000 --> 00:00:01,000\n你好\n"),
    )
    session = PlayerSession(
        vod=VodItem(vod_id="BV1", vod_name="B站视频"),
        playlist=[
            PlayItem(
                title="第1话",
                url="http://m/1.m3u8",
                headers={"Referer": "https://www.bilibili.com/"},
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="中文 [B站]",
                        lang="ai-zh",
                        url="http://sub/zh.srt",
                        format="application/x-subrip",
                        source="bilibili",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.subtitle_combo.setCurrentIndex(2)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    window.video.loaded_external_subtitles.clear()
    window.video.subtitle_apply_calls.clear()

    window.refresh_button.click()

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == [("track", 91)]
    assert window.subtitle_combo.currentText() == "中文 [B站]"


def test_player_window_play_next_reloads_selected_spider_karaoke_subtitle_for_new_item(qtbot, tmp_path) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.removed_subtitle_tracks: list[int | None] = []
            self._next_track_id = 91

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            self.removed_subtitle_tracks.append(track_id)

        def position_seconds(self) -> int:
            return 0

    first_subtitle_path = tmp_path / "song-1.ass"
    second_subtitle_path = tmp_path / "song-2.ass"
    first_subtitle_path.write_text("first", encoding="utf-8")
    second_subtitle_path.write_text("second", encoding="utf-8")

    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件歌曲"),
        playlist=[
            PlayItem(
                title="第1首",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="逐字歌词 [插件]",
                        lang="",
                        url=str(first_subtitle_path),
                        format="text/x-ass",
                        source="spider",
                    )
                ],
            ),
            PlayItem(
                title="第2首",
                url="http://m/2.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="逐字歌词 [插件]",
                        lang="",
                        url=str(second_subtitle_path),
                        format="text/x-ass",
                        source="spider",
                    )
                ],
            ),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    window.subtitle_combo.setCurrentIndex(1)
    window.subtitle_combo.setCurrentIndex(window.subtitle_combo.findText("逐字歌词 [插件]"))

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 2, timeout=2000)

    window.video.load_calls.clear()
    window.video.loaded_external_subtitles.clear()
    window.video.subtitle_apply_calls.clear()

    window.play_next()

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    assert window.video.load_calls == [("http://m/2.m3u8", 0)]
    assert [Path(path).read_text(encoding="utf-8") for path, _ in window.video.loaded_external_subtitles] == ["second"]
    assert [select_for_secondary for _path, select_for_secondary in window.video.loaded_external_subtitles] == [False]
    assert window.video.subtitle_apply_calls == [("track", 93)]
    assert window.subtitle_combo.currentText() == "逐字歌词 [插件]"


def test_player_window_play_next_recovers_selected_spider_karaoke_after_stale_track_snapshot(qtbot, tmp_path) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.removed_subtitle_tracks: list[int | None] = []
            self._next_track_id = 91
            self._subtitle_tracks_calls = 0

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))
            self._subtitle_tracks_calls = 0

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            self._subtitle_tracks_calls += 1
            if self._subtitle_tracks_calls == 1:
                return [SubtitleTrack(id=77, title="旧外挂歌词", lang="", is_default=False, is_forced=False, label="旧外挂歌词")]
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            self.removed_subtitle_tracks.append(track_id)

        def position_seconds(self) -> int:
            return 0

    first_subtitle_path = tmp_path / "song-1.ass"
    second_subtitle_path = tmp_path / "song-2.ass"
    first_subtitle_path.write_text("first", encoding="utf-8")
    second_subtitle_path.write_text("second", encoding="utf-8")

    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件歌曲"),
        playlist=[
            PlayItem(
                title="第1首",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="逐字歌词 [插件]",
                        lang="",
                        url=str(first_subtitle_path),
                        format="text/x-ass",
                        source="spider",
                    )
                ],
            ),
            PlayItem(
                title="第2首",
                url="http://m/2.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="逐字歌词 [插件]",
                        lang="",
                        url=str(second_subtitle_path),
                        format="text/x-ass",
                        source="spider",
                    )
                ],
            ),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.subtitle_combo.setCurrentIndex(1)
    window.subtitle_combo.setCurrentIndex(window.subtitle_combo.findText("逐字歌词 [插件]"))

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    window.video.loaded_external_subtitles.clear()
    window.video.subtitle_apply_calls.clear()

    window.play_next()

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1)

    assert [Path(path).read_text(encoding="utf-8") for path, _ in window.video.loaded_external_subtitles] == ["second"]
    assert window.video.subtitle_apply_calls[-1] == ("track", 92)
    assert window.subtitle_combo.currentText() == "逐字歌词 [插件]"


def test_player_window_play_next_recovers_auto_spider_karaoke_after_stale_track_snapshot(qtbot, tmp_path) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._next_track_id = 91
            self._load_count = 0
            self._subtitle_tracks_calls = 0

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))
            self._load_count += 1
            self._subtitle_tracks_calls = 0

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            self._subtitle_tracks_calls += 1
            if self._load_count >= 2 and self._subtitle_tracks_calls == 1:
                return [SubtitleTrack(id=77, title="旧外挂歌词", lang="", is_default=False, is_forced=False, label="旧外挂歌词")]
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    first_subtitle_path = tmp_path / "song-1.ass"
    second_subtitle_path = tmp_path / "song-2.ass"
    first_subtitle_path.write_text("first", encoding="utf-8")
    second_subtitle_path.write_text("second", encoding="utf-8")

    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件歌曲"),
        playlist=[
            PlayItem(
                title="第1首",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="逐字歌词 [插件]",
                        lang="",
                        url=str(first_subtitle_path),
                        format="text/x-ass",
                        source="spider",
                    )
                ],
            ),
            PlayItem(
                title="第2首",
                url="http://m/2.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="逐字歌词 [插件]",
                        lang="",
                        url=str(second_subtitle_path),
                        format="text/x-ass",
                        source="spider",
                    )
                ],
            ),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1)
    window.video.loaded_external_subtitles.clear()
    window.video.subtitle_apply_calls.clear()

    window.play_next()

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1)

    assert [Path(path).read_text(encoding="utf-8") for path, _ in window.video.loaded_external_subtitles] == ["second"]
    assert window.subtitle_combo.currentText() == "逐字歌词 [插件]"


def test_player_window_play_next_recovers_auto_spider_karaoke_after_multiple_stale_track_snapshots(qtbot, tmp_path) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self._next_track_id = 91
            self._load_count = 0
            self._subtitle_tracks_calls = 0

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))
            self._load_count += 1
            self._subtitle_tracks_calls = 0

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            self._subtitle_tracks_calls += 1
            if self._load_count >= 2 and self._subtitle_tracks_calls <= 5:
                return [SubtitleTrack(id=77, title="旧外挂歌词", lang="", is_default=False, is_forced=False, label="旧外挂歌词")]
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    first_subtitle_path = tmp_path / "song-1.ass"
    second_subtitle_path = tmp_path / "song-2.ass"
    first_subtitle_path.write_text("first", encoding="utf-8")
    second_subtitle_path.write_text("second", encoding="utf-8")

    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件歌曲"),
        playlist=[
            PlayItem(
                title="第1首",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="逐字歌词 [插件]",
                        lang="",
                        url=str(first_subtitle_path),
                        format="text/x-ass",
                        source="spider",
                    )
                ],
            ),
            PlayItem(
                title="第2首",
                url="http://m/2.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="逐字歌词 [插件]",
                        lang="",
                        url=str(second_subtitle_path),
                        format="text/x-ass",
                        source="spider",
                    )
                ],
            ),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1)
    window.video.loaded_external_subtitles.clear()
    window.video.subtitle_apply_calls.clear()

    window.play_next()

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1)

    assert [Path(path).read_text(encoding="utf-8") for path, _ in window.video.loaded_external_subtitles] == ["second"]
    assert window.subtitle_combo.currentText() == "逐字歌词 [插件]"


def test_player_window_play_next_recovers_selected_spider_karaoke_when_track_id_is_delayed_after_stale_snapshots(
    qtbot, tmp_path
) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.loaded_external_subtitles: list[tuple[str, bool]] = []
            self.subtitle_apply_calls: list[tuple[str, int | None]] = []
            self.removed_subtitle_tracks: list[int | None] = []
            self._next_track_id = 91
            self._load_count = 0
            self._subtitle_tracks_calls = 0
            self._pending_track_id = True

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))
            self._load_count += 1
            self._subtitle_tracks_calls = 0
            if self._load_count >= 2:
                self._pending_track_id = True

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            self._subtitle_tracks_calls += 1
            if self._load_count >= 2 and self._subtitle_tracks_calls <= 10:
                return [SubtitleTrack(id=77, title="旧外挂歌词", lang="", is_default=False, is_forced=False, label="旧外挂歌词")]
            return []

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            self.subtitle_apply_calls.append((mode, track_id))
            return track_id

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            self.loaded_external_subtitles.append((path, select_for_secondary))
            if self._load_count >= 2 and self._pending_track_id:
                self._pending_track_id = False
                return None
            track_id = self._next_track_id
            self._next_track_id += 1
            return track_id

        def remove_subtitle_track(self, track_id: int | None) -> None:
            self.removed_subtitle_tracks.append(track_id)

        def position_seconds(self) -> int:
            return 0

    first_subtitle_path = tmp_path / "song-1.ass"
    second_subtitle_path = tmp_path / "song-2.ass"
    first_subtitle_path.write_text("first", encoding="utf-8")
    second_subtitle_path.write_text("second", encoding="utf-8")

    session = PlayerSession(
        vod=VodItem(vod_id="sp1", vod_name="插件歌曲"),
        playlist=[
            PlayItem(
                title="第1首",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="逐字歌词 [插件]",
                        lang="",
                        url=str(first_subtitle_path),
                        format="text/x-ass",
                        source="spider",
                    )
                ],
            ),
            PlayItem(
                title="第2首",
                url="http://m/2.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="逐字歌词 [插件]",
                        lang="",
                        url=str(second_subtitle_path),
                        format="text/x-ass",
                        source="spider",
                    )
                ],
            ),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 1, timeout=2000)

    window.subtitle_combo.setCurrentIndex(1)
    window.subtitle_combo.setCurrentIndex(window.subtitle_combo.findText("逐字歌词 [插件]"))

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 2, timeout=2000)

    window.video.loaded_external_subtitles.clear()
    window.video.subtitle_apply_calls.clear()

    window.play_next()

    qtbot.waitUntil(lambda: len(window.video.loaded_external_subtitles) == 2, timeout=2000)

    assert [Path(path).read_text(encoding="utf-8") for path, _ in window.video.loaded_external_subtitles] == [
        "second",
        "second",
    ]
    assert window.video.subtitle_apply_calls == [("track", 93)]
    assert window.subtitle_combo.currentText() == "逐字歌词 [插件]"
    assert "字幕切换失败" not in window.log_view.toPlainText()


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
    window._update_playback_observation(position=119, duration=120)

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


def test_player_window_logs_loading_message_while_initial_item_resolves_async(qtbot) -> None:
    controller = RecordingPlayerController()
    resolved_vod = VodItem(
        vod_id="ep-1",
        vod_name="Resolved Episode 1",
        items=[PlayItem(title="Episode 1", url="http://resolved/1.m3u8", vod_id="ep-1")],
    )
    ready = threading.Event()

    def detail_resolver(_item: PlayItem) -> VodItem:
        assert ready.wait(timeout=1)
        return resolved_vod

    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[PlayItem(title="Episode 1", url="", vod_id="ep-1")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        detail_resolver=detail_resolver,
    )

    started_at = time.perf_counter()
    window.open_session(session)
    elapsed_seconds = time.perf_counter() - started_at

    assert elapsed_seconds < 0.1
    assert window.video.load_calls == []
    assert "正在加载视频详情: Episode 1" in window.log_view.toPlainText()

    ready.set()

    qtbot.waitUntil(lambda: window.video.load_calls == [("http://resolved/1.m3u8", 0)], timeout=1000)


def test_player_window_keeps_resolving_state_plain_and_logs_source_address_in_playback_log(qtbot) -> None:
    ready = threading.Event()

    def load_item(item: PlayItem) -> None:
        assert ready.wait(timeout=1)
        item.url = "http://resolved/episode-1.m3u8"

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = make_player_session(start_index=0)
    session.playlist[0].title = "网盘剧集"
    session.playlist[0].url = ""
    session.playlist[0].vod_id = "https://pan.baidu.com/s/demo"
    session.playback_loader = load_item
    session.async_playback_loader = True

    window.open_session(session)

    log_lines = window.log_view.toPlainText().splitlines()
    assert_timestamped_log_line(log_lines[0], "正在解析视频详情: https://pan.baidu.com/s/demo")
    assert_timestamped_log_line(log_lines[1], "正在加载播放地址: 网盘剧集")
    ready.set()


def test_player_window_logs_youtube_detail_parse_progress(qtbot) -> None:
    ready = threading.Event()

    def load_item(item: PlayItem) -> None:
        assert ready.wait(timeout=1)
        item.url = "https://media.example/youtube.mp4"

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="yt:video:test123", vod_name="YouTube", detail_style="youtube"),
        playlist=[
            PlayItem(
                title="YouTube Video",
                url="",
                original_url="https://www.youtube.com/watch?v=test123",
                vod_id="yt:video:test123",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        opening_seconds=0,
        ending_seconds=0,
        async_playback_loader=True,
    )
    session.playback_loader = load_item

    window.open_session(session)

    assert "详情解析中" in window.log_view.toPlainText()
    ready.set()
    qtbot.waitUntil(lambda: "详情解析完成" in window.log_view.toPlainText(), timeout=1000)


def test_player_window_logs_resolving_startup_message(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window._set_startup_state(window._startup_coordinator.resolving())

    assert_timestamped_log_line(window.log_view.toPlainText(), "正在解析播放地址")


def test_player_window_does_not_duplicate_same_resolving_startup_message(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    state = window._startup_coordinator.resolving()
    window._set_startup_state(state)
    window._set_startup_state(state)

    lines = window.log_view.toPlainText().splitlines()
    assert len(lines) == 1
    assert_timestamped_log_line(lines[0], "正在解析播放地址")


def test_player_window_appends_timestamp_to_playback_logs(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window._append_log("播放失败: boom")

    assert_timestamped_log_line(window.log_view.toPlainText(), "播放失败: boom")


def test_player_window_does_not_duplicate_same_message_when_dedupe_enabled(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window._append_log("正在解析播放地址", dedupe=True)
    window._append_log("正在解析播放地址", dedupe=True)
    window._append_log("正在加载播放地址: Episode 1", dedupe=True)

    lines = window.log_view.toPlainText().splitlines()
    assert len(lines) == 2
    assert_timestamped_log_line(lines[0], "正在解析播放地址")
    assert_timestamped_log_line(lines[1], "正在加载播放地址: Episode 1")


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


def test_player_window_normalizes_huya_headers_for_direct_playback(qtbot) -> None:
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
    session.playlist = [
        PlayItem(
            title="线路1",
            url="https://al.flv.huya.com/src/demo.flv",
            vod_id="huya-1",
        )
    ]
    session.use_local_history = False

    window.open_session(session)

    assert window.video.load_calls == [
        (
            "https://al.flv.huya.com/src/demo.flv",
            False,
            0,
            {
                "Referer": "https://www.huya.com/",
                "User-Agent": "HYSDK(Windows,30000002)_APP(pc_exe&7080000&official)_SDK(trans&2.34.0.5795)",
            },
        )
    ]


def test_player_window_loads_play_item_via_async_session_loader_without_blocking_open_session(qtbot) -> None:
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

    ready = threading.Event()

    def load_item(item: PlayItem) -> None:
        assert ready.wait(timeout=1)
        item.url = "http://emby/1.mp4"
        item.headers = {"User-Agent": "Yamby"}

    controller = RecordingPlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = FakeVideo()
    session = make_player_session(start_index=0)
    session.playlist = [PlayItem(title="Episode 1", url="", vod_id="1-3458")]
    session.use_local_history = False
    session.playback_loader = load_item
    session.async_playback_loader = True

    started_at = time.perf_counter()
    window.open_session(session)
    elapsed_seconds = time.perf_counter() - started_at

    assert elapsed_seconds < 0.1
    assert window.video.load_calls == []
    assert "正在加载播放地址: Episode 1" in window.log_view.toPlainText()

    ready.set()

    qtbot.waitUntil(
        lambda: window.video.load_calls == [("http://emby/1.mp4", False, 0, {"User-Agent": "Yamby"})],
        timeout=1000,
    )


def test_player_window_async_session_loader_preserves_resume_offset(qtbot) -> None:
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

    ready = threading.Event()

    def load_item(item: PlayItem) -> None:
        assert ready.wait(timeout=1)
        item.url = "http://emby/1.mp4"
        item.headers = {"User-Agent": "Yamby"}

    controller = RecordingPlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = FakeVideo()
    session = make_player_session(start_index=0)
    session.start_position_seconds = 42
    session.playlist = [PlayItem(title="Episode 1", url="", vod_id="1-3458")]
    session.use_local_history = False
    session.playback_loader = load_item
    session.async_playback_loader = True

    window.open_session(session)

    assert window.video.load_calls == []

    ready.set()

    qtbot.waitUntil(
        lambda: window.video.load_calls == [("http://emby/1.mp4", False, 42, {"User-Agent": "Yamby"})],
        timeout=1000,
    )


def test_player_window_async_loader_preloads_prefilled_ytdlp_url_before_initial_playback(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int, dict[str, str], str]] = []

        def load(
            self,
            url: str,
            pause: bool = False,
            start_seconds: int = 0,
            headers: dict[str, str] | None = None,
            ytdl_format: str = "",
        ) -> None:
            self.load_calls.append((url, pause, start_seconds, headers or {}, ytdl_format))

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    ready = threading.Event()

    def load_item(item: PlayItem) -> None:
        assert ready.wait(timeout=1)
        item.title = "Hydrated Video"
        item.media_title = "Hydrated Video"
        item.headers = {"Referer": "https://www.youtube.com/"}
        item.playback_qualities = [
            VideoQualityOption(
                id="ytdlp_1080",
                label="1080P",
                ytdl_format="bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best",
            )
        ]

    controller = RecordingPlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = FakeVideo()
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(
            title="https://www.youtube.com/watch?v=test123",
            url="https://www.youtube.com/watch?v=test123",
            original_url="https://www.youtube.com/watch?v=test123",
            vod_id="https://www.youtube.com/watch?v=test123",
            ytdl_format="bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best",
            selected_playback_quality_id="ytdlp_1080",
        )
    ]
    session.use_local_history = False
    session.playback_loader = load_item
    session.async_playback_loader = True

    window.open_session(session)

    assert window.video.load_calls == []

    ready.set()

    qtbot.waitUntil(lambda: window.playlist.item(0).text() == "Hydrated Video", timeout=1000)
    assert window.video.load_calls == [
        (
            "https://www.youtube.com/watch?v=test123",
            False,
            0,
            {"Referer": "https://www.youtube.com/"},
            "bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best",
        )
    ]


def test_player_window_async_loader_does_not_play_plain_youtube_page_url_before_resolution(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[str] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            del pause, start_seconds
            self.load_calls.append(url)

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    ready = threading.Event()

    def load_item(item: PlayItem) -> None:
        assert ready.wait(timeout=1)
        item.url = "https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/4102444800/playlist/index.m3u8"
        item.headers = {"Referer": "https://www.youtube.com/"}

    class PassThroughM3U8AdFilter:
        def should_prepare(self, url: str) -> bool:
            return False

    window = PlayerWindow(FakePlayerController(), m3u8_ad_filter=PassThroughM3U8AdFilter())
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(
            title="YouTube",
            url="https://www.youtube.com/watch?v=test123",
            original_url="https://www.youtube.com/watch?v=test123",
            vod_id="yt:video:test123",
        )
    ]
    session.playback_loader = load_item
    session.async_playback_loader = True

    window.open_session(session)

    qtbot.wait(100)
    assert video.load_calls == []
    ready.set()
    qtbot.waitUntil(
        lambda: video.load_calls
        == ["https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/4102444800/playlist/index.m3u8"],
        timeout=1000,
    )


def test_player_window_playback_prepare_prefers_youtube_resolved_media_over_original_page(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    item = PlayItem(
        title="YouTube",
        url="https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/4102444800/playlist/index.m3u8",
        original_url="https://www.youtube.com/watch?v=test123",
        vod_id="yt:video:test123",
    )

    assert window._playback_prepare_source_url(item) == item.url


def test_player_window_async_loader_refreshes_title_metadata_and_playlist_after_hydration(qtbot, monkeypatch) -> None:
    poster_sources: list[tuple[str, str]] = []

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        poster_sources.append((target, source))

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    session = PlayerSession(
        vod=VodItem(
            vod_id="https://www.youtube.com/watch?v=test123",
            vod_name="https://www.youtube.com/watch?v=test123",
        ),
        playlist=[
            PlayItem(
                title="https://www.youtube.com/watch?v=test123",
                url="",
                original_url="https://www.youtube.com/watch?v=test123",
                vod_id="https://www.youtube.com/watch?v=test123",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        async_playback_loader=True,
    )
    session.playback_loader = lambda item: None

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    monkeypatch.setattr(
        window,
        "_start_playback_loader",
        lambda previous_index, start_position_seconds, pause, hydrate_only=False: (
            setattr(window, "_playback_loader_request_id", window._playback_loader_request_id + 1),
            setattr(
                window,
                "_pending_playback_loader",
                player_window_module._PendingPlaybackLoader(
                    index=window.current_index,
                    previous_index=previous_index,
                    start_position_seconds=start_position_seconds,
                    pause=pause,
                    hydrate_only=hydrate_only,
                ),
            ),
            window._append_log(
                f"{'正在刷新详情' if hydrate_only else '正在加载播放地址'}: {window.session.playlist[window.current_index].title}"
            ),
        ),
    )

    window.open_session(session)

    assert "正在加载播放地址" in window.log_view.toPlainText()
    session.vod.vod_name = "Hydrated Video"
    session.vod.vod_pic = "https://img.example/poster.jpg"
    session.vod.vod_content = "hydrated description"
    session.playlist[0].title = "Hydrated Video"
    session.playlist[0].media_title = "Hydrated Video"
    session.playlist[0].url = "https://media.example/youtube.mp4"
    session.playlist[0].headers = {"Referer": "https://www.youtube.com/"}
    session.playlist[0].external_subtitles = [
        ExternalSubtitleOption(
            name="English [yt-dlp]",
            lang="en",
            url="https://sub.example/en.vtt",
            format="vtt",
            source="ytdlp",
        )
    ]
    session.playlist[0].playback_qualities = [
        VideoQualityOption(
            id="720p",
            label="720P",
            url="https://media.example/youtube.mp4",
        )
    ]
    session.playlist[0].selected_playback_quality_id = "720p"

    window._handle_playback_loader_succeeded(window._playback_loader_request_id, None)

    assert "Hydrated Video" in window.windowTitle()
    assert window.playlist.item(0).text() == "Hydrated Video"
    assert "hydrated description" in window.metadata_view.toPlainText()
    assert ("video", "https://img.example/poster.jpg") in poster_sources


def test_player_window_hydrate_only_loader_refreshes_media_controls(qtbot, monkeypatch) -> None:
    session = PlayerSession(
        vod=VodItem(vod_id="yt:video:test123", vod_name="Hydrated Video"),
        playlist=[
            PlayItem(
                title="Hydrated Video",
                url="https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/4102444800/playlist/index.m3u8",
                original_url="https://www.youtube.com/watch?v=test123",
                vod_id="yt:video:test123",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="English [yt-dlp]",
                        lang="en",
                        url="https://sub.example/en.vtt",
                        format="vtt",
                        source="ytdlp",
                    )
                ],
                audio_tracks=[
                    YtdlpAudioTrackOption(
                        id="ytdlp_audio_en_251",
                        label="English",
                        lang="en",
                        format_id="251",
                    )
                ],
                playback_qualities=[
                    VideoQualityOption(
                        id="ytdlp_1080",
                        label="1080P",
                        url="https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/4102444800/playlist/index.m3u8",
                    )
                ],
                selected_audio_track_id="ytdlp_audio_en_251",
                selected_playback_quality_id="ytdlp_1080",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.session = session
    window.current_index = 0
    window._playback_loader_request_id = 7
    window._pending_playback_loader = player_window_module._PendingPlaybackLoader(
        index=0,
        previous_index=0,
        start_position_seconds=0,
        pause=False,
        hydrate_only=True,
    )

    calls: list[str] = []
    monkeypatch.setattr(window, "_maybe_restore_cached_danmaku_for_current_item", lambda **_kwargs: calls.append("restore-danmaku"))
    monkeypatch.setattr(window, "_refresh_subtitle_state", lambda: calls.append("refresh-subtitle"))
    monkeypatch.setattr(
        window,
        "_schedule_followup_subtitle_refresh_if_needed",
        lambda current_item: calls.append(f"followup-subtitle:{current_item.title}"),
    )
    monkeypatch.setattr(window, "_refresh_audio_state", lambda: calls.append("refresh-audio"))
    monkeypatch.setattr(window, "_refresh_video_quality_state", lambda: calls.append("refresh-quality"))
    monkeypatch.setattr(window, "_configure_danmaku_for_current_item", lambda: calls.append("configure-danmaku"))

    window._handle_playback_loader_succeeded(window._playback_loader_request_id, None)

    assert calls == [
        "restore-danmaku",
        "refresh-subtitle",
        "followup-subtitle:Hydrated Video",
        "refresh-audio",
        "refresh-quality",
        "configure-danmaku",
    ]


@pytest.mark.parametrize(
    ("pending_prepare_index", "expected_configure_calls"),
    [
        (0, []),
        (1, ["configure-danmaku"]),
    ],
)
def test_player_window_hydrate_only_defers_danmaku_during_matching_prepare(
    qtbot,
    monkeypatch,
    pending_prepare_index: int,
    expected_configure_calls: list[str],
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.session = make_player_session(start_index=0)
    window.current_index = 0
    window._playback_loader_request_id = 7
    window._pending_playback_loader = player_window_module._PendingPlaybackLoader(
        index=0,
        previous_index=0,
        start_position_seconds=0,
        pause=False,
        hydrate_only=True,
    )
    window._pending_playback_prepare = player_window_module._PendingPlaybackPrepare(
        index=pending_prepare_index,
        previous_index=0,
        start_position_seconds=0,
        pause=False,
        source_url="http://m/1.m3u8",
    )
    configure_calls: list[str] = []
    monkeypatch.setattr(
        window,
        "_configure_danmaku_for_current_item",
        lambda: configure_calls.append("configure-danmaku"),
    )

    window._handle_playback_loader_succeeded(window._playback_loader_request_id, None)

    assert configure_calls == expected_configure_calls


def test_player_window_ignores_stale_async_loader_result_after_switching_items(qtbot) -> None:
    ready = threading.Event()
    session = PlayerSession(
        vod=VodItem(vod_id="vod-1", vod_name="Placeholder"),
        playlist=[
            PlayItem(title="待解析", url="", vod_id="ep-1", original_url="https://www.youtube.com/watch?v=one"),
            PlayItem(title="第二集", url="https://media.example/two.mp4", vod_id="ep-2"),
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        async_playback_loader=True,
    )

    def playback_loader(item: PlayItem) -> None:
        assert ready.wait(timeout=1)
        session.vod.vod_name = "旧结果"
        item.title = "旧结果"
        item.url = "https://media.example/stale.mp4"

    session.playback_loader = playback_loader

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    window.current_index = 1
    window.playlist.setCurrentRow(1)
    window._refresh_window_title()
    ready.set()

    qtbot.wait(100)
    assert window.current_index == 1
    assert window.playlist.item(1).text() == "第二集"
    assert "旧结果" not in window.windowTitle()


def test_player_window_async_metadata_hydration_refreshes_metadata_without_reloading_video(qtbot, monkeypatch) -> None:
    poster_sources: list[tuple[str, str]] = []

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

    def fake_start(self, source: str, request_id: int, *, target: str, on_loaded=None) -> None:
        poster_sources.append((target, source))

    monkeypatch.setattr(PlayerWindow, "_start_poster_load", fake_start)

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4", vod_id="ep1")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda current_session: VodItem(
            vod_id=current_session.vod.vod_id,
            vod_name=current_session.vod.vod_name,
            vod_pic="https://img.example/poster.jpg",
            vod_content="豆瓣简介",
            vod_remarks="8.1",
            metadata_field_sources={"poster": "tmdb", "overview": "local_douban", "rating": "local_douban"},
        ),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: "豆瓣简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert window.video.load_calls == [("https://media.example/1.mp4", False, 0, {})]
    assert "评分: 8.1" in window.metadata_view.toPlainText()
    assert ("detail", "https://img.example/poster.jpg") in poster_sources
    assert "元数据增强: 原始标题 年代= 分类=自动" in window.log_view.toPlainText()
    assert "元数据已更新" in window.log_view.toPlainText()
    assert "本地豆瓣" in window.log_view.toPlainText()
    assert "TMDB" in window.log_view.toPlainText()


def test_player_window_async_metadata_hydration_updates_metadata_scrape_dialog_title(qtbot) -> None:
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

    original_title = "努力克服自卑的我们 모두가 자신의 무가치함과 싸우고 있다 (2026)"
    corrected_title = "努力克服自卑的我们"
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name=original_title, vod_year="2026"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4", media_title=original_title)],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=object(),
        metadata_hydrator=lambda current_session: VodItem(
            vod_id=current_session.vod.vod_id,
            vod_name=corrected_title,
            vod_year="2026",
            vod_content="豆瓣简介",
            metadata_field_sources={"overview": "local_douban"},
        ),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window.session is not None and window.session.vod.vod_name == corrected_title, timeout=1000)

    window._open_metadata_scrape_dialog()

    assert window._metadata_scrape_title_edit.text() == corrected_title
    assert window._metadata_scrape_year_edit.text() == "2026"


def test_player_window_async_metadata_hydration_preserves_season_marked_media_title_for_scrape(qtbot) -> None:
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

    original_title = "入侵 第一季"
    corrected_title = "入侵"
    service = FakeMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name=original_title, vod_year="2021"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4", media_title=original_title)],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
        metadata_hydrator=lambda current_session: VodItem(
            vod_id=current_session.vod.vod_id,
            vod_name=corrected_title,
            vod_year="2021",
            vod_content="TMDB简介",
            metadata_field_sources={"overview": "tmdb"},
        ),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window.session is not None and window.session.vod.vod_name == corrected_title, timeout=1000)
    assert window.session.playlist[0].media_title == original_title

    window._open_metadata_scrape_dialog()

    assert window._metadata_scrape_title_edit.text() == original_title
    assert window._metadata_scrape_year_edit.text() == "2021"


def test_player_window_async_metadata_hydration_updates_danmaku_source_dialog_title(qtbot) -> None:
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

    original_title = "努力克服自卑的我们(2026) WEB-1080 简繁字幕 第10集"
    corrected_title = "努力克服自卑的我们"
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name=original_title, vod_year="2026"),
        playlist=[
            PlayItem(
                title="第10集",
                url="https://media.example/10.mp4",
                media_title=original_title,
                danmaku_search_title=original_title,
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda current_session: VodItem(
            vod_id=current_session.vod.vod_id,
            vod_name=corrected_title,
            vod_year="2026",
            vod_content="豆瓣简介",
            metadata_field_sources={"overview": "local_douban"},
        ),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window.session is not None and window.session.vod.vod_name == corrected_title, timeout=1000)

    window._open_danmaku_source_dialog()

    assert window._danmaku_source_title_edit.text() == corrected_title


def test_player_window_async_metadata_hydration_does_not_restart_episode_title_enhancement_with_corrected_title(qtbot) -> None:
    hydration_ready = threading.Event()
    enhancement_calls: list[str] = []

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    corrected_title = "刮削后的标题"

    def hydrate(_session: PlayerSession) -> VodItem:
        assert hydration_ready.wait(timeout=1)
        return VodItem(
            vod_id="v1",
            vod_name=corrected_title,
            vod_year="2026",
            vod_content="刮削后的简介",
        )

    def enhance(current_session: PlayerSession) -> list[PlayItem] | None:
        enhancement_calls.append(current_session.vod.vod_name)
        if current_session.vod.vod_name != corrected_title:
            return None
        return [
            PlayItem(
                title=current_session.playlist[0].title,
                url=current_session.playlist[0].url,
                vod_id=current_session.playlist[0].vod_id,
                original_title="S01E01.mkv",
                episode_display_title="第1集 星门初启",
                episode_title_source="tmdb",
            )
        ]

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_year="2026", vod_content="原始简介"),
        playlist=[
            PlayItem(
                title="S01E01.mkv",
                url="https://media.example/1.mp4",
                vod_id="ep1",
                original_title="S01E01.mkv",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=hydrate,
        episode_title_enhancer=enhance,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: enhancement_calls == ["原始标题"], timeout=1000)
    hydration_ready.set()
    qtbot.waitUntil(lambda: window.session is not None and window.session.vod.vod_name == corrected_title, timeout=1000)
    qtbot.wait(50)
    assert enhancement_calls == ["原始标题"]
    assert window.playlist.item(0).text() == "S01E01.mkv"


def test_player_window_async_metadata_hydration_preserves_manual_danmaku_source_title(qtbot) -> None:
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

    original_title = "努力克服自卑的我们(2026) WEB-1080 简繁字幕 第10集"
    corrected_title = "努力克服自卑的我们"
    manual_title = "手动修正标题"
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name=original_title, vod_year="2026"),
        playlist=[
            PlayItem(
                title="第10集",
                url="https://media.example/10.mp4",
                media_title=original_title,
                danmaku_search_title=manual_title,
                danmaku_search_query_overridden=True,
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda current_session: VodItem(
            vod_id=current_session.vod.vod_id,
            vod_name=corrected_title,
            vod_year="2026",
            vod_content="豆瓣简介",
            metadata_field_sources={"overview": "local_douban"},
        ),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window.session is not None and window.session.vod.vod_name == corrected_title, timeout=1000)

    window._open_danmaku_source_dialog()

    assert window._danmaku_source_title_edit.text() == manual_title


def test_player_window_async_metadata_hydration_skips_update_log_when_metadata_is_unchanged(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4", vod_id="ep1")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda current_session: VodItem(
            vod_id=current_session.vod.vod_id,
            vod_name=current_session.vod.vod_name,
            vod_content=current_session.vod.vod_content,
            metadata_field_sources=dict(current_session.vod.metadata_field_sources),
        ),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window._pending_metadata_session is None, timeout=1000)
    assert "元数据已更新" not in window.log_view.toPlainText()


def test_player_window_async_metadata_hydration_logs_iqiyi_with_localized_label(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4", vod_id="ep1")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_hydrator=lambda current_session: VodItem(
            vod_id=current_session.vod.vod_id,
            vod_name=current_session.vod.vod_name,
            vod_content="爱奇艺简介",
            metadata_field_sources={"overview": "iqiyi"},
        ),
    )

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: "爱奇艺简介" in window.metadata_view.toPlainText(), timeout=1000)
    assert "元数据已更新: 爱奇艺(简介)" in window.log_view.toPlainText()


def test_player_window_shows_episode_title_tabs_when_playlist_has_title_variants(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="示例剧集"),
        playlist=[
            PlayItem(
                title="第1集 星门初启",
                url="https://media.example/1.mp4",
                vod_id="ep1",
                original_title="S01E01.mkv",
                episode_display_title="第1集 星门初启",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    assert window.playlist_title_tabs.isHidden() is False
    assert window.playlist_title_tabs.tabText(0) == "剧集标题"
    assert window.playlist_title_tabs.tabText(1) == "原始文件名"
    assert window.playlist_title_tabs.currentIndex() == 0
    assert window.playlist.item(0).text() == "第1集 星门初启"


def test_player_window_hides_playlist_title_tabs_when_playlist_panel_is_closed(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="示例剧集"),
        playlist=[
            PlayItem(
                title="第1集 星门初启",
                url="https://media.example/1.mp4",
                vod_id="ep1",
                original_title="S01E01.mkv",
                episode_display_title="第1集 星门初启",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.show()

    assert window.playlist.isHidden() is False
    assert window.playlist_panel.isHidden() is False
    assert window.playlist_title_tabs.isHidden() is False

    window._cycle_playlist_panel_mode()

    assert window.playlist.isHidden() is True
    assert window.playlist_panel.isHidden() is True
    assert window.playlist_title_tabs.isHidden() is True

    window._cycle_playlist_panel_mode()

    assert window.playlist.isHidden() is False
    assert window.playlist_panel.isHidden() is False
    assert window.playlist_title_tabs.isHidden() is False


def test_player_window_playlist_button_cycles_bilibili_playlist_panel_modes(qtbot) -> None:
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(bilibili_grouped_playlist_tree_enabled=False),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.open_session(make_bilibili_grouped_session())
    window.show()

    assert window.playlist.isHidden() is False
    assert window.bilibili_playlist_tree.isHidden() is True
    assert window.toggle_playlist_button.toolTip() == "播放列表：普通列表"

    window._cycle_playlist_panel_mode()

    assert window.playlist.isHidden() is True
    assert window.bilibili_playlist_tree.isHidden() is False
    assert window.toggle_playlist_button.toolTip() == "播放列表：分组树"

    window._cycle_playlist_panel_mode()

    assert window.playlist.isHidden() is True
    assert window.bilibili_playlist_tree.isHidden() is True
    assert window.playlist_title_tabs.isHidden() is True
    assert window.toggle_playlist_button.toolTip() == "播放列表：隐藏"

    window._cycle_playlist_panel_mode()

    assert window.playlist.isHidden() is False
    assert window.bilibili_playlist_tree.isHidden() is True
    assert window.toggle_playlist_button.toolTip() == "播放列表：普通列表"


def test_player_window_playlist_button_remains_two_state_for_non_bilibili_sessions(qtbot) -> None:
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(bilibili_grouped_playlist_tree_enabled=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.open_session(make_player_session(start_index=0))
    window.show()

    assert window.playlist.isHidden() is False
    assert window.toggle_playlist_button.toolTip() == "播放列表：普通列表"

    window._cycle_playlist_panel_mode()

    assert window.playlist.isHidden() is True
    assert window.toggle_playlist_button.toolTip() == "播放列表：隐藏"

    window._cycle_playlist_panel_mode()

    assert window.playlist.isHidden() is False
    assert window.toggle_playlist_button.toolTip() == "播放列表：普通列表"


def test_player_window_switches_playlist_labels_without_changing_current_index(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="示例剧集"),
        playlist=[
            PlayItem(
                title="第1集 星门初启",
                url="https://media.example/1.mp4",
                vod_id="ep1",
                original_title="S01E01.mkv",
                episode_display_title="第1集 星门初启",
            ),
            PlayItem(
                title="第2集 星火初燃",
                url="https://media.example/2.mp4",
                vod_id="ep2",
                original_title="S01E02.mkv",
                episode_display_title="第2集 星火初燃",
            ),
        ],
        start_index=1,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.playlist_title_tabs.setCurrentIndex(1)

    assert window.current_index == 1
    assert window.playlist.currentRow() == 1
    assert window.playlist.item(0).text() == "S01E01.mkv"
    assert window.playlist.item(1).text() == "S01E02.mkv"


def test_player_window_async_episode_title_enhancer_updates_playlist_labels_late(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="示例剧集"),
        playlist=[
            PlayItem(
                title="S01E01.mkv",
                url="https://media.example/1.mp4",
                vod_id="ep1",
                original_title="S01E01.mkv",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        episode_title_enhancer=lambda current_session: [
            PlayItem(
                title=current_session.playlist[0].title,
                url=current_session.playlist[0].url,
                vod_id=current_session.playlist[0].vod_id,
                original_title="S01E01.mkv",
                episode_display_title="第1集 星门初启",
                episode_title_source="tmdb",
            )
        ],
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window.playlist_title_tabs.isHidden() is False, timeout=1000)
    assert window.playlist.item(0).text() == "第1集 星门初启"
    assert window.current_index == 0


def test_player_window_async_episode_title_enhancer_refreshes_active_window_title(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: float) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="示例剧集"),
        playlist=[
            PlayItem(
                title="S01E01.mkv",
                url="https://media.example/1.mp4",
                vod_id="ep1",
                original_title="S01E01.mkv",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        episode_title_enhancer=lambda current_session: [
            PlayItem(
                title=current_session.playlist[0].title,
                url=current_session.playlist[0].url,
                vod_id=current_session.playlist[0].vod_id,
                original_title="S01E01.mkv",
                episode_display_title="第1集 星门初启",
                episode_title_source="tmdb",
            )
        ],
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window.playlist.item(0).text() == "第1集 星门初启", timeout=1000)
    assert window.windowTitle() == "示例剧集 - 第1集 星门初启"


def test_player_window_logs_episode_title_mapping_with_source(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    captured: list[str] = []

    def record_info(message: str, *args) -> None:
        captured.append(message % args if args else message)

    monkeypatch.setattr(player_window_module.logger, "info", record_info)

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="示例剧集"),
        playlist=[
            PlayItem(
                title="S01E01.mkv",
                url="https://media.example/1.mp4",
                vod_id="ep1",
                original_title="S01E01.mkv",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        episode_title_enhancer=lambda current_session: [
            PlayItem(
                title=current_session.playlist[0].title,
                url=current_session.playlist[0].url,
                vod_id=current_session.playlist[0].vod_id,
                original_title="S01E01.mkv",
                episode_display_title="第1集 星门初启",
                episode_title_source="tmdb",
            )
        ],
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: any("剧集标题改写" in message for message in captured), timeout=1000)

    assert any("S01E01.mkv → 第1集 星门初启 [来源: tmdb]" in message for message in captured)


def test_player_window_async_episode_title_enhancer_preserves_existing_play_item_object(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    original_item = PlayItem(
        title="11.mp4(1.21 GB)",
        url="https://media.example/11.mp4",
        vod_id="ep11",
        path="/网盘剧集/11.mp4",
        original_title="11.mp4(1.21 GB)",
        play_source="百度",
        media_title="低智商犯罪",
        danmaku_pending=True,
    )
    session = PlayerSession(
        vod=VodItem(vod_id="drive-1", vod_name="低智商犯罪"),
        playlist=[original_item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        episode_title_enhancer=lambda current_session: [
            PlayItem(
                title=current_session.playlist[0].title,
                url=current_session.playlist[0].url,
                vod_id=current_session.playlist[0].vod_id,
                path=current_session.playlist[0].path,
                original_title=current_session.playlist[0].original_title,
                play_source=current_session.playlist[0].play_source,
                media_title=current_session.playlist[0].media_title,
                episode_display_title="第11集 真相逼近",
                episode_title_source="tmdb",
            )
        ],
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window.playlist.item(0).text() == "第11集 真相逼近", timeout=1000)
    assert session.playlist[0] is original_item
    assert session.playlist[0].danmaku_pending is True
    assert session.playlist[0].episode_display_title == "第11集 真相逼近"
    assert session.playlist[0].episode_title_source == "tmdb"


def test_player_window_async_episode_title_enhancer_reorders_playlist_and_keeps_current_item_selected(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int, dict[str, str] | None]] = []

        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            self.load_calls.append((url, pause, start_seconds, headers))

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    def enhance(current_session: PlayerSession) -> list[PlayItem]:
        return [
            PlayItem(
                title="1-4K.mp4",
                url="https://media.example/1-4k.mp4",
                vod_id="ep1-4k",
                original_title="1-4K.mp4",
                episode_display_title="第1集 星门初启",
                episode_title_source="tmdb",
            ),
            PlayItem(
                title="1-1080P.mp4",
                url="https://media.example/1-1080.mp4",
                vod_id="ep1-1080",
                original_title="1-1080P.mp4",
                episode_display_title="第1集 星门初启",
                episode_title_source="tmdb",
            ),
            PlayItem(
                title="2-4K.mp4",
                url="https://media.example/2-4k.mp4",
                vod_id="ep2-4k",
                original_title="2-4K.mp4",
                episode_display_title="第2集 星火初燃",
                episode_title_source="tmdb",
            ),
            PlayItem(
                title="2-1080P.mp4",
                url="https://media.example/2-1080.mp4",
                vod_id="ep2-1080",
                original_title="2-1080P.mp4",
                episode_display_title="第2集 星火初燃",
                episode_title_source="tmdb",
            ),
        ]

    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="示例剧集"),
        playlist=[
            PlayItem(title="1-4K.mp4", url="https://media.example/1-4k.mp4", vod_id="ep1-4k", original_title="1-4K.mp4"),
            PlayItem(title="2-4K.mp4", url="https://media.example/2-4k.mp4", vod_id="ep2-4k", original_title="2-4K.mp4"),
            PlayItem(title="1-1080P.mp4", url="https://media.example/1-1080.mp4", vod_id="ep1-1080", original_title="1-1080P.mp4"),
            PlayItem(title="2-1080P.mp4", url="https://media.example/2-1080.mp4", vod_id="ep2-1080", original_title="2-1080P.mp4"),
        ],
        start_index=2,
        start_position_seconds=0,
        speed=1.0,
        episode_title_enhancer=enhance,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)

    qtbot.waitUntil(lambda: window.playlist_title_tabs.isHidden() is False, timeout=1000)
    qtbot.waitUntil(lambda: window.playlist.count() == 4 and window.playlist.item(1).text() == "第1集 星门初启", timeout=1000)

    assert window.current_index == 1
    assert window.playlist.currentRow() == 1
    assert window.session.playlist[window.current_index].original_title == "1-1080P.mp4"


def test_player_window_async_route_replacement_delays_episode_title_enhancement_until_playlist_ready(qtbot) -> None:
    ready = threading.Event()

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    replacement = [
        PlayItem(title="01(2.49 GB)", url="http://m/1.mp4", vod_id="ep1", play_source="百度"),
        PlayItem(title="02(2.51 GB)", url="http://m/2.mp4", vod_id="ep2", play_source="百度"),
    ]
    enhancement_inputs: list[list[str]] = []

    def load_item(item: PlayItem):
        assert item.title == "百度"
        assert ready.wait(timeout=1)
        return PlaybackLoadResult(replacement_playlist=replacement, replacement_start_index=0)

    def enhance(session: PlayerSession):
        titles = [item.title for item in session.playlist]
        enhancement_inputs.append(titles)
        if titles == ["01(2.49 GB)", "02(2.51 GB)"]:
            return [
                PlayItem(
                    title=session.playlist[0].title,
                    url=session.playlist[0].url,
                    vod_id=session.playlist[0].vod_id,
                    play_source=session.playlist[0].play_source,
                    original_title="01(2.49 GB)",
                    episode_display_title="第1集 超能路人甲",
                    episode_title_source="tmdb",
                ),
                PlayItem(
                    title=session.playlist[1].title,
                    url=session.playlist[1].url,
                    vod_id=session.playlist[1].vod_id,
                    play_source=session.playlist[1].play_source,
                    original_title="02(2.51 GB)",
                    episode_display_title="第2集 超能路人甲",
                    episode_title_source="tmdb",
                ),
            ]
        return None

    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="超能路人甲"),
        playlist=[PlayItem(title="百度", url="", vod_id="https://pan.baidu.com/s/abc", play_source="百度")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        playback_loader=load_item,
        async_playback_loader=True,
        episode_title_enhancer=enhance,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    ready.set()

    qtbot.waitUntil(lambda: window.playlist.count() == 2, timeout=1000)
    qtbot.waitUntil(lambda: window.playlist.item(0).text() == "第1集 超能路人甲", timeout=1000)

    assert enhancement_inputs == [["01(2.49 GB)", "02(2.51 GB)"]]


def test_player_window_ignores_stale_metadata_hydration_results(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    ready = threading.Event()
    session = PlayerSession(
        vod=VodItem(vod_id="v1", vod_name="原始标题", vod_content="原始简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/1.mp4", vod_id="ep1")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    def hydrate(current_session: PlayerSession) -> VodItem:
        assert ready.wait(timeout=1)
        return VodItem(vod_id=current_session.vod.vod_id, vod_name="旧结果", vod_content="旧简介")

    session.metadata_hydrator = hydrate
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    window.session = PlayerSession(
        vod=VodItem(vod_id="v2", vod_name="新会话", vod_content="新简介"),
        playlist=[PlayItem(title="第1集", url="https://media.example/2.mp4", vod_id="ep2")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    ready.set()

    qtbot.wait(100)
    assert "旧简介" not in window.metadata_view.toPlainText()


def test_player_window_resume_from_main_preserves_resume_offset_with_async_loader(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, bool, int, dict[str, str]]] = []
            self.pause_calls = 0
            self.resume_calls = 0

        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            self.load_calls.append((url, pause, start_seconds, headers or {}))

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 30

        def pause(self) -> None:
            self.pause_calls += 1

        def resume(self) -> None:
            self.resume_calls += 1

    controller = RecordingPlayerController()
    config = AppConfig(last_active_window="main", last_player_paused=True)
    window = PlayerWindow(controller, config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    video = FakeVideo()
    window.video = video
    session = make_player_session(start_index=0)
    session.playlist = [PlayItem(title="Episode 1", url="http://emby/1.mp4", vod_id="1-3458")]
    session.use_local_history = False
    session.playback_loader = lambda item: None
    session.async_playback_loader = True

    window.open_session(session)

    window._return_to_main()
    window.resume_from_main()

    assert video.pause_calls == 1
    assert video.resume_calls == 0
    assert video.load_calls == [
        ("http://emby/1.mp4", False, 0, {}),
        ("http://emby/1.mp4", False, 30, {}),
    ]
    assert window.is_playing is True
    assert config.last_player_paused is False


def test_player_window_does_not_report_zero_progress_while_async_loader_is_pending(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

    ready = threading.Event()

    def load_item(item: PlayItem) -> None:
        assert ready.wait(timeout=1)
        item.url = "http://emby/1.mp4"

    controller = RecordingPlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = FakeVideo()
    session = make_player_session(start_index=0)
    session.start_position_seconds = 42
    session.playlist = [PlayItem(title="Episode 1", url="", vod_id="1-3458")]
    session.playback_loader = load_item
    session.async_playback_loader = True

    window.open_session(session)
    window.report_progress()
    qtbot.wait(100)

    assert controller.progress_calls == []
    assert session.start_position_seconds == 42

    ready.set()


def test_player_window_return_to_main_keeps_restore_offset_while_async_loader_is_pending(qtbot) -> None:
    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0, headers: dict[str, str] | None = None) -> None:
            return None

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def position_seconds(self) -> int:
            return 0

        def pause(self) -> None:
            return None

    ready = threading.Event()

    def load_item(item: PlayItem) -> None:
        assert ready.wait(timeout=1)
        item.url = "http://emby/1.mp4"

    window = PlayerWindow(FakePlayerController(), config=AppConfig(last_active_window="player"), save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = FakeVideo()
    session = make_player_session(start_index=0)
    session.start_position_seconds = 42
    session.playlist = [PlayItem(title="Episode 1", url="", vod_id="1-3458")]
    session.playback_loader = load_item
    session.async_playback_loader = True

    window.open_session(session)
    window._return_to_main()

    assert session.start_position_seconds == 42

    ready.set()


def test_player_window_keeps_failed_async_parse_item_selected_and_parse_combo_enabled(qtbot) -> None:
    class FakeParserService:
        def parsers(self):
            return [type("Parser", (), {"key": "jx1", "label": "jx1"})()]

    ready = threading.Event()

    def load_item(item: PlayItem) -> None:
        assert ready.wait(timeout=1)
        item.parse_required = True
        raise RuntimeError("parse failed")

    window = PlayerWindow(FakePlayerController(), config=AppConfig(), playback_parser_service=FakeParserService())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = make_player_session(start_index=0)
    session.playlist = [
        PlayItem(title="Episode 1", url="http://m/1.m3u8"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]

    window.open_session(session)
    assert window.video.load_calls == [("http://m/1.m3u8", 0)]
    session.playback_loader = load_item
    session.async_playback_loader = True
    ready.set()

    window.play_next()

    qtbot.waitUntil(lambda: "播放失败: parse failed" in window.log_view.toPlainText())
    assert window.current_index == 1
    assert window.playlist.currentRow() == 1
    assert window.parse_combo.isEnabled() is True
    assert window.video.load_calls == [("http://m/1.m3u8", 0)]


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


def test_player_window_route_replacement_resets_danmaku_prefetch_state(qtbot) -> None:
    controller = PrefetchResetRecordingPlayerController()
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
        playlists=[[PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="quark")]],
        playlist_index=0,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        playback_loader=load_item,
    )

    window = PlayerWindow(controller, config=None, save_config=lambda: None)
    qtbot.addWidget(window)

    window.open_session(session)

    assert controller.reset_calls == [session]


def test_player_window_switching_playlist_item_resets_danmaku_prefetch_state(qtbot) -> None:
    controller = PrefetchResetRecordingPlayerController()
    playlist = [
        PlayItem(title="第1集", url="http://m/1.mp4", play_source="线路1"),
        PlayItem(title="第2集", url="http://m/2.mp4", play_source="线路1"),
    ]
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="雨霖铃"),
        playlist=playlist,
        playlists=[playlist],
        playlist_index=0,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )

    window = PlayerWindow(controller, config=None, save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()

    window.open_session(session)
    window._play_clicked_item(window.playlist.item(1))

    assert controller.reset_calls == [session]


def test_player_window_async_loader_plays_replacement_item_after_route_replacement(qtbot) -> None:
    ready = threading.Event()
    class StartedRecordingPlayerController(FakePlayerController):
        def __init__(self) -> None:
            self.started_calls: list[tuple[int, str]] = []

        def on_item_started(self, session, current_index: int) -> None:
            self.started_calls.append((current_index, session.playlist[current_index].title))

    controller = StartedRecordingPlayerController()
    replacement = [
        PlayItem(title="离线A1.mp4", url="http://resolved/offline-a1.mp4", vod_id="1@107920@0@0", play_source="磁力线"),
        PlayItem(title="第2集", url="", vod_id="magnet:?xt=urn:btih:bbbb6396e03acb19d72eb2d779a22b2dc00f66bb", play_source="磁力线"),
    ]

    def load_item(item: PlayItem):
        assert item.vod_id == "magnet:?xt=urn:btih:8a06396e03acb19d72eb2d779a22b2dc00f66a33"
        assert ready.wait(timeout=1)
        return PlaybackLoadResult(replacement_playlist=replacement, replacement_start_index=0)

    window = PlayerWindow(controller, config=None, save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="磁力片源"),
        playlist=[
            PlayItem(
                title="磁力1",
                url="",
                vod_id="magnet:?xt=urn:btih:8a06396e03acb19d72eb2d779a22b2dc00f66a33",
                play_source="磁力线",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        playback_loader=load_item,
        async_playback_loader=True,
    )

    window.open_session(session)

    assert window.video.load_calls == []
    assert "正在加载播放地址: 磁力1" in window.log_view.toPlainText()

    ready.set()

    qtbot.waitUntil(lambda: window.video.load_calls == [("http://resolved/offline-a1.mp4", 0)], timeout=1000)
    assert window.session is not None
    assert [item.title for item in window.session.playlist] == ["离线A1.mp4", "第2集"]
    assert window.current_index == 0
    assert controller.started_calls == [(0, "离线A1.mp4")]


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


def test_player_window_replacement_updates_only_active_leaf_source(qtbot) -> None:
    active = [PlayItem(title="查看", url="", vod_id="https://pan.quark.cn/s/demo", play_source="夸克2")]
    sibling = [PlayItem(title="第1集", url="http://q1/1.mp4", play_source="夸克1")]
    session = PlayerSession(
        vod=VodItem(vod_id="plugin-1", vod_name="网盘剧集"),
        playlist=active,
        playlists=[sibling, active],
        playlist_index=1,
        source_groups=[
            PlaybackSourceGroup(
                label="夸克",
                sources=[
                    PlaybackSource(label="夸克1", playlist=sibling),
                    PlaybackSource(label="夸克2", playlist=active),
                ],
            )
        ],
        source_group_index=0,
        source_index=1,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        playback_loader=lambda item: PlaybackLoadResult(
            replacement_playlist=[PlayItem(title="S1 - 1", url="http://m/1.mp4", play_source="夸克2")],
            replacement_start_index=0,
        ),
    )
    window = PlayerWindow(FakePlayerController(), config=None, save_config=lambda: None)
    qtbot.addWidget(window)

    window.open_session(session)

    assert window.session is not None
    assert [item.title for item in window.session.source_groups[0].sources[0].playlist] == ["第1集"]
    assert [item.title for item in window.session.source_groups[0].sources[1].playlist] == ["S1 - 1"]


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
    window.video = RecordingVideo()

    window.open_session(session)

    assert window.playlist_group_combo.count() == 2
    assert window.playlist_group_combo.itemText(0) == "播放源 1"
    assert window.playlist_group_combo.itemText(1) == "网盘线(夸克)"
    assert window.video.load_calls == []


def test_player_window_shows_bilibili_grouped_playlist_tree_when_enabled(qtbot) -> None:
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(bilibili_grouped_playlist_tree_enabled=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    window.open_session(make_bilibili_grouped_session())

    assert window.playlist.isHidden() is True
    assert window.bilibili_playlist_tree.isHidden() is False
    assert window.playlist_group_combo.isHidden() is True
    assert window.bilibili_playlist_tree.topLevelItemCount() == 3
    assert window.bilibili_playlist_tree.topLevelItem(0).text(0) == "BiliBili"
    assert window.bilibili_playlist_tree.topLevelItem(1).text(0) == "相关视频(2)"
    assert window.bilibili_playlist_tree.topLevelItem(2).text(0) == "UP主视频"


def test_player_window_keeps_plain_playlist_for_bilibili_when_tree_mode_disabled(qtbot) -> None:
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(bilibili_grouped_playlist_tree_enabled=False),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    window.open_session(make_bilibili_grouped_session())

    assert window.playlist.isHidden() is False
    assert window.bilibili_playlist_tree.isHidden() is True
    assert window.playlist_group_combo.isHidden() is False


def test_player_window_ignores_bilibili_tree_mode_for_non_bilibili_sessions(qtbot) -> None:
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(bilibili_grouped_playlist_tree_enabled=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    session = make_player_session(start_index=0)
    session.source_kind = "browse"
    window.open_session(session)

    assert window.playlist.isHidden() is False
    assert window.bilibili_playlist_tree.isHidden() is True


def test_player_window_bilibili_tree_single_click_does_not_play_leaf_item(qtbot) -> None:
    controller = RecordingPlayerController()
    window = PlayerWindow(
        controller,
        config=AppConfig(bilibili_grouped_playlist_tree_enabled=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    window.open_session(make_bilibili_grouped_session())
    related_leaf = window.bilibili_playlist_tree.topLevelItem(1).child(1)

    window._handle_bilibili_tree_item_clicked(related_leaf, 0)

    assert window.session is not None
    assert window.session.playlist[window.current_index].title == "正片"


def test_player_window_bilibili_tree_double_click_plays_leaf_item(qtbot) -> None:
    controller = RecordingPlayerController()
    window = PlayerWindow(
        controller,
        config=AppConfig(bilibili_grouped_playlist_tree_enabled=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    window.open_session(make_bilibili_grouped_session())
    related_leaf = window.bilibili_playlist_tree.topLevelItem(1).child(1)

    window._handle_bilibili_tree_item_activated(related_leaf, 0)

    assert window.session is not None
    assert window.session.playlist[window.current_index].title == "相关2"


def test_player_window_bilibili_tree_leaf_tooltip_matches_plain_playlist(qtbot) -> None:
    main_group = [
        PlayItem(
            title="正片",
            url="http://bili/main.mp4",
            vod_id="BV-main",
            play_source="BiliBili",
            original_title="Bili.Main.Full.Title.2160p.mkv",
            episode_display_title="第1集 正片",
        )
    ]
    related_group = [
        PlayItem(
            title="相关1",
            url="http://bili/r1.mp4",
            vod_id="BV-r1",
            play_source="相关视频",
            original_title="Related.Video.Full.Title.1080p.mp4",
            episode_display_title="相关视频 1",
        )
    ]
    session = PlayerSession(
        vod=VodItem(vod_id="BV-main", vod_name="B站视频"),
        playlist=main_group,
        playlists=[main_group, related_group],
        playlist_index=0,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        source_kind="bilibili",
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(bilibili_grouped_playlist_tree_enabled=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    window.open_session(session)
    main_leaf = window.bilibili_playlist_tree.topLevelItem(0).child(0)
    related_leaf = window.bilibili_playlist_tree.topLevelItem(1).child(0)

    assert main_leaf.toolTip(0) == "Bili.Main.Full.Title.2160p.mkv"
    assert related_leaf.toolTip(0) == "Related.Video.Full.Title.1080p.mp4"


def test_player_window_bilibili_tree_play_next_crosses_group_boundaries(qtbot) -> None:
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(bilibili_grouped_playlist_tree_enabled=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    session = make_bilibili_grouped_session()
    session.playlist = session.playlists[1]
    session.playlist_index = 1
    session.start_index = 1
    window.open_session(session)

    window.play_next()

    assert window.session is not None
    assert window.session.playlist[window.current_index].title == "UP主1"


def test_player_window_bilibili_tree_auto_advance_crosses_group_boundaries(qtbot) -> None:
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(bilibili_grouped_playlist_tree_enabled=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    session = make_bilibili_grouped_session()
    session.playlist = session.playlists[0]
    session.playlist_index = 0
    session.start_index = 0
    window.open_session(session)

    window._update_playback_observation(position=119, duration=120)
    window._handle_playback_finished()

    assert window.session is not None
    assert window.session.playlist[window.current_index].title == "相关1"


def test_player_window_bilibili_tree_rebuilds_flat_mapping_after_replacement(qtbot) -> None:
    main_group = [PlayItem(title="正片", url="", vod_id="BV-main", play_source="BiliBili")]
    related_group = [PlayItem(title="查看", url="", vod_id="BV-folder", play_source="相关视频")]
    session = PlayerSession(
        vod=VodItem(vod_id="BV-main", vod_name="B站视频"),
        playlist=related_group,
        playlists=[main_group, related_group],
        playlist_index=1,
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        source_kind="bilibili",
        playback_loader=lambda item: PlaybackLoadResult(
            replacement_playlist=[
                PlayItem(title="相关A", url="http://m/a.mp4", vod_id="BV-a", play_source="相关视频"),
                PlayItem(title="相关B", url="http://m/b.mp4", vod_id="BV-b", play_source="相关视频"),
            ],
            replacement_start_index=1,
        ),
    )
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(bilibili_grouped_playlist_tree_enabled=True),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)

    window.open_session(session)

    assert window.session is not None
    assert [item.title for item in window.session.playlists[1]] == ["相关A", "相关B"]
    assert window.session.playlist[window.current_index].title == "相关B"


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
            duration_seconds: int = 0,
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
                duration_seconds=duration_seconds,
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
    assert window.wide_button.toolTip() == "宽屏 (W)"
    assert window.danmaku_source_button.toolTip() == "弹幕源 (D)"
    assert window.fullscreen_button.toolTip() == "全屏 (Enter)"
    assert window.play_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert window.refresh_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert window.fullscreen_button.cursor().shape() == Qt.CursorShape.PointingHandCursor


def test_player_window_playlist_title_tabs_use_pointing_hand_cursor(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.playlist_title_tabs.cursor().shape() == Qt.CursorShape.PointingHandCursor


def test_player_window_adds_padding_around_bottom_controls(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    margins = window.bottom_layout.contentsMargins()

    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (12, 6, 12, 6)
    assert window.bottom_area.maximumHeight() == 88


def test_player_window_root_layout_has_no_gap_between_video_and_controls(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.layout() is not None
    assert window.layout().spacing() == 0


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


def test_player_window_mouse_move_outside_video_does_not_override_resize_cursor_while_playing(
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

    window.setCursor(Qt.CursorShape.SizeHorCursor)
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
    assert window.cursor().shape() in (
        Qt.CursorShape.SizeHorCursor,
        Qt.CursorShape.SizeVerCursor,
        Qt.CursorShape.SizeFDiagCursor,
        Qt.CursorShape.SizeBDiagCursor,
    )


def test_player_window_cursor_poll_outside_video_does_not_override_resize_cursor_while_playing(
    qtbot, monkeypatch
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    outside_local = window.rect().bottomRight()
    outside_global = window.mapToGlobal(outside_local)
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: outside_global))
    cursor_autohide_calls: list[int | None] = []
    window.video.set_cursor_autohide = lambda value: cursor_autohide_calls.append(value)
    window.is_playing = True
    window._last_cursor_pos = outside_global
    window._last_cursor_activity_ms = 1000
    window.setCursor(Qt.CursorShape.SizeFDiagCursor)

    window._poll_cursor_idle_state(now_ms=1500)

    assert window._video_pointer_inside is False
    assert cursor_autohide_calls[-1] == 2000
    assert window.cursor().shape() == Qt.CursorShape.SizeFDiagCursor


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


def test_player_window_starts_in_wide_mode_when_config_requests_it(qtbot) -> None:
    config = AppConfig(player_wide_mode=True)
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.show()

    assert window.wide_button.isChecked() is True
    assert window.sidebar_container.isHidden() is True
    assert window.main_splitter.sizes()[1] == 0


def test_player_window_toggling_wide_mode_updates_config_and_saves(qtbot) -> None:
    saved = {"count": 0}
    config = AppConfig(player_wide_mode=False)
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)
    window.show()

    window.wide_button.click()

    assert config.player_wide_mode is True
    assert saved["count"] >= 1

    window.wide_button.click()

    assert config.player_wide_mode is False
    assert saved["count"] >= 2


def test_player_window_keeps_danmaku_source_button_visible_in_wide_mode(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()

    window.wide_button.click()
    qtbot.wait(10)

    assert window.sidebar_container.isHidden() is True
    assert window.danmaku_source_button.isVisible() is True


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
    assert restored.wide_button.isChecked() is True
    assert restored.sidebar_container.isHidden() is True

    restored.wide_button.click()
    restored_sizes = restored.main_splitter.sizes()
    restored_ratio = restored_sizes[0] / sum(restored_sizes)

    assert restored.sidebar_container.isHidden() is False
    assert restored_sizes[1] > 0
    # Lower minimum window size (800x600) lets the content pane's min-width clamp
    # shift the restored splitter proportion further from the saved ratio, so a
    # wider round-trip tolerance is needed than at the old 1000x700 minimum.
    assert abs(restored_ratio - expected_ratio) < 0.08


def test_player_window_restores_sidebar_after_toggling_wide_mode_from_fullscreen(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.show()
    window.main_splitter.setSizes([900, 300])
    expected_sizes = window.main_splitter.sizes()

    window.toggle_fullscreen()
    assert window.isFullScreen() is True

    # Some platforms collapse the hidden sidebar pane to zero width in fullscreen.
    window.main_splitter.setSizes([sum(expected_sizes), 0])

    window.wide_button.click()
    assert window.wide_button.isChecked() is True

    window.toggle_fullscreen()
    assert window.isFullScreen() is False
    assert window.sidebar_container.isHidden() is True

    window.wide_button.click()

    assert window.wide_button.isChecked() is False
    assert window.sidebar_container.isHidden() is False
    assert window.main_splitter.sizes()[1] > 0
    assert window.main_splitter.sizes() == expected_sizes


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


def test_player_window_restores_geometry_after_building_main_layout(qtbot, monkeypatch) -> None:
    calls: list[tuple[bool, bool, bytes]] = []

    def fake_restore_geometry(self, geometry) -> bool:
        calls.append((self.layout() is not None, hasattr(self, "main_splitter"), bytes(geometry.data())))
        return True

    monkeypatch.setattr(PlayerWindow, "restoreGeometry", fake_restore_geometry)
    config = AppConfig(player_window_geometry=b"saved-player-geometry")

    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)

    assert calls == [(True, True, b"saved-player-geometry")]


def test_player_window_persists_playback_log_visibility(qtbot) -> None:
    saved = {"count": 0}
    config = AppConfig()
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)
    window.show()

    window.toggle_log_button.click()

    assert config.player_log_visible is False
    assert saved["count"] >= 1

    restored = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(restored)

    assert restored.toggle_log_button.isChecked() is False
    assert restored.log_section.isHidden() is True


def test_player_window_mirrors_playback_logs_into_app_log_service(qtbot) -> None:
    class RecordingLogService:
        def __init__(self) -> None:
            self.events: list[object] = []

        def write_event(self, event) -> None:
            self.events.append(event)

    service = RecordingLogService()
    window = PlayerWindow(FakePlayerController(), config=AppConfig(), app_log_service=service)
    qtbot.addWidget(window)
    window.session = make_player_session(start_index=0)
    window.current_index = 0

    window._append_log("缓冲完成", level="INFO", category="playback")

    assert len(service.events) == 1
    event = service.events[0]
    assert event.source == "player"
    assert event.category == "playback"
    assert event.level == "INFO"
    assert event.message == "缓冲完成"
    assert event.vod_id == "movie-1"
    assert event.vod_name == "Movie"
    assert event.episode_title == "Episode 1"


def test_player_window_skips_detailed_log_accumulation_when_logging_disabled(qtbot) -> None:
    class RecordingLogService:
        def __init__(self) -> None:
            self.events: list[object] = []

        def write_event(self, event) -> None:
            self.events.append(event)

    service = RecordingLogService()
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(logging_enabled=False),
        app_log_service=service,
    )
    qtbot.addWidget(window)

    window._append_log("播放地址: https://media.example/1.m3u8", level="INFO", category="playback")

    assert window.log_view.toPlainText() == ""
    assert service.events == []

    window._append_log("播放失败: boom", level="ERROR", category="playback")

    assert "播放失败: boom" in window.log_view.toPlainText()
    assert service.events == []


def test_player_window_return_to_main_hides_window_and_keeps_video_backend_warm(qtbot, monkeypatch) -> None:
    emitted = {"count": 0}
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    pauses = {"count": 0}
    shutdowns = {"count": 0}
    stop_media_calls = {"count": 0}

    class FakeVideo:
        def pause(self) -> None:
            pauses["count"] += 1

    window.session = make_player_session(start_index=0)
    window.video = FakeVideo()
    monkeypatch.setattr(window.video_widget, "shutdown", lambda: shutdowns.__setitem__("count", shutdowns["count"] + 1))
    monkeypatch.setattr(
        window.video_widget,
        "stop_media",
        lambda: stop_media_calls.__setitem__("count", stop_media_calls["count"] + 1),
    )
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))
    window.show()
    window._return_to_main()

    assert emitted["count"] == 1
    assert window.isHidden() is True
    assert window.session is not None
    assert config.last_active_window == "main"
    assert pauses["count"] == 1
    assert stop_media_calls["count"] == 1
    assert shutdowns["count"] == 0


def test_player_window_return_to_main_uses_video_shutdown_on_windows(qtbot, monkeypatch) -> None:
    emitted = {"count": 0}
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    pauses = {"count": 0}
    shutdowns = {"count": 0}

    class FakeVideo:
        def pause(self) -> None:
            pauses["count"] += 1

    window.session = make_player_session(start_index=0)
    window.video = FakeVideo()
    monkeypatch.setattr(player_window_module.sys, "platform", "win32")
    monkeypatch.setattr(
        window.video_widget,
        "shutdown",
        lambda: shutdowns.__setitem__("count", shutdowns["count"] + 1),
    )
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))
    window.show()

    window._return_to_main()

    assert emitted["count"] == 1
    assert window.isHidden() is True
    assert pauses["count"] == 1
    assert shutdowns["count"] == 1


def test_player_window_return_to_main_shuts_down_video_backend_for_gpu_render_profiles(qtbot, monkeypatch) -> None:
    emitted = {"count": 0}
    config = AppConfig(last_active_window="player", mpv_render_profile="quality")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    pauses = {"count": 0}
    shutdowns = {"count": 0}

    class FakeVideo:
        def pause(self) -> None:
            pauses["count"] += 1

    window.session = make_player_session(start_index=0)
    window.video = FakeVideo()
    monkeypatch.setattr(
        window.video_widget,
        "shutdown",
        lambda: shutdowns.__setitem__("count", shutdowns["count"] + 1),
    )
    monkeypatch.setattr(player_window_module.sys, "platform", "linux")
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))
    window.show()

    window._return_to_main()

    assert emitted["count"] == 1
    assert window.isHidden() is True
    assert pauses["count"] == 1
    assert shutdowns["count"] == 1


def test_player_window_context_menu_exit_playback_returns_to_main(qtbot, monkeypatch) -> None:
    quit_calls = {"count": 0}
    emitted = {"count": 0}
    shutdowns = {"count": 0}
    stop_media_calls = {"count": 0}
    controller = RecordingPlayerController()
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(controller, config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1))
    controller.progress_calls.clear()
    controller.stop_calls.clear()
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))

    monkeypatch.setattr(
        window.video_widget,
        "shutdown",
        lambda: shutdowns.__setitem__("count", shutdowns["count"] + 1),
    )
    monkeypatch.setattr(
        window.video_widget,
        "stop_media",
        lambda: stop_media_calls.__setitem__("count", stop_media_calls["count"] + 1),
    )
    monkeypatch.setattr(
        QApplication,
        "quit",
        lambda *args, **kwargs: quit_calls.__setitem__("count", quit_calls["count"] + 1),
    )

    menu = window._build_video_context_menu()
    exit_action = next(action for action in menu.actions() if action.text() == "退出播放")

    window.show()
    exit_action.trigger()

    assert quit_calls["count"] == 0
    assert emitted["count"] == 1
    assert window.isHidden() is True
    assert config.last_active_window == "main"
    assert stop_media_calls["count"] == 1
    assert shutdowns["count"] == 0
    assert window.video.pause_calls == 1
    qtbot.waitUntil(
        lambda: controller.progress_calls == [(1, 30, 1.0, 0, 0, True)] and controller.stop_calls == [1]
    )


def test_player_window_reconfigures_danmaku_after_matching_video_file_loaded(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig(), save_config=lambda: None)
    qtbot.addWidget(window)
    window.session = make_player_session(start_index=0)
    window.current_index = 0
    current_item = window.session.playlist[0]

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        window.video_widget,
        "load",
        lambda url, pause=False, start_seconds=0, **_kwargs: calls.append(("load", url)),
    )
    monkeypatch.setattr(window.video_widget, "set_speed", lambda value: None)
    monkeypatch.setattr(window.video_widget, "set_volume", lambda value: None)
    monkeypatch.setattr(window.video_widget, "set_muted", lambda value: None)
    monkeypatch.setattr(
        window,
        "_configure_danmaku_for_current_item",
        lambda: calls.append(("danmaku", window.session.playlist[window.current_index])),
    )

    window._start_current_item_playback()

    assert calls == [("load", current_item.url), ("danmaku", current_item)]
    assert window._pending_file_loaded_danmaku_item is current_item

    window._handle_video_file_loaded()

    assert calls == [
        ("load", current_item.url),
        ("danmaku", current_item),
        ("danmaku", current_item),
    ]
    assert window._pending_file_loaded_danmaku_item is None


def test_player_window_ignores_stale_file_loaded_danmaku_item(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig(), save_config=lambda: None)
    qtbot.addWidget(window)
    window.session = make_player_session(start_index=0)
    window.current_index = 0
    original_item = window.session.playlist[0]

    configure_items: list[PlayItem] = []
    monkeypatch.setattr(window.video_widget, "load", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(window.video_widget, "set_speed", lambda value: None)
    monkeypatch.setattr(window.video_widget, "set_volume", lambda value: None)
    monkeypatch.setattr(window.video_widget, "set_muted", lambda value: None)
    monkeypatch.setattr(
        window,
        "_configure_danmaku_for_current_item",
        lambda: configure_items.append(window.session.playlist[window.current_index]),
    )

    window._start_current_item_playback()
    window.current_index = 1
    window._handle_video_file_loaded()

    assert configure_items == [original_item]
    assert window._pending_file_loaded_danmaku_item is None


def test_player_window_clears_pending_file_loaded_danmaku_item_when_video_load_fails(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig(), save_config=lambda: None)
    qtbot.addWidget(window)
    window.session = make_player_session(start_index=0)
    window.current_index = 0

    def fail_load(*_args, **_kwargs) -> None:
        raise RuntimeError("load failed")

    monkeypatch.setattr(window, "_video_load", fail_load)

    with pytest.raises(RuntimeError, match="load failed"):
        window._start_current_item_playback()

    assert window._pending_file_loaded_danmaku_item is None


def test_player_window_applies_post_load_configuration_immediately_on_windows(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig(), save_config=lambda: None)
    qtbot.addWidget(window)
    monkeypatch.setattr(player_window_module.sys, "platform", "win32")
    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")
    window.session = make_player_session(start_index=0)
    window.current_index = 0

    calls: list[tuple[str, object | None]] = []
    monkeypatch.setattr(
        window.video_widget,
        "load",
        lambda url, pause=False, start_seconds=0, **_kwargs: calls.append(("load", url)),
    )
    monkeypatch.setattr(window.video_widget, "set_speed", lambda value: calls.append(("speed", value)))
    monkeypatch.setattr(window.video_widget, "set_volume", lambda value: calls.append(("volume", value)))
    monkeypatch.setattr(window.video_widget, "set_muted", lambda value: calls.append(("mute", value)))
    monkeypatch.setattr(window, "_configure_danmaku_for_current_item", lambda: calls.append(("danmaku", None)))

    window._start_current_item_playback()

    assert calls == [
        ("load", "http://m/1.m3u8"),
        ("speed", 1.0),
        ("volume", 100),
        ("mute", False),
        ("danmaku", None),
    ]


def test_player_window_deferred_file_loaded_callback_does_not_run_after_window_is_deleted(qtbot, monkeypatch) -> None:
    destroyed = {"count": 0}
    window = PlayerWindow(FakePlayerController(), config=AppConfig(), save_config=lambda: None)
    monkeypatch.setattr(player_window_module.sys, "platform", "win32")
    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")
    window.destroyed.connect(lambda *_args: destroyed.__setitem__("count", destroyed["count"] + 1))
    window.session = make_player_session(start_index=0)
    window.current_index = 0
    window._pending_post_load_item = window.session.playlist[0]
    window._pending_post_load_pause = True

    window._handle_video_file_loaded()
    window.deleteLater()
    qtbot.waitUntil(lambda: destroyed["count"] == 1, timeout=1000)
    qtbot.wait(50)

    assert destroyed["count"] == 1


def test_player_window_applies_windows_paused_start_immediately(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig(), save_config=lambda: None)
    qtbot.addWidget(window)
    monkeypatch.setattr(player_window_module.sys, "platform", "win32")
    monkeypatch.setattr("atv_player.player.mpv_widget.sys.platform", "win32")
    window.session = make_player_session(start_index=0)
    window.current_index = 0

    calls: list[tuple[str, object | None]] = []
    monkeypatch.setattr(
        window.video_widget,
        "load",
        lambda url, pause=False, start_seconds=0, **_kwargs: calls.append(("load", url, pause)),
    )
    monkeypatch.setattr(window.video_widget, "set_speed", lambda value: calls.append(("speed", value)))
    monkeypatch.setattr(window.video_widget, "set_volume", lambda value: calls.append(("volume", value)))
    monkeypatch.setattr(window.video_widget, "set_muted", lambda value: calls.append(("mute", value)))
    monkeypatch.setattr(window, "_configure_danmaku_for_current_item", lambda: calls.append(("danmaku", None)))

    window._start_current_item_playback(pause=True)

    assert calls == [
        ("load", "http://m/1.m3u8", True),
        ("speed", 1.0),
        ("volume", 100),
        ("mute", False),
        ("danmaku", None),
    ]


def test_player_window_followup_subtitle_refresh_does_not_run_after_window_is_deleted(qtbot, tmp_path) -> None:
    destroyed = {"count": 0}
    subtitle_path = tmp_path / "song.ass"
    subtitle_path.write_text("dummy", encoding="utf-8")

    class FakeVideo:
        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            return None

        def set_speed(self, speed: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def set_muted(self, value: bool) -> None:
            return None

        def subtitle_tracks(self) -> list[SubtitleTrack]:
            return []

        def current_subtitle_track_id(self) -> int | None:
            return None

        def load_external_subtitle(self, path: str, *, select_for_secondary: bool = False) -> int | None:
            return 1

        def remove_subtitle_track(self, track_id: int | None) -> None:
            return None

        def apply_subtitle_mode(self, mode: str, track_id: int | None = None) -> int | None:
            return track_id

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="Episode 1",
                url="http://m/1.m3u8",
                external_subtitles=[
                    ExternalSubtitleOption(
                        name="外挂字幕",
                        lang="zh",
                        url=str(subtitle_path),
                        format="text/x-ass",
                    )
                ],
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    window.destroyed.connect(lambda *_args: destroyed.__setitem__("count", destroyed["count"] + 1))
    window.video = FakeVideo()
    window.session = session
    window.current_index = 0

    window._schedule_followup_subtitle_refresh_if_needed(session.playlist[0])
    window.deleteLater()
    qtbot.waitUntil(lambda: destroyed["count"] == 1, timeout=1000)
    qtbot.wait(200)

    assert destroyed["count"] == 1


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
            duration_seconds: int = 0,
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
                duration_seconds=duration_seconds,
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


def test_player_window_close_during_app_quit_preserves_player_restore_state(qtbot) -> None:
    saved = {"count": 0}
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)
    window.open_session(make_player_session())
    window.show()

    app = QApplication.instance()
    assert app is not None
    app.aboutToQuit.emit()
    window.close()

    assert config.last_active_window == "player"
    assert saved["count"] >= 1


def test_player_window_close_event_returns_to_main_instead_of_exiting(qtbot) -> None:
    saved = {"count": 0}
    emitted = {"count": 0}
    config = AppConfig(last_active_window="player", last_player_paused=False)
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1), start_paused=False)
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))
    window.show()

    window.close()

    assert window.isHidden() is True
    assert window.session is not None
    assert config.last_active_window == "main"
    assert config.last_player_paused is True
    assert emitted["count"] == 1
    assert saved["count"] >= 1

    window._quit_requested = True
    window.close()


def test_player_window_title_bar_return_button_returns_to_main(qtbot) -> None:
    saved = {"count": 0}
    emitted = {"count": 0}
    config = AppConfig(last_active_window="player", last_player_paused=False)
    window = PlayerWindow(
        FakePlayerController(),
        config=config,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    qtbot.addWidget(window)
    pauses = {"count": 0}

    class FakeVideo:
        def pause(self) -> None:
            pauses["count"] += 1

    window.session = make_player_session(start_index=0)
    window.video = FakeVideo()
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))

    window.title_bar_return_button.click()

    assert emitted["count"] == 1
    assert pauses["count"] == 1
    assert config.last_active_window == "main"
    assert saved["count"] >= 1


def test_player_window_title_bar_close_button_quits_application(qtbot, monkeypatch) -> None:
    quit_calls = {"count": 0}
    emitted = {"count": 0}
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))

    monkeypatch.setattr(
        QApplication,
        "quit",
        lambda *args, **kwargs: quit_calls.__setitem__("count", quit_calls["count"] + 1),
    )

    window.title_bar().close_button.click()

    assert quit_calls["count"] == 1
    assert emitted["count"] == 0
    assert config.last_active_window == "player"


def test_player_window_shows_title_bar_maximize_button(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.title_bar().maximize_button.isHidden() is False


def visible_shortcut_help_dialogs() -> list[QDialog]:
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, QDialog)
        and widget.windowTitle() == "帮助"
        and widget.isVisible()
    ]


def visible_danmaku_source_dialogs() -> list[QDialog]:
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, QDialog)
        and widget.windowTitle() == "弹幕源"
        and widget.isVisible()
    ]


def visible_danmaku_settings_dialogs() -> list[QDialog]:
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, QDialog)
        and widget.windowTitle() == "弹幕设置"
        and widget.isVisible()
    ]


def visible_metadata_scrape_dialogs() -> list[QDialog]:
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, QDialog)
        and widget.windowTitle() == "刮削"
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

    assert ("F1", "打开帮助") in rows
    assert ("Space", "播放/暂停") in rows
    assert ("Left", "后退 15 秒") in rows
    assert ("Ctrl+Right", "前进 60 秒") in rows
    assert ("M", "静音") in rows
    assert ("Enter", "切换全屏") in rows
    assert ("W", "切换宽屏") in rows
    assert ("D", "打开弹幕源") in rows
    assert ("S", "打开刮削") in rows
    assert ("Ctrl+D", "打开弹幕设置") in rows
    assert ("I", "显示视频信息") in rows


def test_player_window_s_shortcut_opens_metadata_scrape_dialog(qtbot) -> None:
    session = make_player_session()
    session.metadata_scrape_service = object()
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(session)
    window.show()
    window.activateWindow()
    window.setFocus()

    shortcuts = [
        shortcut
        for shortcut in window._shortcut_bindings
        if shortcut.key().toString(QKeySequence.SequenceFormat.PortableText) == "S"
    ]

    assert len(shortcuts) == 1

    shortcuts[0].activated.emit()

    qtbot.waitUntil(lambda: len(visible_metadata_scrape_dialogs()) == 1)


def test_player_window_ctrl_d_shortcut_opens_danmaku_settings_dialog(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session())
    window.show()
    window.activateWindow()
    window.setFocus()

    shortcuts = [
        shortcut
        for shortcut in window._shortcut_bindings
        if shortcut.key().toString(QKeySequence.SequenceFormat.PortableText) == "Ctrl+D"
    ]

    assert len(shortcuts) == 1

    shortcuts[0].activated.emit()

    qtbot.waitUntil(lambda: len(visible_danmaku_settings_dialogs()) == 1)


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

    send_key(window, Qt.Key.Key_W, text="w")
    assert window.wide_button.isChecked() is True

    send_key(window, Qt.Key.Key_W, text="w")
    assert window.wide_button.isChecked() is False

    send_key(window, Qt.Key.Key_D, text="d")
    qtbot.waitUntil(lambda: len(visible_danmaku_source_dialogs()) == 1)

    send_key(window, Qt.Key.Key_I, text="i")
    assert video.toggle_video_info_calls == 1

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


def test_player_window_playback_topmost_releases_on_pause_and_restores_on_resume(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    native_calls: list[bool] = []
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )
    window._set_always_on_top(True)
    native_calls.clear()

    window.toggle_playback()

    assert window.is_playing is False
    assert native_calls == [False]
    assert window._is_always_on_top() is True
    assert window._always_on_top_applied is False
    assert window.always_on_top_button.isChecked() is True

    window.toggle_playback()

    assert window.is_playing is True
    assert native_calls == [False, True]
    assert window._is_always_on_top() is True
    assert window._always_on_top_applied is True


def test_player_window_disabling_playback_topmost_while_paused_blocks_restore(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    native_calls: list[bool] = []
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )
    window._set_always_on_top(True)
    window.toggle_playback()
    native_calls.clear()

    window.always_on_top_button.click()
    window.toggle_playback()

    assert window.is_playing is True
    assert native_calls == []
    assert window._is_always_on_top() is False
    assert window._always_on_top_applied is False


@pytest.mark.parametrize("state", ["normal", "maximized", "fullscreen"])
def test_player_window_playback_topmost_pause_resume_preserves_window_state(
    qtbot,
    monkeypatch,
    state: str,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.show()
    if state == "maximized":
        window.showMaximized()
    elif state == "fullscreen":
        window.showFullScreen()
    qtbot.wait(30)
    native_calls: list[bool] = []
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )
    window._set_always_on_top(True)
    native_calls.clear()
    before = (
        window.isVisible(),
        window.isMinimized(),
        window.isMaximized(),
        window.isFullScreen(),
    )

    window.toggle_playback()
    window.toggle_playback()

    assert native_calls == [False, True]
    assert (
        window.isVisible(),
        window.isMinimized(),
        window.isMaximized(),
        window.isFullScreen(),
    ) == before


@pytest.mark.parametrize("transition", ["pause", "resume"])
def test_player_window_playback_continues_when_topmost_sync_fails(
    qtbot,
    monkeypatch,
    transition: str,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    messages: list[str] = []
    monkeypatch.setattr(window, "_set_native_always_on_top", lambda _enabled: None)
    window._set_always_on_top(True)
    if transition == "resume":
        window.toggle_playback()

    def fail_native_state(_enabled: bool) -> None:
        raise RuntimeError("unsupported")

    monkeypatch.setattr(window, "_set_native_always_on_top", fail_native_state)
    monkeypatch.setattr(window, "_append_log", messages.append)

    window.toggle_playback()

    assert window.is_playing is (transition == "resume")
    assert window._is_always_on_top() is True
    assert window.always_on_top_button.isChecked() is True
    assert messages == ["播放时置顶同步失败: unsupported"]


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


def test_player_window_opening_paused_session_releases_playback_topmost(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    native_calls: list[bool] = []
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )
    window._set_always_on_top(True)
    native_calls.clear()

    window.open_session(make_player_session(start_index=0), start_paused=True)

    assert window.is_playing is False
    assert native_calls == [False]
    assert window._is_always_on_top() is True
    assert window._always_on_top_applied is False


def test_player_window_replay_restores_playback_topmost(qtbot, monkeypatch) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=0))
    native_calls: list[bool] = []
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )
    window._set_always_on_top(True)
    window.toggle_playback()
    native_calls.clear()

    window._replay_current_item()

    assert window.is_playing is True
    assert native_calls == [True]
    assert window._always_on_top_applied is True


def test_player_window_play_next_updates_window_title_to_new_item(qtbot) -> None:
    controller = RecordingPlayerController()
    window = PlayerWindow(controller)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=0))

    window.play_next()

    assert window.current_index == 1
    assert window.windowTitle() == "Movie - Episode 2"


def test_player_window_uses_youtube_channel_detail_for_active_title(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    video_title = "Survive 30 Days On An Island With Your Ex, Win $250,000"
    session = PlayerSession(
        vod=VodItem(
            vod_id="yt:channel:UCX6OQ3DkcsbYNE6H8uQQuVA",
            vod_name=video_title,
            detail_fields=[PlaybackDetailField("频道", "MrBeast 野兽先生")],
        ),
        playlist=[
            PlayItem(
                title=video_title,
                url="https://www.youtube.com/watch?v=test123",
                original_url="https://www.youtube.com/watch?v=test123",
                selected_playback_quality_id="ytdlp_1080",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        initial_vod_name="UCX6OQ3DkcsbYNE6H8uQQuVA",
    )
    window.session = session
    window.current_index = 0

    assert window._active_playback_title() == (
        "MrBeast 野兽先生 - Survive 30 Days On An Island With Your Ex, Win $250,000"
    )


def test_player_window_treats_bare_youtube_id_as_placeholder_active_title(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    session = PlayerSession(
        vod=VodItem(vod_id="abc123xyz89", vod_name="Resolved YouTube Video"),
        playlist=[
            PlayItem(
                title="Resolved YouTube Video",
                url="https://www.youtube.com/watch?v=abc123xyz89",
                original_url="abc123xyz89",
                selected_playback_quality_id="ytdlp_1080",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        initial_vod_name="abc123xyz89",
    )
    window.session = session
    window.current_index = 0

    assert window._active_media_title(session.playlist[0]) == "Resolved YouTube Video"


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
    window.session = make_player_session(start_index=0)
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))
    window.show()
    window.activateWindow()
    window.setFocus()

    send_key(window, Qt.Key.Key_Escape)

    assert window.isHidden() is True
    assert emitted["count"] == 1
    assert pauses["count"] == 1
    assert config.last_active_window == "main"


def test_player_window_escape_closes_danmaku_source_dialog_without_returning_to_main(qtbot) -> None:
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    pauses = {"count": 0}

    class FakeVideo:
        def pause(self) -> None:
            pauses["count"] += 1

        def position_seconds(self) -> int:
            return 0

        def duration_seconds(self) -> int:
            return 120

    window.video = FakeVideo()
    window.open_session(make_player_session())
    window.show()
    window.activateWindow()
    window.setFocus()

    window._open_danmaku_source_dialog()
    qtbot.waitUntil(lambda: len(visible_danmaku_source_dialogs()) == 1)
    dialog = visible_danmaku_source_dialogs()[0]

    window.escape_shortcut.activated.emit()

    qtbot.waitUntil(lambda: not dialog.isVisible())
    assert window.isVisible() is True
    assert pauses["count"] == 0
    assert config.last_active_window == "player"


def test_player_window_escape_closes_danmaku_settings_dialog_without_returning_to_main(qtbot) -> None:
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    pauses = {"count": 0}

    class FakeVideo:
        def pause(self) -> None:
            pauses["count"] += 1

        def position_seconds(self) -> int:
            return 0

        def duration_seconds(self) -> int:
            return 120

    window.video = FakeVideo()
    window.open_session(make_player_session())
    window.show()
    window.activateWindow()
    window.setFocus()

    window._open_danmaku_settings_dialog()
    qtbot.waitUntil(lambda: len(visible_danmaku_settings_dialogs()) == 1)
    dialog = visible_danmaku_settings_dialogs()[0]

    window.escape_shortcut.activated.emit()

    qtbot.waitUntil(lambda: not dialog.isVisible())
    assert window.isVisible() is True
    assert pauses["count"] == 0
    assert config.last_active_window == "player"


def test_player_window_escape_closes_metadata_scrape_dialog_without_returning_to_main(qtbot) -> None:
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    pauses = {"count": 0}
    emitted = {"count": 0}

    class FakeVideo:
        def pause(self) -> None:
            pauses["count"] += 1

        def position_seconds(self) -> int:
            return 0

        def duration_seconds(self) -> int:
            return 120

    session = make_player_session()
    session.metadata_scrape_service = object()
    window.video = FakeVideo()
    window.open_session(session)
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))
    window.show()
    window.activateWindow()
    window.setFocus()

    window._open_metadata_scrape_dialog()
    qtbot.waitUntil(lambda: len(visible_metadata_scrape_dialogs()) == 1)
    dialog = visible_metadata_scrape_dialogs()[0]

    window.escape_shortcut.activated.emit()

    qtbot.waitUntil(lambda: not dialog.isVisible())
    assert window.isVisible() is True
    assert pauses["count"] == 0
    assert emitted["count"] == 0
    assert config.last_active_window == "player"


def test_player_window_return_to_main_persists_paused_restore_state(qtbot) -> None:
    config = AppConfig(last_active_window="player", last_player_paused=False)
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)

    class FakeVideo:
        def pause(self) -> None:
            return None

    window.session = make_player_session(start_index=0)
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


def test_player_window_playback_topmost_releases_on_return_and_restores_from_main(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(last_active_window="player"),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=0))
    native_calls: list[bool] = []
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )
    window._set_always_on_top(True)
    native_calls.clear()

    window._return_to_main()

    assert native_calls == [False]
    assert window.is_playing is False
    assert window._is_always_on_top() is True
    assert window._always_on_top_applied is False

    window.resume_from_main()

    assert native_calls == [False, True]
    assert window.is_playing is True
    assert window._always_on_top_applied is True


def test_player_window_failed_resume_from_main_does_not_restore_topmost(
    qtbot,
    monkeypatch,
) -> None:
    window = PlayerWindow(
        FakePlayerController(),
        config=AppConfig(last_active_window="player"),
        save_config=lambda: None,
    )
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=0))
    native_calls: list[bool] = []
    monkeypatch.setattr(
        window,
        "_set_native_always_on_top",
        lambda enabled: native_calls.append(enabled),
    )
    window._set_always_on_top(True)
    window._return_to_main()
    native_calls.clear()
    monkeypatch.setattr(
        window,
        "_play_item_at_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("load failed")),
    )

    window.resume_from_main()

    assert window.is_playing is False
    assert native_calls == []
    assert window._is_always_on_top() is True
    assert window._always_on_top_applied is False


def test_player_window_resume_from_main_preserves_scraped_metadata_over_cached_resolved_detail(qtbot) -> None:
    class CachedResolvedDetailController(FakePlayerController):
        def resolve_play_item_detail(self, session, play_item):
            resolved_vod = session.resolved_vod_by_id.get(play_item.vod_id)
            if resolved_vod is None:
                return None
            play_item.url = resolved_vod.items[0].url if resolved_vod.items else resolved_vod.vod_play_url
            return resolved_vod

    class TitleReplacingMetadataScrapeService(FakeMetadataScrapeService):
        def apply(self, vod: VodItem, candidate: MetadataScrapeCandidate) -> VodItem:
            self.apply_calls.append((vod.vod_name, candidate.provider_id))
            return VodItem(
                vod_id=vod.vod_id,
                vod_name="刮削后的标题",
                vod_year="2026",
                vod_content="刮削后的简介",
                detail_fields=[PlaybackDetailField(label="TMDB ID", value="1")],
            )

    config = AppConfig(last_active_window="main", last_player_paused=True)
    window = PlayerWindow(CachedResolvedDetailController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    service = TitleReplacingMetadataScrapeService()
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="原始标题", vod_content="原始简介"),
        playlist=[PlayItem(title="Episode 1", url="http://m/1.m3u8", vod_id="ep-1")],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        metadata_scrape_service=service,
        resolved_vod_by_id={
            "ep-1": VodItem(
                vod_id="ep-1",
                vod_name="原始标题",
                vod_content="原始简介",
                items=[PlayItem(title="Episode 1", url="http://m/1.m3u8", vod_id="ep-1")],
            )
        },
    )
    window.open_session(session)
    window._open_metadata_scrape_dialog()
    window._rerun_metadata_scrape_search()
    qtbot.waitUntil(lambda: window._metadata_scrape_result_list.count() == 1, timeout=1000)

    window._apply_selected_metadata_scrape_result()

    qtbot.waitUntil(lambda: "刮削后的标题" in window.metadata_view.toPlainText(), timeout=1000)

    window._return_to_main()
    window.resume_from_main()

    assert "刮削后的标题" in window.metadata_view.toPlainText()
    assert "刮削后的简介" in window.metadata_view.toPlainText()


def test_player_window_resume_from_main_reloads_active_danmaku(qtbot) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.load_calls: list[tuple[str, int]] = []
            self.loaded_danmaku_paths: list[str] = []
            self.removed_danmaku_track_ids: list[int] = []
            self._next_track_id = 70

        def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
            self.load_calls.append((url, start_seconds))

        def set_speed(self, value: float) -> None:
            return None

        def set_volume(self, value: int) -> None:
            return None

        def pause(self) -> None:
            return None

        def position_seconds(self) -> int:
            return 30

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

    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="Movie"),
        playlist=[
            PlayItem(
                title="Episode 1",
                url="http://m/1.m3u8",
                danmaku_xml='<?xml version="1.0" encoding="UTF-8"?><i><d p="0.0,1,25,16777215">第一条</d></i>',
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    config = AppConfig(last_active_window="main", last_player_paused=True)
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = FakeVideo()

    window.open_session(session)
    initial_count = len(window.video.loaded_danmaku_paths)

    window._return_to_main()
    window.resume_from_main()

    assert len(window.video.loaded_danmaku_paths) == initial_count + 1
    assert window.video.removed_danmaku_track_ids == [70]
    assert window.video.load_calls == [("http://m/1.m3u8", 0), ("http://m/1.m3u8", 30)]


def test_player_window_close_during_quit_clears_session_for_future_restore(qtbot) -> None:
    window = PlayerWindow(FakePlayerController(), config=AppConfig(last_active_window="player"), save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=1))
    window._quit_requested = True

    window.close()

    assert window.session is None


def test_clickable_slider_buffer_value_clamps_to_range(qtbot) -> None:
    slider = ClickableSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setMinimum(0)
    slider.setMaximum(100)
    slider.setValue(30)

    slider.set_buffer_value(70)
    assert slider._buffer_value == 70

    slider.set_buffer_value(250)
    assert slider._buffer_value == 100

    slider.set_buffer_value(-5)
    assert slider._buffer_value == 0


def test_clickable_slider_paints_without_error_with_buffer(qtbot) -> None:
    slider = ClickableSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setMinimum(0)
    slider.setMaximum(100)
    slider.setValue(30)
    slider.set_buffer_value(70)
    slider.resize(200, 24)

    pixmap = slider.grab()

    assert not pixmap.isNull()


def test_sync_progress_slider_sets_buffer_value_from_cache(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.progress_timer.stop()
    session = make_player_session(start_index=0)
    session.vod = VodItem(vod_id="movie-1", vod_name="Movie")
    window.open_session(session)
    window.video = type(
        "Video",
        (),
        {
            "position_seconds": lambda _self: 30,
            "duration_seconds": lambda _self: 120,
            "demuxer_cache_duration_seconds": lambda _self: 40,
        },
    )()

    window._sync_progress_slider()

    assert window.progress.value() == 30
    assert window.progress._buffer_value == 70


def test_sync_progress_slider_clamps_buffer_to_duration(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.progress_timer.stop()
    session = make_player_session(start_index=0)
    session.vod = VodItem(vod_id="movie-1", vod_name="Movie")
    window.open_session(session)
    window.video = type(
        "Video",
        (),
        {
            "position_seconds": lambda _self: 100,
            "duration_seconds": lambda _self: 120,
            "demuxer_cache_duration_seconds": lambda _self: 200,
        },
    )()

    window._sync_progress_slider()

    assert window.progress._buffer_value == 120


def test_clickable_slider_chapter_positions_are_sorted_and_deduped(qtbot) -> None:
    slider = ClickableSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setMinimum(0)
    slider.setMaximum(100)

    slider.set_chapter_positions([70.0, 30.0, 30.0, 10.5])

    assert slider._chapter_positions == [10.5, 30.0, 70.0]


def test_clickable_slider_chapter_positions_ignore_unusable_values(qtbot) -> None:
    slider = ClickableSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setMinimum(0)
    slider.setMaximum(100)

    slider.set_chapter_positions([30.0, "abc", None, float("nan"), float("inf")])

    assert slider._chapter_positions == [30.0]


def test_clickable_slider_keeps_chapter_positions_outside_current_range(qtbot) -> None:
    """Chapters can arrive before the duration is observed, so keep them for later."""
    slider = ClickableSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setMinimum(0)
    slider.setMaximum(0)
    slider.resize(200, 24)

    slider.set_chapter_positions([30.0, 60.0])

    assert slider._chapter_positions == [30.0, 60.0]
    assert slider._chapter_segments(12, 188) == []

    slider.setMaximum(120)

    assert len(slider._chapter_segments(12, 188)) == 3


def test_clickable_slider_without_chapters_has_no_segments(qtbot) -> None:
    slider = ClickableSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setMinimum(0)
    slider.setMaximum(120)
    slider.resize(200, 24)

    assert slider._chapter_segments(12, 188) == []


def test_clickable_slider_chapter_segments_span_full_width_with_gaps(qtbot) -> None:
    slider = ClickableSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setMinimum(0)
    slider.setMaximum(120)
    slider.resize(200, 24)
    slider.set_chapter_positions([60.0])

    segments = slider._chapter_segments(12, 188)

    assert len(segments) == 2
    first_start, first_width = segments[0]
    last_start, last_width = segments[-1]
    assert first_start == 0.0
    # Last segment reaches the right edge; earlier ones are trimmed by the gap.
    assert last_start + last_width == float(slider.width())
    assert first_start + first_width < last_start


def test_clickable_slider_merges_chapter_boundaries_that_are_too_close(qtbot) -> None:
    slider = ClickableSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setMinimum(0)
    slider.setMaximum(1200)
    slider.resize(200, 24)
    # 1200s over 188px => ~6.4s per pixel, so these land within the 4px merge window.
    slider.set_chapter_positions([600.0, 601.0, 602.0])

    assert len(slider._chapter_segments(12, 188)) == 2


def test_clickable_slider_paints_without_error_with_chapters(qtbot) -> None:
    slider = ClickableSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setMinimum(0)
    slider.setMaximum(120)
    slider.setValue(45)
    slider.set_buffer_value(90)
    slider.set_chapter_positions([30.0, 60.0, 90.0])
    slider.resize(200, 24)

    pixmap = slider.grab()

    assert not pixmap.isNull()


def test_player_window_refreshes_chapter_markers_from_video(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.progress_timer.stop()
    chapters = [
        Chapter(index=0, title="", start_seconds=0.0, label="章节 1"),
        Chapter(index=1, title="正片", start_seconds=92.0, label="正片"),
    ]
    window.video = type("Video", (), {"chapters": lambda _self: chapters})()

    window._refresh_chapter_markers()

    assert window._current_chapters == chapters
    assert window.progress._chapter_positions == [0.0, 92.0]

    window._clear_chapter_markers()

    assert window._current_chapters == []
    assert window.progress._chapter_positions == []


def test_player_window_chapter_refresh_tolerates_video_without_chapters(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.progress_timer.stop()
    window.progress.set_chapter_positions([30.0])

    window.video = type("Video", (), {})()
    window._refresh_chapter_markers()

    assert window._current_chapters == []
    assert window.progress._chapter_positions == []

    def _raise(_self):
        raise RuntimeError("mpv property unavailable")

    window.video = type("Video", (), {"chapters": _raise})()
    window._refresh_chapter_markers()

    assert window._current_chapters == []


def test_player_window_progress_tooltip_includes_chapter_label(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.progress_timer.stop()

    assert window._format_progress_tooltip(95) == "01:35"

    window._current_chapters = [
        Chapter(index=0, title="片头", start_seconds=0.0, label="片头"),
        Chapter(index=1, title="正片", start_seconds=92.0, label="正片"),
    ]

    assert window._format_progress_tooltip(10) == "00:10 · 片头"
    assert window._format_progress_tooltip(95) == "01:35 · 正片"
    assert window._format_progress_tooltip(92) == "01:32 · 正片"


def test_player_window_progress_tooltip_without_leading_chapter(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.progress_timer.stop()
    window._current_chapters = [
        Chapter(index=0, title="正片", start_seconds=92.0, label="正片"),
    ]

    assert window._format_progress_tooltip(10) == "00:10"
    assert window._format_progress_tooltip(200) == "03:20 · 正片"


def test_sync_progress_slider_handles_missing_cache_method(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.progress_timer.stop()
    session = make_player_session(start_index=0)
    session.vod = VodItem(vod_id="movie-1", vod_name="Movie")
    window.open_session(session)
    window.video = RecordingVideo()  # 没有 demuxer_cache_duration_seconds

    window._sync_progress_slider()

    assert window.progress._buffer_value == window.progress.value()


def test_open_session_resets_buffer_value(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.progress_timer.stop()
    window.progress.setMaximum(100)
    window.progress.set_buffer_value(90)
    session = make_player_session(start_index=0)
    session.vod = VodItem(vod_id="movie-1", vod_name="Movie")

    window.open_session(session)

    assert window.progress._buffer_value == 0


def test_player_window_restores_nested_drive_history_by_drive_path(qtbot) -> None:
    import base64

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    def dir_id(rel: str) -> str:
        path = f"/我的夸克分享/temp/quark@79710fe33776@{rel}"
        return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")

    s05_playlist = [
        PlayItem(
            title="S05E07",
            url="http://m/7",
            play_id="1@900001",
            path=f"/我的夸克分享/temp/quark@79710fe33776@/C 菜鸟老警 全8季 1080P/S05/S05E07.mp4",
        )
    ]
    s06_playlist = [
        PlayItem(
            title="S06E07",
            url="http://m/77",
            play_id="1@900002",
            path=f"/我的夸克分享/temp/quark@79710fe33776@/C 菜鸟老警 全8季 1080P/S06/S06E07.mp4",
        ),
        PlayItem(
            title="S06E08",
            url="http://m/8",
            play_id="1@188698",
            path=f"/我的夸克分享/temp/quark@79710fe33776@/C 菜鸟老警 全8季 1080P/S06/S06E08.mp4",
        ),
    ]
    parent_source = PlaybackSource(
        label="夸克资源",
        playlist=[PlayItem(title="夸克资源", url="")],
        subgroups=[
            PlaybackSourceGroup(
                label="S05",
                sources=[PlaybackSource(label="S05", playlist=s05_playlist)],
                drive_dir_id=dir_id("/C 菜鸟老警 全8季 1080P/S05"),
            ),
            PlaybackSourceGroup(
                label="S06",
                sources=[PlaybackSource(label="S06", playlist=s06_playlist)],
                drive_dir_id=dir_id("/C 菜鸟老警 全8季 1080P/S06"),
            ),
        ],
        subgroup_index=0,
    )
    # 安卓端记录:子目录名/索引/播放 id 都对不上本端解析结果,只有规范路径可信
    history = HistoryRecord(
        id=1,
        key="gy_tv_58kD",
        vod_name="菜鸟老警",
        vod_pic="",
        vod_remarks="08",
        episode=0,
        episode_url="1@188698@5@7",
        position=1063896,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1786763901121,
        source_subgroup_index=0,
        source_subgroup_name="6",
        drive_share_key="quark@79710fe33776@",
        drive_path="/C 菜鸟老警 全8季 1080P/S06/S06E08.mp4",
    )
    session = PlayerSession(
        vod=VodItem(vod_id="gy_tv_58kD", vod_name="菜鸟老警"),
        playlist=[],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        resume_history=history,
    )
    session.source_groups = [
        PlaybackSourceGroup(label="夸克", sources=[parent_source])
    ]
    session.source_group_index = 0
    session.source_index = 0
    window.session = session

    assert window._restore_nested_drive_history(parent_source) is True
    assert parent_source.subgroup_index == 1
    assert session.playlist is s06_playlist
    assert window.current_index == 1


def _make_youtube_hydrated_session() -> PlayerSession:
    return PlayerSession(
        vod=VodItem(vod_id="yt:video:up1234567", vod_name="Upgrade Video"),
        playlist=[
            PlayItem(
                title="Upgrade Video",
                url="https://rr4.example.googlevideo.com/videoplayback?itag=137",
                original_url="https://www.youtube.com/watch?v=up1234567",
                vod_id="yt:video:up1234567",
                playback_qualities=[
                    VideoQualityOption(id="ytdlp_360", label="360p"),
                    VideoQualityOption(id="ytdlp_1080", label="1080p"),
                ],
                selected_playback_quality_id="ytdlp_1080",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        source_kind="youtube",
    )


def test_player_window_upgrades_ytdlp_quality_after_hydration(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    video = RecordingVideo()
    window.video = video
    window.video_widget.current_video_height = lambda: 360

    window.open_session(_make_youtube_hydrated_session())
    current = window.session.playlist[window.current_index]
    load_count = len(video.load_calls)

    pending = player_window_module._PendingPlaybackLoader(
        index=window.current_index,
        previous_index=window.current_index,
        start_position_seconds=0,
        pause=False,
        hydrate_only=True,
        playback_started_url="https://rr3.example.googlevideo.com/videoplayback?itag=18",
        playback_started_quality_id="ytdlp_1080",
    )

    assert window._maybe_upgrade_ytdlp_playback_quality(current, pending) is True
    assert len(video.load_calls) == load_count + 1
    assert video.load_calls[-1][0] == "https://rr4.example.googlevideo.com/videoplayback?itag=137"
    assert video.load_calls[-1][1] == 30  # RecordingVideo.position_seconds()

    # Same-or-higher playing quality must not trigger another reload.
    window.video_widget.current_video_height = lambda: 1080
    assert window._maybe_upgrade_ytdlp_playback_quality(current, pending) is False
    assert len(video.load_calls) == load_count + 1

    # Live streams never auto-upgrade.
    window.video_widget.current_video_height = lambda: 360
    current.is_live = True
    assert window._maybe_upgrade_ytdlp_playback_quality(current, pending) is False
    assert len(video.load_calls) == load_count + 1
    current.is_live = False

    # No upgrade when hydration did not replace the playing URL.
    pending_same_url = player_window_module._PendingPlaybackLoader(
        index=window.current_index,
        previous_index=window.current_index,
        start_position_seconds=0,
        pause=False,
        hydrate_only=True,
        playback_started_url="https://rr4.example.googlevideo.com/videoplayback?itag=137",
    )
    assert window._maybe_upgrade_ytdlp_playback_quality(current, pending_same_url) is False
    assert len(video.load_calls) == load_count + 1


def test_player_window_recovers_failed_ytdlp_fast_stream_with_full_resolve(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    video = RecordingVideo()
    window.video = video

    loader_calls: list[PlayItem] = []

    def playback_loader(item):
        loader_calls.append(item)
        item.url = "https://rr5.example.googlevideo.com/videoplayback?itag=316"
        return None

    session = _make_youtube_hydrated_session()
    session.playlist[0].url = "https://rr3.example.googlevideo.com/videoplayback?itag=18"
    session.playlist[0].playback_qualities = []
    session.playlist[0].selected_playback_quality_id = "ytdlp_1080"

    window.open_session(session)
    session.playback_loader = playback_loader
    session.async_playback_loader = True
    current = window.session.playlist[window.current_index]
    load_count = len(video.load_calls)

    window._handle_playback_failed("播放失败: fast stream died")

    _spin_until(lambda: len(loader_calls) == 1 and len(video.load_calls) > load_count)
    assert loader_calls[0] is current
    assert video.load_calls[-1][0] == "https://rr5.example.googlevideo.com/videoplayback?itag=316"
    assert video.load_calls[-1][1] == 30  # resumed at the failure position

    # Recovery only fires once per item.
    assert window._ytdlp_full_resolve_recovery_item is current
    assert window._try_recover_youtube_playback_with_full_resolve("again") is False


def test_player_window_recovery_cancels_pending_hydration_loader(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    video = RecordingVideo()
    window.video = video

    loader_calls: list[PlayItem] = []

    def playback_loader(item):
        loader_calls.append(item)
        item.url = "https://rr5.example.googlevideo.com/videoplayback?itag=316"
        return None

    session = _make_youtube_hydrated_session()
    session.playlist[0].url = "https://rr3.example.googlevideo.com/videoplayback?itag=18"
    session.playlist[0].playback_qualities = []
    session.playlist[0].selected_playback_quality_id = "ytdlp_1080"

    window.open_session(session)
    session.playback_loader = playback_loader
    session.async_playback_loader = True
    before_request_id = window._playback_loader_request_id
    window._pending_playback_loader = player_window_module._PendingPlaybackLoader(
        index=window.current_index,
        previous_index=window.current_index,
        start_position_seconds=0,
        pause=False,
        hydrate_only=True,
    )

    assert window._try_recover_youtube_playback_with_full_resolve("dead") is True
    # One bump cancels the stale hydration wait, one starts the recovery loader.
    assert window._playback_loader_request_id == before_request_id + 2
    assert window._pending_playback_loader is not None
    assert window._pending_playback_loader.hydrate_only is False

    _spin_until(lambda: len(loader_calls) == 1)


class PauseStateVideo(RecordingVideo):
    """Mirrors mpv's pause property so tests can compare UI state vs actual state."""

    def __init__(self) -> None:
        super().__init__()
        self.paused = True
        self.load_pause_flags: list[bool] = []

    def load(
        self, url: str, pause: bool = False, start_seconds: int = 0, **_kwargs
    ) -> None:
        super().load(url, start_seconds=start_seconds)
        self.paused = pause
        self.load_pause_flags.append(pause)

    def pause(self) -> None:
        super().pause()
        self.paused = True

    def resume(self) -> None:
        super().resume()
        self.paused = False


class GatedPrepareAdFilter:
    """prepare() blocks until released, simulating a slow stream resolve on restore."""

    def __init__(self) -> None:
        self.enabled = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.error: Exception | None = None

    def should_prepare(self, url: str) -> bool:
        return self.enabled

    def prepare(self, url: str, headers=None, **_kwargs) -> str:
        self.entered.set()
        self.release.wait(timeout=5)
        if self.error is not None:
            raise self.error
        return url


def _open_playing_session(
    qtbot, ad_filter=None
) -> tuple[PlayerWindow, PauseStateVideo]:
    config = AppConfig()
    window = PlayerWindow(
        FakePlayerController(), config=config, m3u8_ad_filter=ad_filter
    )
    qtbot.addWidget(window)
    video = PauseStateVideo()
    window.video = video
    window.open_session(make_player_session())
    _spin_until(lambda: bool(video.load_pause_flags))
    return window, video


def test_player_window_restore_prepare_keeps_toggled_pause(qtbot) -> None:
    ad_filter = GatedPrepareAdFilter()
    window, video = _open_playing_session(qtbot, ad_filter=ad_filter)
    assert window.is_playing is True
    assert video.paused is False

    window._return_to_main()
    assert window.is_playing is False
    assert video.paused is True

    ad_filter.enabled = True
    window.resume_from_main()
    _spin_until(ad_filter.entered.is_set)
    # The user pauses while the restore reload is still resolving in the background.
    window.toggle_playback()
    assert window.is_playing is False
    assert video.paused is True

    ad_filter.release.set()
    _spin_until(lambda: len(video.load_pause_flags) >= 2)

    assert video.paused is True, "deferred load honors the pause toggled while pending"
    assert window.is_playing == (not video.paused)


def test_player_window_restore_prepare_failure_fallback_keeps_pause(qtbot) -> None:
    ad_filter = GatedPrepareAdFilter()
    window, video = _open_playing_session(qtbot, ad_filter=ad_filter)

    window._return_to_main()
    ad_filter.enabled = True
    ad_filter.error = RuntimeError("boom")
    window.resume_from_main()
    _spin_until(ad_filter.entered.is_set)
    window.toggle_playback()
    ad_filter.release.set()
    # Prepare fails; the original URL fallback still loads and must stay paused.
    _spin_until(lambda: len(video.load_pause_flags) >= 2)

    assert video.paused is True
    assert window.is_playing == (not video.paused)


def test_player_window_playback_failure_resets_play_button_state(qtbot) -> None:
    window, video = _open_playing_session(qtbot)
    assert window.is_playing is True

    window._handle_playback_failed("播放失败: 网络中断")

    assert window._startup_state.stage.value == "failed"
    assert window.is_playing is False, "nothing is playing after the failure"


def test_player_window_syncs_play_button_from_mpv_pause_state(qtbot) -> None:
    window, video = _open_playing_session(qtbot)
    assert window.is_playing is True

    window.video_widget.pause_state_changed.emit(True)
    assert window.is_playing is False

    window.video_widget.pause_state_changed.emit(False)
    assert window.is_playing is True
