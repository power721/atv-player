"""B站发送弹幕输入条:快捷键抢键处理与发送闭环。

输入条打开时必须整体禁用应用级快捷键(ApplicationShortcut 会先于控件拿走
Enter/空格),Esc 经 _handle_escape 路由到关闭输入条并恢复快捷键;
窗口 keyPressEvent 里还有 Enter→全屏的第二轨,须一并守卫。
"""

import pytest

from atv_player.controllers.player_controller import PlayerSession
from atv_player.models import PlayItem, VodItem
from atv_player.ui.player_window import PlayerWindow


@pytest.fixture(autouse=True)
def prevent_real_mpv_load(monkeypatch: pytest.MonkeyPatch) -> None:
    # 同 test_player_window_ui:open_session 意外触发真实 mpv 加载会泄漏原生
    # core 线程,跨用例累积后 segfault 整个 pytest 进程
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


class FakeVideo:
    def position_seconds(self) -> int:
        return 5


def _make_window(qtbot, loader=None) -> PlayerWindow:
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)
    session = PlayerSession(
        vod=VodItem(vod_id="s1", vod_name="Show"),
        playlist=[
            PlayItem(
                title="第2集",
                url="http://example.com/2.mkv",
                vod_id="170001-62131",
            )
        ],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
        bilibili_comments_loader=loader,
    )
    window.open_session(session)
    window.video = FakeVideo()
    return window


def _return_shortcut(window: PlayerWindow):
    return next(
        shortcut
        for shortcut in window._shortcut_bindings
        if shortcut.key().toString() in ("Return", "Enter")
    )


def test_danmaku_input_open_disables_shortcuts_and_focuses_edit(qtbot) -> None:
    window = _make_window(qtbot, loader=lambda query: {})
    fullscreen_shortcut = _return_shortcut(window)
    assert fullscreen_shortcut.isEnabled()

    window._toggle_danmaku_input()

    assert not window.danmaku_input_bar.isHidden()
    assert window.focusWidget() is window.danmaku_input_edit
    assert not fullscreen_shortcut.isEnabled()

    window._handle_escape()

    assert window.danmaku_input_bar.isHidden()
    assert fullscreen_shortcut.isEnabled()


def test_danmaku_input_requires_bilibili_loader(qtbot) -> None:
    window = _make_window(qtbot, loader=None)

    window._toggle_danmaku_input()

    assert window.danmaku_input_bar.isHidden()


def test_danmaku_input_submits_with_progress(qtbot) -> None:
    seen: dict[str, object] = {}

    def loader(query: dict[str, object]) -> dict[str, object]:
        seen.update(query)
        return {"dmid": "1"}

    window = _make_window(qtbot, loader=loader)
    window._toggle_danmaku_input()
    window.danmaku_input_edit.setText("前来考古")

    window._submit_danmaku_input()

    qtbot.waitUntil(lambda: "kind" in seen, timeout=3000)
    assert seen["kind"] == "danmaku"
    assert seen["bvid"] == "170001-62131"
    assert seen["message"] == "前来考古"
    # position_seconds()=5 → progress 毫秒
    assert seen["progress"] == 5000
    assert seen["mode"] == 1


def test_danmaku_input_empty_message_shows_hint(qtbot) -> None:
    window = _make_window(qtbot, loader=lambda query: {})
    window._toggle_danmaku_input()

    window._submit_danmaku_input()

    assert window.danmaku_input_status_label.text() == "弹幕内容不能为空"
    assert not window.danmaku_input_bar.isHidden()


def test_danmaku_send_failure_keeps_input_open(qtbot) -> None:
    window = _make_window(qtbot, loader=lambda query: {})
    window._toggle_danmaku_input()
    window.danmaku_input_edit.setText("太快")

    window._handle_danmaku_send_finished(False, "", "B站返回 36703: 弹幕发送频率过快")

    assert not window.danmaku_input_bar.isHidden()
    assert "频率过快" in window.danmaku_input_status_label.text()
    assert window.danmaku_input_send_button.isEnabled()


def test_danmaku_send_success_closes_input(qtbot) -> None:
    window = _make_window(qtbot, loader=lambda query: {})
    window._toggle_danmaku_input()
    window.danmaku_input_edit.setText("hi")

    window._handle_danmaku_send_finished(True, "hi", "")

    assert window.danmaku_input_bar.isHidden()


def test_enter_in_input_edit_sends_instead_of_fullscreen(qtbot) -> None:
    """焦点在输入框时按 Enter 走真实按键分发:发送弹幕,不切全屏。

    回归:keyPressEvent 里有 Enter→全屏的第二轨实现(与 QShortcut 双轨),
    焦点不在输入框时 QShortcut 禁用挡不住它。
    """
    from PySide6.QtCore import Qt

    seen: dict[str, object] = {}

    def loader(query: dict[str, object]) -> dict[str, object]:
        seen.update(query)
        return {"dmid": "1"}

    window = _make_window(qtbot, loader=loader)
    fullscreen_calls: list[bool] = []
    window.toggle_fullscreen = lambda: fullscreen_calls.append(True)
    window._toggle_danmaku_input()
    assert window.focusWidget() is window.danmaku_input_edit

    window.danmaku_input_edit.setText("前来考古")
    qtbot.keyClick(window.danmaku_input_edit, Qt.Key.Key_Return)

    qtbot.waitUntil(lambda: "kind" in seen, timeout=3000)
    assert seen["message"] == "前来考古"
    assert fullscreen_calls == []


def test_enter_without_input_focus_does_not_toggle_fullscreen(qtbot) -> None:
    """输入条打开但焦点不在输入框(如点了视频区被 clearFocus)时,Enter 不切全屏。"""
    from PySide6.QtCore import Qt

    window = _make_window(qtbot, loader=lambda query: {})
    fullscreen_calls: list[bool] = []
    window.toggle_fullscreen = lambda: fullscreen_calls.append(True)
    window._toggle_danmaku_input()
    window.danmaku_input_edit.clearFocus()

    qtbot.keyClick(window, Qt.Key.Key_Return)

    assert fullscreen_calls == []
    assert not window.danmaku_input_bar.isHidden()
