from atv_player.player.mpv_widget import MpvWidget


class FakeDeadPlayer:
    core_shutdown = True


class FakeAlivePlayer:
    def __init__(self) -> None:
        self.play_calls: list[str] = []
        self.pause = False
        self.volume = 100
        self.mute = False

    def play(self, url: str) -> None:
        self.play_calls.append(url)


def test_mpv_widget_recreates_player_when_core_is_shutdown(qtbot, monkeypatch) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = FakeDeadPlayer()

    alive = FakeAlivePlayer()
    monkeypatch.setattr(widget, "_create_player", lambda: alive)

    widget.load("http://m/1.m3u8")

    assert widget._player is alive
    assert alive.play_calls == ["http://m/1.m3u8"]


def test_mpv_widget_updates_volume_and_mute_state(qtbot) -> None:
    widget = MpvWidget()
    qtbot.addWidget(widget)
    widget._player = FakeAlivePlayer()

    widget.set_volume(35)
    widget.toggle_mute()
    widget.toggle_mute()

    assert widget._player.volume == 35
    assert widget._player.mute is False
