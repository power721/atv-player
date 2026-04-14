from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class MpvWidget(QWidget):
    double_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._player = None
        self._placeholder = QLabel("mpv surface")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.addWidget(self._placeholder)

    def _create_player(self):
        import mpv

        return mpv.MPV(
            wid=str(int(self.winId())),
            input_default_bindings=True,
            input_vo_keyboard=True,
        )

    def _ensure_player(self) -> None:
        if self._player is not None and not getattr(self._player, "core_shutdown", False):
            return
        self._player = self._create_player()

    def load(self, url: str, pause: bool = False, start_seconds: int = 0) -> None:
        self._ensure_player()
        try:
            if start_seconds > 0:
                self._player.loadfile(url, start=str(start_seconds))
            else:
                self._player.play(url)
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                self._player = self._create_player()
                if start_seconds > 0:
                    self._player.loadfile(url, start=str(start_seconds))
                else:
                    self._player.play(url)
            else:
                raise
        self._player.pause = pause

    def seek(self, seconds: int) -> None:
        if self._player is None:
            return
        try:
            self._player.command("seek", seconds, "absolute")
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def seek_relative(self, seconds: int) -> None:
        if self._player is None:
            return
        try:
            self._player.command("seek", seconds, "relative")
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def can_seek(self) -> bool:
        if self._player is None:
            return False
        try:
            return bool(self._player.seekable)
        except Exception:
            return False

    def set_speed(self, speed: float) -> None:
        if self._player is None:
            return
        try:
            self._player.speed = speed
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def set_volume(self, volume: int) -> None:
        if self._player is None:
            return
        try:
            self._player.volume = volume
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def toggle_mute(self) -> None:
        if self._player is None:
            return
        try:
            self._player.mute = not bool(self._player.mute)
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def pause(self) -> None:
        if self._player is None:
            return
        try:
            self._player.pause = True
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def resume(self) -> None:
        if self._player is None:
            return
        try:
            self._player.pause = False
        except Exception:
            if getattr(self._player, "core_shutdown", False):
                return
            raise

    def position_seconds(self) -> int:
        if self._player is None:
            return 0
        try:
            return int(self._player.time_pos or 0)
        except Exception:
            return 0

    def duration_seconds(self) -> int:
        if self._player is None:
            return 0
        try:
            return int(self._player.duration or 0)
        except Exception:
            return 0

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)
