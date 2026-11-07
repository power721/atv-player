"""换播放会话时,旧会话仍在跑的弹幕解析应被翻代取消。

错剧/无弹幕匹配会把整部剧几十集当候选逐个下载(分钟级),用户切到新视频后
旧解析线程仍会向新会话日志刷「弹幕下载中」;open_session 需对旧会话的
danmaku_controller 调 invalidate_running_danmaku_resolves 让其在下一个
候选检查点退出。
"""

import pytest

from atv_player.controllers.player_controller import PlayerSession
from atv_player.models import PlayItem, VodItem
from atv_player.ui.player_window import PlayerWindow


@pytest.fixture(autouse=True)
def prevent_real_mpv_load(monkeypatch: pytest.MonkeyPatch) -> None:
    # 与 test_player_window_ui.py 相同的护栏:避免测试里真的创建 libmpv 核心。
    monkeypatch.setattr(
        "atv_player.player.mpv_widget.MpvWidget.load",
        lambda self, *args, **kwargs: None,
    )


class FakePlayerController:
    def report_progress(self, *args, **kwargs) -> None:
        return None

    def resolve_play_item_detail(self, session, play_item):
        return None

    def stop_playback(self, session, current_index: int) -> None:
        return None


class FakeDanmakuController:
    def __init__(self) -> None:
        self.invalidate_calls = 0

    def invalidate_running_danmaku_resolves(self) -> None:
        self.invalidate_calls += 1


def _session() -> PlayerSession:
    return PlayerSession(
        vod=VodItem(vod_id="s1", vod_name="Show"),
        playlist=[
            PlayItem(
                title="第1集",
                url="http://example.com/1.mkv",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )


def test_open_session_invalidates_previous_session_running_danmaku_resolves(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    first = _session()
    first.danmaku_controller = FakeDanmakuController()
    window.open_session(first)
    assert first.danmaku_controller.invalidate_calls == 0

    window.open_session(_session())

    assert first.danmaku_controller.invalidate_calls == 1


def test_open_session_without_previous_session_skips_invalidation(qtbot) -> None:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(_session())

    assert window.session is not None
