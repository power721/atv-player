from PySide6.QtCore import QByteArray, QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QComboBox
from PySide6.QtWidgets import QSplitter
from atv_player.controllers.player_controller import PlayerSession
from atv_player.models import AppConfig, PlayItem, VodItem

import atv_player.ui.player_window as player_window_module
from atv_player.ui.player_window import PlayerWindow


class FakePlayerController:
    def report_progress(self, session, current_index: int, position_seconds: int, speed: float) -> None:
        return None


class RecordingPlayerController(FakePlayerController):
    def __init__(self) -> None:
        self.progress_calls: list[tuple[int, int, float]] = []

    def report_progress(self, session, current_index: int, position_seconds: int, speed: float) -> None:
        self.progress_calls.append((current_index, position_seconds, speed))


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


def test_player_window_uses_splitters_for_resizable_panels(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    assert isinstance(window.main_splitter, QSplitter)
    assert window.main_splitter.orientation() == Qt.Orientation.Horizontal
    assert isinstance(window.sidebar_splitter, QSplitter)
    assert window.sidebar_splitter.orientation() == Qt.Orientation.Vertical


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
    assert "恢复播放失败" in window.details.toPlainText()


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

    assert window.video.seek_relative_calls == [-10, 10]
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
    assert window.current_speed == 0.5

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
    assert video.seek_relative_calls == [-15, 15]

    send_key(window, Qt.Key.Key_PageUp)
    assert window.current_index == 0
    assert window.playlist.currentRow() == 0

    send_key(window, Qt.Key.Key_PageDown)
    assert window.current_index == 1
    assert window.playlist.currentRow() == 1
    assert controller.progress_calls == [(1, 30, 1.0), (0, 30, 1.0)]


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
