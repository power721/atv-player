from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QIcon, QKeyEvent, QKeySequence, QShortcut, QMouseEvent
from PySide6.QtWidgets import QApplication, QStyleOptionSlider, QStyle
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
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

    clicked_value = Signal(int)  # Emits the value at click position

    def mousePressEvent(self, event: QMouseEvent):
        """Handle mouse press - jump to click position."""
        if event.button() == Qt.LeftButton:
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            handle_rect = self.style().subControlRect(
                QStyle.CC_Slider,
                option,
                QStyle.SC_SliderHandle,
                self,
            )

            # Press on handle: keep default QSlider drag behavior.
            if handle_rect.contains(event.position().toPoint()):
                super().mousePressEvent(event)
                return

            # Press on groove: jump directly and emit click signal.
            value = self._pixel_pos_to_value(event.position().x())
            self.setValue(value)
            self.clicked_value.emit(value)
            event.accept()
            return

        super().mousePressEvent(event)

    def _pixel_pos_to_value(self, pos: int) -> int:
        """Convert pixel position to slider value."""
        groove_rect = self.rect()
        # Account for handle size
        handle_width = 12
        available_width = groove_rect.width() - handle_width

        if available_width <= 0:
            return self.minimum()

        # Adjust position to account for handle offset
        adjusted_pos = pos - handle_width // 2
        adjusted_pos = max(0, min(adjusted_pos, available_width))

        # Calculate value
        value_range = self.maximum() - self.minimum()
        value = self.minimum() + int((adjusted_pos / available_width) * value_range)
        return value


class PlayerWindow(QWidget):
    closed_to_main = Signal()

    def __init__(self, controller, config=None, save_config=None) -> None:
        super().__init__()
        self.controller = controller
        self.config = config
        self._save_config = save_config or (lambda: None)
        self.session = None
        self.current_index = 0
        self.current_speed = 1.0
        self.is_playing = True
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
        self.backward_button = self._create_icon_button("previous.svg", "后退")
        self.forward_button = self._create_icon_button("next.svg", "前进")
        self.mute_button = self._create_icon_button("volume-off.svg", "静音")
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
        self.volume_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.progress = ClickableSlider(Qt.Orientation.Horizontal)
        self.progress.setFixedHeight(24)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.report_timer = QTimer(self)
        self.report_timer.setInterval(5000)
        self.report_timer.timeout.connect(self.report_progress)
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(1000)
        self.progress_timer.timeout.connect(self._sync_progress_slider)
        self._slider_dragging = False

        sidebar_actions = QHBoxLayout()
        sidebar_actions.addWidget(self.toggle_playlist_button)
        sidebar_actions.addWidget(self.toggle_details_button)

        left = QVBoxLayout()
        left.addWidget(self.video)
        left.addWidget(self.progress)
        controls = QHBoxLayout()
        controls.addWidget(self.prev_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.next_button)
        controls.addWidget(self.backward_button)
        controls.addWidget(self.forward_button)
        controls.addWidget(self.mute_button)
        controls.addWidget(self.wide_button)
        controls.addWidget(self.fullscreen_button)
        controls.addWidget(self.speed_combo)
        controls.addWidget(self.volume_slider)
        left.addLayout(controls)

        video_container = QWidget()
        video_container.setLayout(left)

        self.sidebar_splitter = QSplitter(Qt.Orientation.Vertical)
        self.sidebar_splitter.addWidget(self.playlist)
        self.sidebar_splitter.addWidget(self.details)
        self.sidebar_splitter.setChildrenCollapsible(True)

        sidebar_layout = QVBoxLayout()
        sidebar_layout.addLayout(sidebar_actions)
        sidebar_layout.addWidget(self.sidebar_splitter)
        self.sidebar_container = QWidget()
        self.sidebar_container.setLayout(sidebar_layout)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.addWidget(video_container)
        self.main_splitter.addWidget(self.sidebar_container)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 1)
        if self.config and self.config.player_main_splitter_state:
            self.main_splitter.restoreState(QByteArray(self.config.player_main_splitter_state))

        layout = QHBoxLayout(self)
        layout.addWidget(self.main_splitter)

        self.play_button.clicked.connect(self.toggle_playback)
        self.prev_button.clicked.connect(self.play_previous)
        self.next_button.clicked.connect(self.play_next)
        self.backward_button.clicked.connect(lambda: self._seek_relative(-10))
        self.forward_button.clicked.connect(lambda: self._seek_relative(10))
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
        self.escape_shortcut.activated.connect(self._return_to_main)
        self._update_play_button_icon()

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

    def open_session(self, session) -> None:
        self.session = session
        self.current_index = session.start_index
        self.current_speed = session.speed
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
        self.playlist.setHidden(not self.toggle_playlist_button.isChecked())
        self.details.setHidden(not self.toggle_details_button.isChecked())

    def _toggle_wide_mode(self) -> None:
        if self.wide_button.isChecked():
            self._sidebar_sizes = self.main_splitter.sizes()
            self.sidebar_container.hide()
            self.main_splitter.setSizes([1, 0])
            return
        self.sidebar_container.show()
        if hasattr(self, "_sidebar_sizes"):
            self.main_splitter.setSizes(self._sidebar_sizes)

    def _seek_relative(self, seconds: int) -> None:
        try:
            self.video.seek_relative(seconds)
        except Exception as exc:
            self.details.append(f"\n跳转失败: {exc}")

    def _toggle_mute(self) -> None:
        try:
            self.video.toggle_mute()
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

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            return
        self.showFullScreen()

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
            self._return_to_main()
            event.accept()
            return
        if event.key() == Qt.Key.Key_P and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._return_to_main()
            event.accept()
            return
        super().keyPressEvent(event)
