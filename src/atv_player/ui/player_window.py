from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from PySide6.QtCore import QByteArray, QEvent, QObject, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QCursor, QIcon, QImage, QKeyEvent, QKeySequence, QMouseEvent, QPixmap, QShortcut
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionSlider
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedLayout,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from atv_player.player.mpv_widget import MpvWidget, SubtitleTrack
from atv_player.ui.poster_loader import load_remote_poster_image, normalize_poster_url


class ClickableSlider(QSlider):
    """A QSlider that allows clicking on the groove to set position directly."""

    clicked_value = Signal(int)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            handle_rect = self.style().subControlRect(
                QStyle.CC_Slider,
                option,
                QStyle.SC_SliderHandle,
                self,
            )

            if handle_rect.contains(event.position().toPoint()):
                super().mousePressEvent(event)
                return

            value = self._pixel_pos_to_value(event.position().x())
            self.setValue(value)
            self.clicked_value.emit(value)
            event.accept()
            return

        super().mousePressEvent(event)

    def _pixel_pos_to_value(self, pos: int) -> int:
        groove_rect = self.rect()
        handle_width = 12
        available_width = groove_rect.width() - handle_width

        if available_width <= 0:
            return self.minimum()

        adjusted_pos = pos - handle_width // 2
        adjusted_pos = max(0, min(adjusted_pos, available_width))

        value_range = self.maximum() - self.minimum()
        value = self.minimum() + int((adjusted_pos / available_width) * value_range)
        return value


class _PosterLoadSignals(QObject):
    loaded = Signal(int, object)


@dataclass(slots=True)
class SubtitlePreference:
    mode: str = "auto"
    title: str = ""
    lang: str = ""
    is_default: bool = False
    is_forced: bool = False


class PlayerWindow(QWidget):
    closed_to_main = Signal()
    _SEEK_SHORTCUT_SECONDS = 15
    _VOLUME_SHORTCUT_STEP = 5
    _CURSOR_HIDE_DELAY_MS = 3000
    _POSTER_SIZE = QSize(180, 260)
    _POSTER_REQUEST_TIMEOUT_SECONDS = 10.0

    def __init__(self, controller, config=None, save_config=None) -> None:
        super().__init__()
        self.controller = controller
        self.config = config
        self._save_config = save_config or (lambda: None)
        self.session = None
        self.current_index = 0
        self.current_speed = 1.0
        self.is_playing = True
        self._is_muted = False
        self._was_maximized_before_fullscreen = False
        self._quit_requested = False
        self._video_pointer_inside = False
        self._app_event_filter_installed = False
        self._last_cursor_pos = None
        self._last_cursor_activity_ms = 0
        self._poster_request_id = 0
        self._video_surface_ready = False
        self._poster_load_signals = _PosterLoadSignals()
        self._poster_load_signals.loaded.connect(self._handle_poster_load_finished)
        self.setWindowTitle("alist-tvbox 播放器")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.resize(1280, 800)
        self.setMinimumSize(1000, 700)
        self._icons_dir = Path(__file__).resolve().parent.parent / "icons"
        if self.config and self.config.player_window_geometry:
            self.restoreGeometry(QByteArray(self.config.player_window_geometry))

        self.video_widget = MpvWidget(self)
        self._configure_video_surface_widgets()
        self.video = self.video_widget
        self.playlist = QListWidget()
        self.play_button = self._create_icon_button("play.svg", "播放/暂停", "Space")
        self.prev_button = self._create_icon_button("previous.svg", "上一集", "PgUp")
        self.next_button = self._create_icon_button("next.svg", "下一集", "PgDn")
        self.backward_button = self._create_icon_button("seek-backward.svg", "后退", "Left")
        self.forward_button = self._create_icon_button("seek-forward.svg", "前进", "Right")
        self.refresh_button = self._create_icon_button("refresh.svg", "重新播放")
        self.mute_button = self._create_icon_button("volume-on.svg", "静音", "M")
        self.wide_button = self._create_icon_button("grid.svg", "宽屏")
        self.fullscreen_button = self._create_icon_button("maximize.svg", "全屏", "Enter")
        self.wide_button.setCheckable(True)
        self.toggle_playlist_button = self._create_icon_button("queue.svg", "播放列表")
        self.toggle_details_button = self._create_icon_button("info.svg", "详情")
        self.toggle_playlist_button.setCheckable(True)
        self.toggle_details_button.setCheckable(True)
        self.toggle_playlist_button.setChecked(True)
        self.toggle_details_button.setChecked(True)

        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "2.0x"])
        self.speed_combo.setCurrentText("1.0x")
        self._subtitle_tracks: list[SubtitleTrack] = []
        self._subtitle_preference = SubtitlePreference()
        self.subtitle_combo = QComboBox()
        self.subtitle_combo.addItem("自动选择", ("auto", None))
        self.subtitle_combo.setEnabled(False)
        self.opening_spin = self._create_skip_spinbox("片头 ")
        self.ending_spin = self._create_skip_spinbox("片尾 ")

        self.current_time_label = QLabel("00:00")
        self.current_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.duration_label = QLabel("00:00")
        self.duration_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress = ClickableSlider(Qt.Orientation.Horizontal)
        self.progress.setFixedHeight(24)
        self.volume_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        initial_volume = 100
        if self.config is not None:
            initial_volume = max(
                self.volume_slider.minimum(),
                min(getattr(self.config, "player_volume", 100), self.volume_slider.maximum()),
            )
        self.volume_slider.setValue(initial_volume)
        self.volume_slider.setMaximumWidth(180)
        self.poster_label = QLabel()
        self.poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster_label.setMinimumSize(self._POSTER_SIZE)
        self.poster_label.setMaximumSize(self._POSTER_SIZE)
        self.poster_label.setText("")
        self.video_poster_overlay = QLabel()
        self.video_poster_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_poster_overlay.setText("")
        self.video_poster_overlay.hide()
        self.metadata_view = QTextEdit()
        self.metadata_view.setReadOnly(True)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(6)
        details_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        details_layout.addWidget(self.poster_label, 0, Qt.AlignmentFlag.AlignHCenter)
        details_layout.addWidget(QLabel("影片详情"))
        details_layout.addWidget(self.metadata_view, 3)
        details_layout.addWidget(QLabel("播放日志"))
        details_layout.addWidget(self.log_view, 1)

        self.report_timer = QTimer(self)
        self.report_timer.setInterval(5000)
        self.report_timer.timeout.connect(self.report_progress)
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(1000)
        self.progress_timer.timeout.connect(self._sync_progress_slider)
        self._cursor_hide_timer = QTimer(self)
        self._cursor_hide_timer.setInterval(100)
        self._cursor_hide_timer.timeout.connect(self._poll_cursor_idle_state)
        self._slider_dragging = False

        self.sidebar_actions_widget = QWidget()
        sidebar_actions = QHBoxLayout(self.sidebar_actions_widget)
        sidebar_actions.setContentsMargins(0, 0, 0, 0)
        sidebar_actions.addWidget(self.toggle_playlist_button)
        sidebar_actions.addWidget(self.toggle_details_button)

        self.bottom_area = QWidget()
        self.bottom_area.setMaximumHeight(72)
        bottom_layout = QVBoxLayout(self.bottom_area)
        self.bottom_layout = bottom_layout
        bottom_layout.setContentsMargins(12, 6, 12, 6)
        bottom_layout.setSpacing(4)

        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.addWidget(self.current_time_label)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.duration_label)
        bottom_layout.addLayout(progress_row)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addStretch(1)

        control_group = QWidget()
        control_group_layout = QHBoxLayout(control_group)
        control_group_layout.setContentsMargins(0, 0, 0, 0)
        control_group_layout.addWidget(self.prev_button)
        control_group_layout.addWidget(self.play_button)
        control_group_layout.addWidget(self.next_button)
        control_group_layout.addWidget(self.backward_button)
        control_group_layout.addWidget(self.forward_button)
        control_group_layout.addWidget(self.refresh_button)
        control_group_layout.addWidget(self.wide_button)
        control_group_layout.addWidget(self.fullscreen_button)
        control_group_layout.addWidget(self.speed_combo)
        control_group_layout.addWidget(self.subtitle_combo)
        control_group_layout.addWidget(self.opening_spin)
        control_group_layout.addWidget(self.ending_spin)
        controls.addWidget(control_group, 0, Qt.AlignmentFlag.AlignCenter)
        controls.addStretch(1)

        volume_group = QWidget()
        self.volume_layout = QHBoxLayout(volume_group)
        self.volume_layout.setContentsMargins(0, 0, 0, 0)
        self.volume_layout.addWidget(self.mute_button)
        self.volume_layout.addWidget(self.volume_slider)
        controls.addWidget(volume_group, 0, Qt.AlignmentFlag.AlignRight)
        bottom_layout.addLayout(controls)

        video_container = QWidget()
        video_layout = QVBoxLayout(video_container)
        video_layout.setContentsMargins(0, 0, 0, 0)
        self.video_stack = QWidget()
        self.video_stack_layout = QStackedLayout(self.video_stack)
        self.video_stack_layout.setContentsMargins(0, 0, 0, 0)
        self.video_stack_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.video_stack_layout.addWidget(self.video_widget)
        self.video_stack_layout.addWidget(self.video_poster_overlay)
        video_layout.addWidget(self.video_stack)

        self.sidebar_splitter = QSplitter(Qt.Orientation.Vertical)
        self.sidebar_splitter.addWidget(self.playlist)
        self.sidebar_splitter.addWidget(self.details)
        self.sidebar_splitter.setChildrenCollapsible(True)

        sidebar_layout = QVBoxLayout()
        sidebar_layout.addWidget(self.sidebar_actions_widget)
        sidebar_layout.addWidget(self.sidebar_splitter)
        self.sidebar_container = QWidget()
        self.sidebar_container.setMinimumWidth(250)
        self.sidebar_container.setLayout(sidebar_layout)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.addWidget(video_container)
        self.main_splitter.addWidget(self.sidebar_container)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 1)
        self._restore_main_splitter_state()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.main_splitter, 1)
        layout.addWidget(self.bottom_area, 0)

        self.play_button.clicked.connect(self.toggle_playback)
        self.prev_button.clicked.connect(self.play_previous)
        self.next_button.clicked.connect(self.play_next)
        self.backward_button.clicked.connect(lambda: self._seek_relative(-self._SEEK_SHORTCUT_SECONDS))
        self.forward_button.clicked.connect(lambda: self._seek_relative(self._SEEK_SHORTCUT_SECONDS))
        self.refresh_button.clicked.connect(self._replay_current_item)
        self.mute_button.clicked.connect(self._toggle_mute)
        self.wide_button.clicked.connect(self._toggle_wide_mode)
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.speed_combo.currentTextChanged.connect(self._change_speed)
        self.subtitle_combo.currentIndexChanged.connect(self._change_subtitle_selection)
        self.opening_spin.valueChanged.connect(self._change_opening_seconds)
        self.ending_spin.valueChanged.connect(self._change_ending_seconds)
        self.volume_slider.valueChanged.connect(self._change_volume)
        self.playlist.itemDoubleClicked.connect(self._play_clicked_item)
        self.toggle_playlist_button.clicked.connect(self._update_sidebar_visibility)
        self.toggle_details_button.clicked.connect(self._update_sidebar_visibility)
        self.video_widget.double_clicked.connect(self.toggle_fullscreen)
        self.video_widget.playback_finished.connect(self._handle_playback_finished)
        self.progress.sliderPressed.connect(self._handle_slider_pressed)
        self.progress.sliderReleased.connect(self._seek_from_slider)
        self.progress.clicked_value.connect(self._seek_to_position)
        self.quit_shortcut = QShortcut(QKeySequence.StandardKey.Quit, self)
        self.quit_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.quit_shortcut.activated.connect(self._quit_application)
        self.return_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        self.return_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.return_shortcut.activated.connect(self._return_to_main)
        self.escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.escape_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.escape_shortcut.activated.connect(self._handle_escape)
        self._shortcut_bindings: list[QShortcut] = []
        self._register_shortcuts()
        self._update_play_button_icon()
        self._apply_visibility_state()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._app_event_filter_installed = True

    def _format_tooltip(self, label: str, shortcut: str | None = None) -> str:
        if shortcut is None:
            return label
        return f"{label} ({shortcut})"

    def _create_icon_button(self, icon_name: str, tooltip: str, shortcut: str | None = None) -> QPushButton:
        button = QPushButton("")
        button.setToolTip(self._format_tooltip(tooltip, shortcut))
        button.setIcon(QIcon(str(self._icons_dir / icon_name)))
        button.setIconSize(button.iconSize())
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedHeight(28)
        return button

    def _create_skip_spinbox(self, prefix: str) -> QSpinBox:
        spinbox = QSpinBox()
        spinbox.setPrefix(prefix)
        spinbox.setSuffix("s")
        spinbox.setRange(0, 240)
        spinbox.setFixedHeight(28)
        spinbox.setSingleStep(10)
        return spinbox

    def _update_play_button_icon(self) -> None:
        icon_name = "pause.svg" if self.is_playing else "play.svg"
        self.play_button.setIcon(QIcon(str(self._icons_dir / icon_name)))

    def _set_button_icon(self, button: QPushButton, icon_name: str) -> None:
        button.setIcon(QIcon(str(self._icons_dir / icon_name)))

    def _update_mute_button_icon(self) -> None:
        icon_name = "volume-off.svg" if self._is_muted else "volume-on.svg"
        self._set_button_icon(self.mute_button, icon_name)

    def _video_surface_widgets(self) -> list[QWidget]:
        return [self.video_widget, *self.video_widget.findChildren(QWidget)]

    def _belongs_to_player_window(self, watched: object) -> bool:
        return isinstance(watched, QWidget) and (watched is self or watched.window() is self)

    def _configure_video_surface_widgets(self) -> None:
        for widget in self._video_surface_widgets():
            widget.setMouseTracking(True)
            widget.installEventFilter(self)
            widget.setCursor(Qt.CursorShape.ArrowCursor)

    def _set_video_cursor_hidden(self, hidden: bool) -> None:
        cursor_shape = Qt.CursorShape.BlankCursor if hidden else Qt.CursorShape.ArrowCursor
        for widget in self._video_surface_widgets():
            widget.setCursor(cursor_shape)
        self.setCursor(cursor_shape)

    def _restore_video_cursor(self, stop_timer: bool = True, disable_native_autohide: bool = True) -> None:
        if stop_timer:
            self._cursor_hide_timer.stop()
        self._set_video_cursor_hidden(False)
        if hasattr(self.video, "set_cursor_autohide"):
            if disable_native_autohide:
                self.video.set_cursor_autohide(None)
            elif self.is_playing:
                self.video.set_cursor_autohide(self._CURSOR_HIDE_DELAY_MS)

    def _cursor_now_ms(self) -> int:
        return int(time.monotonic() * 1000)

    def _handle_video_mouse_activity(self, now_ms: int | None = None) -> None:
        now_ms = self._cursor_now_ms() if now_ms is None else now_ms
        self._last_cursor_pos = QCursor.pos()
        self._last_cursor_activity_ms = now_ms
        self._set_video_cursor_hidden(False)
        if self.is_playing:
            if hasattr(self.video, "set_cursor_autohide"):
                self.video.set_cursor_autohide(self._CURSOR_HIDE_DELAY_MS)
            if not self._cursor_hide_timer.isActive():
                self._cursor_hide_timer.start()
            return
        self._restore_video_cursor()

    def _handle_video_leave(self) -> None:
        self._video_pointer_inside = False
        if self.is_playing:
            self._restore_video_cursor(stop_timer=False, disable_native_autohide=False)
            if not self._cursor_hide_timer.isActive():
                self._cursor_hide_timer.start()
            return
        self._restore_video_cursor()

    def _hide_video_cursor_if_idle(self) -> None:
        if self.is_playing and self._video_pointer_inside:
            self._set_video_cursor_hidden(True)

    def _refresh_video_pointer_inside_state(self) -> None:
        global_pos = QCursor.pos()
        local_pos = self.video_widget.mapFromGlobal(global_pos)
        self._video_pointer_inside = self.video_widget.rect().contains(local_pos)

    def _poll_cursor_idle_state(self, now_ms: int | None = None) -> None:
        now_ms = self._cursor_now_ms() if now_ms is None else now_ms
        global_pos = QCursor.pos()
        if self._last_cursor_pos is None or global_pos != self._last_cursor_pos:
            self._refresh_video_pointer_inside_state()
            self._handle_video_mouse_activity(now_ms=now_ms)
            return
        self._refresh_video_pointer_inside_state()
        if not self.is_playing:
            self._restore_video_cursor()
            return
        if not self._video_pointer_inside:
            self._restore_video_cursor(stop_timer=False, disable_native_autohide=False)
            if not self._cursor_hide_timer.isActive():
                self._cursor_hide_timer.start()
            return
        if hasattr(self.video, "set_cursor_autohide"):
            self.video.set_cursor_autohide(self._CURSOR_HIDE_DELAY_MS)
        if now_ms - self._last_cursor_activity_ms >= self._CURSOR_HIDE_DELAY_MS:
            self._set_video_cursor_hidden(True)

    def _sync_video_cursor_autohide(self) -> None:
        self._refresh_video_pointer_inside_state()
        if self.is_playing and self._video_pointer_inside:
            self._handle_video_mouse_activity()
            return
        if self.is_playing:
            self._last_cursor_pos = QCursor.pos()
            self._last_cursor_activity_ms = self._cursor_now_ms()
            if not self._cursor_hide_timer.isActive():
                self._cursor_hide_timer.start()
            self._restore_video_cursor(stop_timer=False, disable_native_autohide=False)
            return
        self._restore_video_cursor()

    def open_session(self, session, start_paused: bool = False) -> None:
        self.session = session
        self._render_poster()
        self._render_metadata()
        self._reset_log()
        self.current_index = session.start_index
        self.current_speed = session.speed
        self.opening_spin.blockSignals(True)
        self.ending_spin.blockSignals(True)
        self.opening_spin.setValue(session.opening_seconds)
        self.ending_spin.setValue(session.ending_seconds)
        self.opening_spin.blockSignals(False)
        self.ending_spin.blockSignals(False)
        speed_text = self._speed_text(session.speed)
        speed_index = self.speed_combo.findText(speed_text)
        if speed_index >= 0:
            self.speed_combo.setCurrentIndex(speed_index)
        self.is_playing = not start_paused
        self._set_last_player_paused(start_paused)
        self._update_play_button_icon()
        self.playlist.clear()
        for item in session.playlist:
            self.playlist.addItem(QListWidgetItem(item.title))
        self.playlist.setCurrentRow(self.current_index)
        self.progress.setValue(0)
        self._reset_subtitle_combo()
        try:
            self._play_item_at_index(self.current_index, start_position_seconds=session.start_position_seconds, pause=start_paused)
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")
        self.report_timer.start()
        self.progress_timer.start()
        self._sync_video_cursor_autohide()

    def _load_current_item(self, start_position_seconds: int = 0, pause: bool = False) -> None:
        if self.session is None:
            return
        self._resolve_current_play_item()
        current_item = self.session.playlist[self.current_index]
        self._append_log(f"当前: {current_item.title}")
        self._append_log(f"URL: {current_item.url}")
        effective_start_seconds = max(start_position_seconds, self.opening_spin.value())
        self.video.load(current_item.url, pause=pause, start_seconds=effective_start_seconds)
        self.video.set_speed(self.current_speed)
        self.video.set_volume(self.volume_slider.value())
        self._refresh_subtitle_state()

    def _format_metadata_text(self, vod) -> str:
        rows = [
            ("名称", vod.vod_name),
            ("类型", vod.type_name),
            ("年代", vod.vod_year),
            ("地区", vod.vod_area),
            ("语言", vod.vod_lang),
            ("评分", vod.vod_remarks),
            ("导演", vod.vod_director),
            ("演员", vod.vod_actor),
            ("豆瓣ID", str(vod.dbid) if vod.dbid else ""),
        ]
        lines = [f"{label}: {value}".rstrip() for label, value in rows]
        lines.append("")
        lines.append("简介:")
        lines.append(vod.vod_content)
        return "\n".join(lines)

    def _render_metadata(self) -> None:
        if self.session is None:
            self.metadata_view.clear()
            return
        self.metadata_view.setPlainText(self._format_metadata_text(self.session.vod))

    def _apply_resolved_vod(self, resolved_vod: VodItem) -> None:
        if self.session is None:
            return
        self.session.vod = resolved_vod
        self._render_poster()
        self._render_metadata()

    def _resolve_current_play_item(self) -> None:
        if self.session is None:
            return
        current_item = self.session.playlist[self.current_index]
        resolved_vod = self.controller.resolve_play_item_detail(self.session, current_item)
        if resolved_vod is not None:
            self._apply_resolved_vod(resolved_vod)

    def _play_item_at_index(self, index: int, start_position_seconds: int = 0, pause: bool = False) -> None:
        if self.session is None:
            return
        previous_index = self.current_index
        self.current_index = index
        try:
            self.playlist.setCurrentRow(self.current_index)
            self._load_current_item(start_position_seconds=start_position_seconds, pause=pause)
        except Exception:
            self.current_index = previous_index
            self.playlist.setCurrentRow(previous_index)
            raise

    def _clear_poster(self) -> None:
        self.poster_label.clear()
        self.poster_label.setText("")
        self.poster_label.setPixmap(QPixmap())
        self._clear_video_poster_overlay()

    def _clear_video_poster_overlay(self) -> None:
        self.video_poster_overlay.clear()
        self.video_poster_overlay.setText("")
        self.video_poster_overlay.setPixmap(QPixmap())
        self.video_poster_overlay.hide()

    def _show_video_poster_overlay(self, pixmap: QPixmap) -> None:
        if pixmap.isNull() or self._video_surface_ready:
            self.video_poster_overlay.hide()
            return
        target_size = self.video_stack.size()
        if target_size.width() <= 0 or target_size.height() <= 0:
            target_size = self._POSTER_SIZE
        self.video_poster_overlay.setText("")
        self.video_poster_overlay.setPixmap(
            pixmap.scaled(
                target_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.video_poster_overlay.show()

    def _load_poster_pixmap(self, source: str) -> QPixmap:
        if not source:
            return QPixmap()
        source_path = Path(source)
        if not source_path.is_file():
            return QPixmap()
        pixmap = QPixmap(str(source_path))
        if pixmap.isNull():
            return QPixmap()
        return pixmap.scaled(
            self._POSTER_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _start_poster_load(self, source: str, request_id: int) -> None:
        image_url = normalize_poster_url(source)
        if not image_url:
            return

        def load() -> None:
            image = load_remote_poster_image(
                image_url,
                self._POSTER_SIZE,
                timeout=self._POSTER_REQUEST_TIMEOUT_SECONDS,
                get=httpx.get,
            )
            self._poster_load_signals.loaded.emit(request_id, image)

        threading.Thread(target=load, daemon=True).start()

    def _handle_poster_load_finished(self, request_id: int, image: QImage | None) -> None:
        if request_id != self._poster_request_id:
            return
        if image is None or image.isNull():
            self._clear_poster()
            return
        pixmap = QPixmap.fromImage(image)
        self.poster_label.setText("")
        self.poster_label.setPixmap(pixmap)
        self._show_video_poster_overlay(pixmap)

    def _render_poster(self) -> None:
        self._poster_request_id += 1
        self._video_surface_ready = False
        if self.session is None:
            self._clear_poster()
            return
        source = self.session.vod.vod_pic
        if not source:
            self._clear_poster()
            return
        pixmap = self._load_poster_pixmap(source)
        if not pixmap.isNull():
            self.poster_label.setText("")
            self.poster_label.setPixmap(pixmap)
            self._show_video_poster_overlay(pixmap)
            return
        self._clear_poster()
        self._start_poster_load(source, self._poster_request_id)

    def _reset_log(self) -> None:
        self.log_view.clear()

    def _append_log(self, message: str) -> None:
        if not message:
            return
        if self.log_view.toPlainText():
            self.log_view.append(message)
            return
        self.log_view.setPlainText(message)

    def _set_last_player_paused(self, paused: bool) -> None:
        if self.config is None:
            return
        self.config.last_player_paused = paused
        self._save_config()

    def _attempt_resume_seek(self, seconds: int, retries_remaining: int) -> None:
        if hasattr(self.video, "can_seek") and not self.video.can_seek():
            if retries_remaining > 0:
                QTimer.singleShot(
                    300,
                    lambda: self._attempt_resume_seek(seconds, retries_remaining=retries_remaining - 1),
                )
                return
            self._append_log("恢复播放失败: 媒体尚未进入可跳转状态")
            return
        try:
            self.video.seek(seconds)
        except Exception as exc:
            if retries_remaining > 0:
                QTimer.singleShot(
                    300,
                    lambda: self._attempt_resume_seek(seconds, retries_remaining=retries_remaining - 1),
                )
                return
            self._append_log(f"恢复播放失败: {exc}")

    def report_progress(self) -> None:
        if self.session is None:
            return
        try:
            position_seconds = self.video.position_seconds()
            self.session.opening_seconds = self.opening_spin.value()
            self.session.ending_seconds = self.ending_spin.value()
            self.controller.report_progress(
                self.session,
                current_index=self.current_index,
                position_seconds=position_seconds,
                speed=self.current_speed,
                opening_seconds=self.session.opening_seconds,
                ending_seconds=self.session.ending_seconds,
            )
        except Exception as exc:
            self._append_log(f"进度上报失败: {exc}")

    def _update_sidebar_visibility(self) -> None:
        self._apply_visibility_state()

    def _toggle_wide_mode(self) -> None:
        if self.wide_button.isChecked():
            self._sidebar_sizes = self.main_splitter.sizes()
            self._apply_visibility_state()
            self.main_splitter.setSizes([1, 0])
            return
        self._apply_visibility_state()
        if hasattr(self, "_sidebar_sizes"):
            self.main_splitter.setSizes(self._sidebar_sizes)

    def _seek_relative(self, seconds: int) -> None:
        try:
            self.video.seek_relative(seconds)
        except Exception as exc:
            self._append_log(f"跳转失败: {exc}")

    def _replay_current_item(self) -> None:
        if self.session is None:
            return
        self.is_playing = True
        self._update_play_button_icon()
        self.playlist.setCurrentRow(self.current_index)
        self._load_current_item(start_position_seconds=0)

    def _toggle_mute(self) -> None:
        try:
            self.video.toggle_mute()
            self._is_muted = not self._is_muted
            self._update_mute_button_icon()
        except Exception as exc:
            self._append_log(f"静音失败: {exc}")

    def _change_speed(self, text: str) -> None:
        try:
            self.current_speed = float(text.rstrip("x"))
            self.video.set_speed(self.current_speed)
        except Exception as exc:
            self._append_log(f"倍速设置失败: {exc}")

    def _change_opening_seconds(self, value: int) -> None:
        if self.session is None:
            return
        self.session.opening_seconds = value
        self.report_progress()

    def _change_ending_seconds(self, value: int) -> None:
        if self.session is None:
            return
        self.session.ending_seconds = value
        self.report_progress()

    def _reset_subtitle_combo(self) -> None:
        self.subtitle_combo.blockSignals(True)
        self.subtitle_combo.clear()
        self.subtitle_combo.addItem("自动选择", ("auto", None))
        self.subtitle_combo.setCurrentIndex(0)
        self.subtitle_combo.setEnabled(False)
        self.subtitle_combo.blockSignals(False)

    def _remember_track_preference(self, track: SubtitleTrack) -> None:
        self._subtitle_preference = SubtitlePreference(
            mode="track",
            title=track.title,
            lang=track.lang,
            is_default=track.is_default,
            is_forced=track.is_forced,
        )

    def _populate_subtitle_combo(self, tracks: list[SubtitleTrack]) -> None:
        self.subtitle_combo.blockSignals(True)
        self.subtitle_combo.clear()
        self.subtitle_combo.addItem("自动选择", ("auto", None))
        if tracks:
            self.subtitle_combo.addItem("关闭字幕", ("off", None))
            for track in tracks:
                self.subtitle_combo.addItem(track.label, ("track", track.id))
        self.subtitle_combo.setEnabled(bool(tracks))
        self.subtitle_combo.setCurrentIndex(0)
        self.subtitle_combo.blockSignals(False)

    def _apply_subtitle_preference(self) -> None:
        self.subtitle_combo.blockSignals(True)
        try:
            if self._subtitle_preference.mode == "off":
                self.video.apply_subtitle_mode("off")
                if self.subtitle_combo.count() > 1:
                    self.subtitle_combo.setCurrentIndex(1)
                return

            if self._subtitle_preference.mode == "track":
                matched_track = self._matching_track_for_preference()
                if matched_track is not None:
                    applied_track_id = self.video.apply_subtitle_mode("track", track_id=matched_track.id)
                    for index, track in enumerate(self._subtitle_tracks, start=2):
                        if track.id == applied_track_id:
                            self.subtitle_combo.setCurrentIndex(index)
                            return
                self._subtitle_preference = SubtitlePreference()

            self.video.apply_subtitle_mode("auto")
            self.subtitle_combo.setCurrentIndex(0)
        finally:
            self.subtitle_combo.blockSignals(False)

    def _subtitle_track_match_score(self, track: SubtitleTrack, preference: SubtitlePreference) -> tuple[int, int, int]:
        return (
            int(bool(preference.title) and track.title == preference.title),
            int(bool(preference.lang) and track.lang == preference.lang),
            int(track.is_forced == preference.is_forced and track.is_default == preference.is_default),
        )

    def _matching_track_for_preference(self) -> SubtitleTrack | None:
        if self._subtitle_preference.mode != "track" or not self._subtitle_tracks:
            return None
        ranked_tracks = sorted(
            self._subtitle_tracks,
            key=lambda track: self._subtitle_track_match_score(track, self._subtitle_preference),
            reverse=True,
        )
        best_track = ranked_tracks[0]
        if self._subtitle_track_match_score(best_track, self._subtitle_preference) == (0, 0, 0):
            return None
        return best_track

    def _refresh_subtitle_state(self) -> None:
        if not hasattr(self.video, "subtitle_tracks") or not hasattr(self.video, "apply_subtitle_mode"):
            self._subtitle_tracks = []
            self._subtitle_preference = SubtitlePreference()
            self._reset_subtitle_combo()
            return
        try:
            self._subtitle_tracks = self.video.subtitle_tracks()
        except Exception as exc:
            self._subtitle_tracks = []
            self._subtitle_preference = SubtitlePreference()
            self._reset_subtitle_combo()
            self._append_log(f"字幕加载失败: {exc}")
            return
        self._populate_subtitle_combo(self._subtitle_tracks)
        if not self._subtitle_tracks:
            self._subtitle_preference = SubtitlePreference()
            return
        try:
            self._apply_subtitle_preference()
        except Exception as exc:
            self._subtitle_preference = SubtitlePreference()
            self._reset_subtitle_combo()
            self._append_log(f"字幕切换失败: {exc}")

    def _change_subtitle_selection(self, index: int) -> None:
        if index < 0:
            return
        item_data = self.subtitle_combo.itemData(index)
        if item_data is None:
            return
        mode, track_id = item_data
        if mode == "auto":
            self._subtitle_preference = SubtitlePreference()
            self.video.apply_subtitle_mode("auto")
            return
        if mode == "off":
            self._subtitle_preference = SubtitlePreference(mode="off")
            self.video.apply_subtitle_mode("off")
            return
        track = next(track for track in self._subtitle_tracks if track.id == track_id)
        self._remember_track_preference(track)
        self.video.apply_subtitle_mode("track", track_id=track_id)

    def _change_volume(self, value: int) -> None:
        try:
            self.video.set_volume(value)
        except Exception as exc:
            self._append_log(f"音量设置失败: {exc}")
            return
        if self.config is not None and self.config.player_volume != value:
            self.config.player_volume = value
            self._save_config()

    def _step_volume(self, delta: int) -> None:
        value = max(self.volume_slider.minimum(), min(self.volume_slider.value() + delta, self.volume_slider.maximum()))
        self.volume_slider.setValue(value)

    def _speed_text(self, speed: float) -> str:
        return f"{speed:.2f}".rstrip("0").rstrip(".") + "x"

    def _current_speed_index(self) -> int:
        speeds = [float(self.speed_combo.itemText(index).rstrip("x")) for index in range(self.speed_combo.count())]
        return min(
            range(len(speeds)),
            key=lambda index: abs(speeds[index] - self.current_speed),
        )

    def _step_speed(self, delta: int) -> None:
        if self.speed_combo.count() == 0:
            return
        current_index = self._current_speed_index()
        new_index = max(0, min(current_index + delta, self.speed_combo.count() - 1))
        if new_index == self.speed_combo.currentIndex():
            self._change_speed(self.speed_combo.itemText(new_index))
            return
        self.speed_combo.setCurrentIndex(new_index)

    def _reset_speed(self) -> None:
        speed_index = self.speed_combo.findText("1.0x")
        if speed_index < 0:
            return
        if speed_index == self.speed_combo.currentIndex():
            self._change_speed("1.0x")
            return
        self.speed_combo.setCurrentIndex(speed_index)

    def _register_shortcuts(self) -> None:
        bindings = [
            (QKeySequence(Qt.Key.Key_Space), self.toggle_playback),
            (QKeySequence(Qt.Key.Key_Return), self.toggle_fullscreen),
            (QKeySequence(Qt.Key.Key_Enter), self.toggle_fullscreen),
            (QKeySequence("M"), self._toggle_mute),
            (QKeySequence("-"), lambda: self._step_speed(-1)),
            (QKeySequence("+"), lambda: self._step_speed(1)),
            (QKeySequence("="), self._reset_speed),
            (QKeySequence(Qt.Key.Key_Down), lambda: self._step_volume(-self._VOLUME_SHORTCUT_STEP)),
            (QKeySequence(Qt.Key.Key_Up), lambda: self._step_volume(self._VOLUME_SHORTCUT_STEP)),
            (QKeySequence(Qt.Key.Key_Left), lambda: self._seek_relative(-self._SEEK_SHORTCUT_SECONDS)),
            (QKeySequence(Qt.Key.Key_Right), lambda: self._seek_relative(self._SEEK_SHORTCUT_SECONDS)),
            (QKeySequence(Qt.Key.Key_PageUp), self.play_previous),
            (QKeySequence(Qt.Key.Key_PageDown), self.play_next),
        ]
        for sequence, handler in bindings:
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(handler)
            self._shortcut_bindings.append(shortcut)

    def _handle_slider_pressed(self) -> None:
        self._slider_dragging = True

    def _seek_from_slider(self) -> None:
        self._slider_dragging = False
        self._seek_to_position(self.progress.value())

    def _seek_to_position(self, seconds: int) -> None:
        try:
            self.video.seek(seconds)
        except Exception as exc:
            self._append_log(f"跳转失败: {exc}")

    def _sync_progress_slider(self) -> None:
        if self._slider_dragging:
            return
        duration = self.video.duration_seconds() if hasattr(self.video, "duration_seconds") else 0
        position = self.video.position_seconds()
        if duration > 0 or position > 0:
            self._video_surface_ready = True
            self.video_poster_overlay.hide()
        if (
            self.session is not None
            and self.current_index + 1 < len(self.session.playlist)
            and duration > self.opening_spin.value() + self.ending_spin.value()
            and position < duration
            and position + self.ending_spin.value() >= duration
        ):
            self.play_next()
            return
        self.progress.setMaximum(max(duration, 0))
        self.progress.setValue(max(min(position, self.progress.maximum()), 0))
        self.current_time_label.setText(self._format_time(position))
        self.duration_label.setText(self._format_time(duration))

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            if self._was_maximized_before_fullscreen:
                self.showMaximized()
            else:
                self.showNormal()
            self._apply_visibility_state()
            return
        self._was_maximized_before_fullscreen = self.isMaximized()
        self.showFullScreen()
        self._apply_visibility_state()

    def _apply_visibility_state(self) -> None:
        is_fullscreen = self.isFullScreen()
        sidebar_hidden = is_fullscreen or self.wide_button.isChecked()
        self.bottom_area.setHidden(is_fullscreen)
        self.sidebar_actions_widget.setHidden(is_fullscreen)
        self.sidebar_container.setHidden(sidebar_hidden)
        self.playlist.setHidden(is_fullscreen or not self.toggle_playlist_button.isChecked())
        self.details.setHidden(is_fullscreen or not self.toggle_details_button.isChecked())

    def _format_time(self, seconds: int) -> str:
        total_seconds = max(int(seconds), 0)
        minutes, remaining_seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"
        return f"{minutes:02d}:{remaining_seconds:02d}"

    def _restore_main_splitter_state(self) -> None:
        if self.config is None or not self.config.player_main_splitter_state:
            self.main_splitter.setSizes([960, 320])
            return
        restored = self.main_splitter.restoreState(QByteArray(self.config.player_main_splitter_state))
        if not restored:
            self.main_splitter.setSizes([960, 320])

    def _persist_geometry(self) -> None:
        if self.config is None:
            return
        self.config.player_window_geometry = bytes(self.saveGeometry())
        self.config.player_main_splitter_state = bytes(self.main_splitter.saveState())
        self._save_config()

    def _quit_application(self) -> None:
        self._quit_requested = True
        if self.config is not None:
            self.config.last_active_window = "player"
        self._set_last_player_paused(not self.is_playing)
        self._restore_video_cursor()
        self._persist_geometry()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _return_to_main(self) -> None:
        try:
            self.video.pause()
        except Exception:
            pass
        self.is_playing = False
        self._restore_video_cursor()
        self._set_last_player_paused(True)
        self._update_play_button_icon()
        if self.config is not None:
            self.config.last_active_window = "main"
        self._persist_geometry()
        self.hide()
        self.closed_to_main.emit()

    def _handle_escape(self) -> None:
        if self.isFullScreen():
            self.toggle_fullscreen()
            return
        self._return_to_main()

    def toggle_playback(self) -> None:
        if self.is_playing:
            self.video.pause()
        else:
            self.video.resume()
        self.is_playing = not self.is_playing
        self._set_last_player_paused(not self.is_playing)
        self._update_play_button_icon()
        self._sync_video_cursor_autohide()

    def play_previous(self) -> None:
        if self.session is None or self.current_index <= 0:
            return
        self.report_progress()
        target_index = self.current_index - 1
        try:
            self._play_item_at_index(target_index)
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")

    def play_next(self) -> None:
        if self.session is None or self.current_index + 1 >= len(self.session.playlist):
            return
        self.report_progress()
        target_index = self.current_index + 1
        try:
            self._play_item_at_index(target_index)
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")

    def _handle_playback_finished(self) -> None:
        if self.session is None or self.current_index + 1 >= len(self.session.playlist):
            return
        self.play_next()

    def _play_clicked_item(self, item: QListWidgetItem) -> None:
        row = self.playlist.row(item)
        if row == self.current_index or self.session is None:
            return
        self.report_progress()
        try:
            self._play_item_at_index(row)
        except Exception as exc:
            self._append_log(f"播放失败: {exc}")

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            self._poster_request_id += 1
            self._video_surface_ready = False
            self.report_progress()
        finally:
            self.report_timer.stop()
            self.progress_timer.stop()
            self._restore_video_cursor()
            app = QApplication.instance()
            if self._app_event_filter_installed and app is not None:
                app.removeEventFilter(self)
                self._app_event_filter_installed = False
        self._persist_geometry()
        if not self._quit_requested and self.config is not None:
            self.config.last_active_window = "main"
            self._save_config()
            self.closed_to_main.emit()
        super().closeEvent(event)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseMove and self._belongs_to_player_window(watched):
            self._refresh_video_pointer_inside_state()
            if self.is_playing and self._video_pointer_inside:
                self._handle_video_mouse_activity()
            elif self.is_playing:
                self._restore_video_cursor(stop_timer=False, disable_native_autohide=False)
                if not self._cursor_hide_timer.isActive():
                    self._cursor_hide_timer.start()
            else:
                self._restore_video_cursor()
        if isinstance(watched, QWidget) and watched in self._video_surface_widgets():
            if event.type() == QEvent.Type.Enter:
                self._video_pointer_inside = True
                self._handle_video_mouse_activity()
            elif event.type() == QEvent.Type.MouseMove:
                self._video_pointer_inside = True
                self._handle_video_mouse_activity()
            elif event.type() == QEvent.Type.Leave:
                self._handle_video_leave()
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._handle_escape()
            event.accept()
            return
        if event.key() == Qt.Key.Key_P and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._return_to_main()
            event.accept()
            return
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            super().keyPressEvent(event)
            return
        if event.key() == Qt.Key.Key_Space:
            self.toggle_playback()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle_fullscreen()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Down:
            self._step_volume(-self._VOLUME_SHORTCUT_STEP)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Up:
            self._step_volume(self._VOLUME_SHORTCUT_STEP)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Left:
            self._seek_relative(-self._SEEK_SHORTCUT_SECONDS)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right:
            self._seek_relative(self._SEEK_SHORTCUT_SECONDS)
            event.accept()
            return
        if event.key() == Qt.Key.Key_PageUp:
            self.play_previous()
            event.accept()
            return
        if event.key() == Qt.Key.Key_PageDown:
            self.play_next()
            event.accept()
            return
        key_text = event.text().lower()
        if key_text == "m":
            self._toggle_mute()
            event.accept()
            return
        if key_text == "-":
            self._step_speed(-1)
            event.accept()
            return
        if key_text == "+":
            self._step_speed(1)
            event.accept()
            return
        if key_text == "=":
            self._reset_speed()
            event.accept()
            return
        super().keyPressEvent(event)
