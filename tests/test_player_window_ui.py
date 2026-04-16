from PySide6.QtCore import QByteArray, QEvent, Qt
from PySide6.QtGui import QColor, QCursor, QImage, QKeyEvent, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication, QComboBox
from PySide6.QtWidgets import QSplitter, QToolTip
from atv_player.controllers.player_controller import PlayerSession
from atv_player.models import AppConfig, PlayItem, VodItem
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
    ) -> None:
        return None

    def resolve_play_item_detail(self, session, play_item):
        return None

    def stop_playback(self, session, current_index: int) -> None:
        return None


class RecordingPlayerController(FakePlayerController):
    def __init__(self) -> None:
        self.progress_calls: list[tuple[int, int, float, int, int]] = []
        self.stop_calls: list[int] = []

    def report_progress(
        self,
        session,
        current_index: int,
        position_seconds: int,
        speed: float,
        opening_seconds: int,
        ending_seconds: int,
    ) -> None:
        self.progress_calls.append((current_index, position_seconds, speed, opening_seconds, ending_seconds))

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


def test_player_window_uses_splitters_for_resizable_panels(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert isinstance(window.main_splitter, QSplitter)
    assert window.main_splitter.orientation() == Qt.Orientation.Horizontal
    assert isinstance(window.sidebar_splitter, QSplitter)
    assert window.sidebar_splitter.orientation() == Qt.Orientation.Vertical


def test_player_window_uses_detail_container_with_metadata_and_log_views(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert window.details is not None
    assert window.metadata_view.isReadOnly() is True
    assert window.log_view.isReadOnly() is True
    assert window.details.layout().indexOf(window.metadata_view) != -1
    assert window.details.layout().indexOf(window.log_view) != -1


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

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
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

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
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

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
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

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
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


def test_player_window_exposes_audio_combo_with_default_auto_entry(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert isinstance(window.audio_combo, QComboBox)
    assert window.audio_combo.count() == 1
    assert window.audio_combo.itemText(0) == "自动选择"
    assert window.audio_combo.isEnabled() is False


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
    assert controller.progress_calls == [(0, 30, 1.0, 0, 0)]


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
        PlayItem(title="Episode 1", url="http://m/1.m3u8", vod_id="ep-1"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]
    session.detail_resolver = lambda item: resolved_vod
    window.open_session(session)
    window.video.load_calls.clear()

    window.play_next()

    assert window.current_index == 1
    assert window.video.load_calls == [("http://resolved/2.m3u8", 0)]
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
        PlayItem(title="Episode 1", url="", vod_id="ep-1"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]
    session.detail_resolver = detail_resolver
    window.open_session(session)
    detail_calls.clear()
    window.video.load_calls.clear()

    window.play_next()
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
        PlayItem(title="Episode 1", url="", vod_id="ep-1"),
        PlayItem(title="Episode 2", url="", vod_id="ep-2"),
    ]
    session.detail_resolver = detail_resolver
    window.open_session(session)
    window.video.load_calls.clear()

    window.play_next()

    assert window.current_index == 0
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

    assert controller.progress_calls == [(0, 30, 1.0, 0, 0)]
    assert controller.stop_calls == [0]
    assert window.video.load_calls == [("http://m/2.m3u8", 0)]


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


def test_player_window_return_to_main_hides_window_without_closing_session(qtbot) -> None:
    emitted = {"count": 0}
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    pauses = {"count": 0}

    class FakeVideo:
        def pause(self) -> None:
            pauses["count"] += 1

    window.session = object()
    window.video = FakeVideo()
    window.closed_to_main.connect(lambda: emitted.__setitem__("count", emitted["count"] + 1))
    window.show()
    window._return_to_main()

    assert emitted["count"] == 1
    assert window.isHidden() is True
    assert window.session is not None
    assert config.last_active_window == "main"
    assert pauses["count"] == 1


def test_player_window_ctrl_q_quits_application(qtbot, monkeypatch) -> None:
    called = {"count": 0}
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)

    monkeypatch.setattr(QApplication, "quit", lambda *args, **kwargs: called.__setitem__("count", called["count"] + 1))

    window._quit_application()

    assert called["count"] == 1
    assert config.last_active_window == "player"


def test_player_window_quit_application_preserves_current_paused_state(qtbot, monkeypatch) -> None:
    config = AppConfig(last_active_window="player", last_player_paused=False)
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.is_playing = False

    monkeypatch.setattr(QApplication, "quit", lambda *args, **kwargs: None)

    window._quit_application()

    assert config.last_player_paused is True


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
    assert controller.progress_calls == [(1, 30, 1.0, 0, 0), (0, 30, 1.0, 0, 0)]


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


def test_player_window_return_to_main_restores_application_title(qtbot) -> None:
    config = AppConfig(last_active_window="player")
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    window.video = RecordingVideo()
    window.open_session(make_player_session(start_index=0))

    window._return_to_main()

    assert window.windowTitle() == "alist-tvbox 播放器"


def test_player_window_resume_from_main_resumes_playback_and_updates_state(qtbot) -> None:
    config = AppConfig(last_active_window="main", last_player_paused=True)
    window = PlayerWindow(FakePlayerController(), config=config, save_config=lambda: None)
    qtbot.addWidget(window)
    video = RecordingVideo()
    window.video = video
    window.open_session(make_player_session(start_index=0))

    window._return_to_main()
    window.resume_from_main()

    assert video.pause_calls == 1
    assert video.resume_calls == 1
    assert window.is_playing is True
    assert window.windowTitle() == "Movie - Episode 1"
    assert config.last_player_paused is False
