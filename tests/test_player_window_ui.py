from PySide6.QtCore import Qt

import atv_player.ui.player_window as player_window_module
from atv_player.ui.player_window import PlayerWindow


class FakePlayerController:
    def report_progress(self, session, current_index: int, position_seconds: int, speed: float) -> None:
        return None


def test_player_window_has_reasonable_default_size_and_horizontal_progress(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())

    qtbot.addWidget(window)
    window.show()

    assert window.width() >= 1000
    assert window.height() >= 700
    assert window.progress.orientation() == Qt.Orientation.Horizontal


def test_player_window_retries_resume_seek_when_player_is_not_ready(qtbot, monkeypatch) -> None:
    class FakeVideo:
        def __init__(self) -> None:
            self.seek_calls = 0

        def seek(self, seconds: int) -> None:
            self.seek_calls += 1
            if self.seek_calls == 1:
                raise SystemError("mpv not ready")

    scheduled_delays: list[int] = []

    def immediate_single_shot(delay: int, callback) -> None:
        scheduled_delays.append(delay)
        callback()

    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    window.video = FakeVideo()
    monkeypatch.setattr(player_window_module.QTimer, "singleShot", immediate_single_shot)

    window._attempt_resume_seek(42, retries_remaining=2)

    assert window.video.seek_calls == 2
    assert scheduled_delays == [300]
