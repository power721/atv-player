from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from atv_player.player.mpv_widget import MpvWidget


class PlayerWindow(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.session = None
        self.current_index = 0
        self.current_speed = 1.0
        self.is_playing = True
        self.setWindowTitle("alist-tvbox 播放器")
        self.video = MpvWidget(self)
        self.playlist = QListWidget()
        self.play_button = QPushButton("播放/暂停")
        self.prev_button = QPushButton("上一集")
        self.next_button = QPushButton("下一集")
        self.progress = QSlider()
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.report_timer = QTimer(self)
        self.report_timer.setInterval(5000)
        self.report_timer.timeout.connect(self.report_progress)

        right = QVBoxLayout()
        right.addWidget(self.playlist)
        right.addWidget(self.details)

        left = QVBoxLayout()
        left.addWidget(self.video)
        left.addWidget(self.progress)
        controls = QHBoxLayout()
        controls.addWidget(self.prev_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.next_button)
        left.addLayout(controls)

        layout = QHBoxLayout(self)
        layout.addLayout(left, 3)
        layout.addLayout(right, 2)

        self.play_button.clicked.connect(self.toggle_playback)
        self.prev_button.clicked.connect(self.play_previous)
        self.next_button.clicked.connect(self.play_next)
        self.playlist.itemDoubleClicked.connect(self._play_clicked_item)

    def open_session(self, session) -> None:
        self.session = session
        self.current_index = session.start_index
        self.current_speed = session.speed
        self.is_playing = True
        self.playlist.clear()
        for item in session.playlist:
            self.playlist.addItem(QListWidgetItem(item.title))
        self.playlist.setCurrentRow(self.current_index)
        self._load_current_item(session.start_position_seconds)
        self.report_timer.start()

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
            self.video.load(current_item.url)
            if start_position_seconds:
                QTimer.singleShot(200, lambda: self.video.seek(start_position_seconds))
            self.video.set_speed(self.current_speed)
        except Exception as exc:
            self.details.append(f"\n播放失败: {exc}")

    def report_progress(self) -> None:
        if self.session is None:
            return
        self.controller.report_progress(
            self.session,
            current_index=self.current_index,
            position_seconds=self.video.position_seconds(),
            speed=self.current_speed,
        )

    def toggle_playback(self) -> None:
        if self.is_playing:
            self.video.pause()
        else:
            self.video.resume()
        self.is_playing = not self.is_playing

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
        super().closeEvent(event)
