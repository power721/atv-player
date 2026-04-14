from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QIcon, QKeyEvent, QKeySequence, QMouseEvent, QShortcut
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionSlider
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from atv_player.player.mpv_widget import MpvWidget


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


class PlayerWindow(QWidget):
    closed_to_main = Signal()
    _SEEK_SHORTCUT_SECONDS = 15
    _VOLUME_SHORTCUT_STEP = 5

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
        self._quit_requested = False
        self.setWindowTitle("alist-tvbox 播放器")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(1280, 800)
        self.setMinimumSize(1000, 700)
        self._icons_dir = Path(__file__).resolve().parent.parent / "icons"
        if self.config and self.config.player_window_geometry:
            self.restoreGeometry(QByteArray(self.config.player_window_geometry))

        self.video = MpvWidget(self)
        self.playlist = QListWidget()
        self.play_button = self._create_icon_button("play.svg", "播放/暂停")
        self.prev_button = self._create_icon_button("previous.svg", "上一集")
        self.next_button = self._create_icon_button("next.svg", "下一集")
        self.backward_button = self._create_icon_button("seek-backward.svg", "后退")
        self.forward_button = self._create_icon_button("seek-forward.svg", "前进")
        self.refresh_button = self._create_icon_button("refresh.svg", "重新播放")
        self.mute_button = self._create_icon_button("volume-on.svg", "静音")
        self.wide_button = self._create_icon_button("grid.svg", "宽屏")
        self.fullscreen_button = self._create_icon_button("maximize.svg", "全屏")
        self.wide_button.setCheckable(True)
        self.toggle_playlist_button = self._create_icon_button("queue.svg", "播放列表")
        self.toggle_details_button = self._create_icon_button("info.svg", "详情")
        self.toggle_playlist_button.setCheckable(True)
        self.toggle_details_button.setCheckable(True)
        self.toggle_playlist_button.setChecked(True)
        self.toggle_details_button.setChecked(True)

        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.5x", "1.0x", "1.25x", "1.5x", "2.0x"])
        self.speed_combo.setCurrentText("1.0x")

        self.current_time_label = QLabel("00:00")
        self.current_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.duration_label = QLabel("00:00")
        self.duration_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress = ClickableSlider(Qt.Orientation.Horizontal)
        self.progress.setFixedHeight(24)
        self.volume_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setMaximumWidth(180)
        self.details = QTextEdit()
        self.details.setReadOnly(True)

        self.report_timer = QTimer(self)
        self.report_timer.setInterval(5000)
        self.report_timer.timeout.connect(self.report_progress)
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(1000)
        self.progress_timer.timeout.connect(self._sync_progress_slider)
        self._slider_dragging = False

        self.sidebar_actions_widget = QWidget()
        sidebar_actions = QHBoxLayout(self.sidebar_actions_widget)
        sidebar_actions.setContentsMargins(0, 0, 0, 0)
        sidebar_actions.addWidget(self.toggle_playlist_button)
        sidebar_actions.addWidget(self.toggle_details_button)

        self.bottom_area = QWidget()
        self.bottom_area.setMaximumHeight(60)
        bottom_layout = QVBoxLayout(self.bottom_area)
        self.bottom_layout = bottom_layout
        bottom_layout.setContentsMargins(0, 0, 0, 0)
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
        video_layout.addWidget(self.video)

        self.sidebar_splitter = QSplitter(Qt.Orientation.Vertical)
        self.sidebar_splitter.addWidget(self.playlist)
        self.sidebar_splitter.addWidget(self.details)
        self.sidebar_splitter.setChildrenCollapsible(True)

        sidebar_layout = QVBoxLayout()
        sidebar_layout.addWidget(self.sidebar_actions_widget)
        sidebar_layout.addWidget(self.sidebar_splitter)
        self.sidebar_container = QWidget()
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
        self.backward_button.clicked.connect(lambda: self._seek_relative(-10))
        self.forward_button.clicked.connect(lambda: self._seek_relative(10))
        self.refresh_button.clicked.connect(self._replay_current_item)
        self.mute_button.clicked.connect(self._toggle_mute)
        self.wide_button.clicked.connect(self._toggle_wide_mode)
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.speed_combo.currentTextChanged.connect(self._change_speed)
        self.volume_slider.valueChanged.connect(self._change_volume)
        self.playlist.itemDoubleClicked.connect(self._play_clicked_item)
        self.toggle_playlist_button.clicked.connect(self._update_sidebar_visibility)
        self.toggle_details_button.clicked.connect(self._update_sidebar_visibility)
        self.video.double_clicked.connect(self.toggle_fullscreen)
        self.progress.sliderPressed.connect(self._handle_slider_pressed)
        self.progress.sliderReleased.connect(self._seek_from_slider)
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

    def _create_icon_button(self, icon_name: str, tooltip: str) -> QPushButton:
        button = QPushButton("")
        button.setToolTip(tooltip)
        button.setIcon(QIcon(str(self._icons_dir / icon_name)))
        button.setIconSize(button.iconSize())
        button.setFixedHeight(28)
        return button

    def _update_play_button_icon(self) -> None:
        icon_name = "pause.svg" if self.is_playing else "play.svg"
        self.play_button.setIcon(QIcon(str(self._icons_dir / icon_name)))

    def _set_button_icon(self, button: QPushButton, icon_name: str) -> None:
        button.setIcon(QIcon(str(self._icons_dir / icon_name)))

    def _update_mute_button_icon(self) -> None:
        icon_name = "volume-off.svg" if self._is_muted else "volume-on.svg"
        self._set_button_icon(self.mute_button, icon_name)

    def open_session(self, session) -> None:
        self.session = session
        self.current_index = session.start_index
        self.current_speed = session.speed
        speed_text = self._speed_text(session.speed)
        speed_index = self.speed_combo.findText(speed_text)
        if speed_index >= 0:
            self.speed_combo.setCurrentIndex(speed_index)
        self.is_playing = True
        self._update_play_button_icon()
        self.playlist.clear()
        for item in session.playlist:
            self.playlist.addItem(QListWidgetItem(item.title))
        self.playlist.setCurrentRow(self.current_index)
        self.progress.setValue(0)
        self._load_current_item(session.start_position_seconds)
        self.report_timer.start()
        self.progress_timer.start()

    def _load_current_item(self, start_position_seconds: int = 0) -> None:
        if self.session is None:
            return
        current_item = self.session.playlist[self.current_index]
        self.details.setPlainText(
            f"标题: {self.session.vod.vod_name}\n"
            f"当前: {current_item.title}\n"
            f"URL: {current_item.url}"
        )
        try:
            self.video.load(current_item.url, start_seconds=start_position_seconds)
            self.video.set_speed(self.current_speed)
            self.video.set_volume(self.volume_slider.value())
        except Exception as exc:
            self.details.append(f"\n播放失败: {exc}")

    def _attempt_resume_seek(self, seconds: int, retries_remaining: int) -> None:
        if hasattr(self.video, "can_seek") and not self.video.can_seek():
            if retries_remaining > 0:
                QTimer.singleShot(
                    300,
                    lambda: self._attempt_resume_seek(seconds, retries_remaining=retries_remaining - 1),
                )
                return
            self.details.append("\n恢复播放失败: 媒体尚未进入可跳转状态")
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
            self.details.append(f"\n恢复播放失败: {exc}")

    def report_progress(self) -> None:
        if self.session is None:
            return
        try:
            position_seconds = self.video.position_seconds()
            self.controller.report_progress(
                self.session,
                current_index=self.current_index,
                position_seconds=position_seconds,
                speed=self.current_speed,
            )
        except Exception as exc:
            self.details.append(f"\n进度上报失败: {exc}")

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
            self.details.append(f"\n跳转失败: {exc}")

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
            self.details.append(f"\n静音失败: {exc}")

    def _change_speed(self, text: str) -> None:
        try:
            self.current_speed = float(text.rstrip("x"))
            self.video.set_speed(self.current_speed)
        except Exception as exc:
            self.details.append(f"\n倍速设置失败: {exc}")

    def _change_volume(self, value: int) -> None:
        try:
            self.video.set_volume(value)
        except Exception as exc:
            self.details.append(f"\n音量设置失败: {exc}")

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
        try:
            self.video.seek(self.progress.value())
        except Exception as exc:
            self.details.append(f"\n跳转失败: {exc}")

    def _sync_progress_slider(self) -> None:
        if self._slider_dragging:
            return
        duration = self.video.duration_seconds() if hasattr(self.video, "duration_seconds") else 0
        position = self.video.position_seconds()
        self.progress.setMaximum(max(duration, 0))
        self.progress.setValue(max(min(position, self.progress.maximum()), 0))
        self.current_time_label.setText(self._format_time(position))
        self.duration_label.setText(self._format_time(duration))

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._apply_visibility_state()
            return
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
        self._update_play_button_icon()

    def play_previous(self) -> None:
        if self.session is None or self.current_index <= 0:
            return
        self.report_progress()
        self.current_index -= 1
        self.playlist.setCurrentRow(self.current_index)
        self._load_current_item()

    def play_next(self) -> None:
        if self.session is None or self.current_index + 1 >= len(self.session.playlist):
            return
        self.report_progress()
        self.current_index += 1
        self.playlist.setCurrentRow(self.current_index)
        self._load_current_item()

    def _play_clicked_item(self, item: QListWidgetItem) -> None:
        row = self.playlist.row(item)
        if row == self.current_index or self.session is None:
            return
        self.report_progress()
        self.current_index = row
        self._load_current_item()

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            self.report_progress()
        finally:
            self.report_timer.stop()
            self.progress_timer.stop()
        self._persist_geometry()
        if not self._quit_requested and self.config is not None:
            self.config.last_active_window = "main"
            self._save_config()
            self.closed_to_main.emit()
        super().closeEvent(event)

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
