from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class MpvWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self._player = None
        self._placeholder = QLabel("mpv surface")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.addWidget(self._placeholder)

    def _ensure_player(self) -> None:
        if self._player is not None:
            return
        import mpv

        self._player = mpv.MPV(
            wid=str(int(self.winId())),
            input_default_bindings=True,
            input_vo_keyboard=True,
        )

    def load(self, url: str, pause: bool = False) -> None:
        self._ensure_player()
        self._player.play(url)
        self._player.pause = pause

    def seek(self, seconds: int) -> None:
        if self._player is None:
            return
        self._player.command("seek", seconds, "absolute")

    def set_speed(self, speed: float) -> None:
        if self._player is None:
            return
        self._player.speed = speed

    def pause(self) -> None:
        if self._player is None:
            return
        self._player.pause = True

    def resume(self) -> None:
        if self._player is None:
            return
        self._player.pause = False

    def position_seconds(self) -> int:
        if self._player is None:
            return 0
        return int(self._player.time_pos or 0)
