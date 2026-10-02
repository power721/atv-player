from __future__ import annotations

import threading

import pytest

from atv_player.ui.bilibili_comments_dialog import BilibiliCommentsDialog


def _comment(rpid: str, uname: str, message: str = "内容", **extra) -> dict:
    payload = {
        "rpid": rpid,
        "uname": uname,
        "avatar": "",
        "level": 4,
        "message": message,
        "like": 12,
        "rcount": 0,
        "time_desc": "3天前发布",
        "location": "IP属地：河北",
        "top": False,
        "is_up": False,
    }
    payload.update(extra)
    return payload


def _main_payload(comments: list[dict], count: int = 962, is_end: bool = False, next_offset: str = "cursor-2") -> dict:
    return {"count": count, "is_end": is_end, "next_offset": next_offset if not is_end else "", "comments": comments}


class FakeLoader:
    """同步假 loader:记录请求,按 kind 返回预设载荷;可切换为抛错。"""

    def __init__(self, payloads: list[dict] | None = None, error: Exception | None = None) -> None:
        self.requests: list[dict] = []
        self.payloads = list(payloads or [])
        self.error = error
        self.lock = threading.Lock()

    def __call__(self, request: dict) -> dict:
        with self.lock:
            self.requests.append(dict(request))
        if self.error is not None:
            raise self.error
        with self.lock:
            if not self.payloads:
                raise AssertionError(f"unexpected loader request: {request}")
            return self.payloads.pop(0)


def _make_dialog(qtbot, loader) -> BilibiliCommentsDialog:
    dialog = BilibiliCommentsDialog("BV1xx411c7mD", loader)
    qtbot.addWidget(dialog)
    return dialog


def _wait_until_cards(qtbot, dialog: BilibiliCommentsDialog, count: int = 1) -> None:
    qtbot.waitUntil(lambda: len(dialog.cards()) >= count, timeout=5000)


def test_dialog_loads_main_comments_with_count_and_pagination(qtbot) -> None:
    loader = FakeLoader(payloads=[_main_payload([_comment("1001", "小明"), _comment("1002", "小红", like=25000)])])
    dialog = _make_dialog(qtbot, loader)

    _wait_until_cards(qtbot, dialog, 2)

    assert loader.requests[0] == {"kind": "main", "bvid": "BV1xx411c7mD", "mode": 3, "next": ""}
    assert dialog.title_label.text() == "评论 · 962条"
    assert dialog.status_label.text() == ""
    assert dialog.load_more_button.isVisibleTo(dialog)
    assert dialog.mode_hot_button.isChecked()
    card = dialog.card_by_rpid("1001")
    assert card is not None
    assert card.message_label.text() == "内容"
    card2 = dialog.card_by_rpid("1002")
    assert card2 is not None
    assert card2.like_button.text() == "👍 2.5万"


def test_dialog_mode_switch_resets_list_and_requests_new_mode(qtbot) -> None:
    loader = FakeLoader(
        payloads=[
            _main_payload([_comment("1001", "小明")]),
            _main_payload([_comment("2001", "最新小明")], count=100, is_end=True),
        ]
    )
    dialog = _make_dialog(qtbot, loader)
    _wait_until_cards(qtbot, dialog, 1)

    dialog.mode_latest_button.click()
    _wait_until_cards(qtbot, dialog, 1)
    qtbot.waitUntil(lambda: dialog.card_by_rpid("2001") is not None, timeout=5000)

    assert loader.requests[1] == {"kind": "main", "bvid": "BV1xx411c7mD", "mode": 2, "next": ""}
    assert dialog.card_by_rpid("1001") is None
    assert not dialog.load_more_button.isVisibleTo(dialog)
    assert dialog.mode_latest_button.isChecked()
    assert not dialog.mode_hot_button.isChecked()


def test_dialog_load_more_appends_next_page_with_cursor(qtbot) -> None:
    loader = FakeLoader(
        payloads=[
            _main_payload([_comment("1001", "小明")]),
            _main_payload([_comment("1002", "小红")], is_end=True),
        ]
    )
    dialog = _make_dialog(qtbot, loader)
    _wait_until_cards(qtbot, dialog, 1)

    dialog.load_more_button.click()
    qtbot.waitUntil(lambda: dialog.card_by_rpid("1002") is not None, timeout=5000)

    assert loader.requests[1]["next"] == "cursor-2"
    assert len(dialog.cards()) == 2
    assert not dialog.load_more_button.isVisibleTo(dialog)


def test_card_expands_floor_from_preview_without_request(qtbot) -> None:
    preview = [_comment("1003", "小刚"), _comment("1004", "UP主", is_up=True)]
    loader = FakeLoader(payloads=[_main_payload([_comment("1001", "小明", rcount=2, preview=preview)])])
    dialog = _make_dialog(qtbot, loader)
    _wait_until_cards(qtbot, dialog, 1)

    card = dialog.card_by_rpid("1001")
    card.toggle_reply_button.click()
    qtbot.waitUntil(lambda: len(card.floor_rows()) == 2, timeout=5000)

    assert card.floor_expanded()
    assert card.toggle_reply_button.text() == "收起 ▴"
    assert [row.comment.uname for row in card.floor_rows()] == ["小刚", "UP主"]
    # 行必须真正挂进楼中楼布局(仅 parent 构造不自动入布局,曾致展开空白)
    floor_layout = card._floor_widget.layout()
    assert all(floor_layout.indexOf(row) >= 0 for row in card.floor_rows())
    assert card.more_replies_button() is None
    assert all(request.get("kind") == "main" for request in loader.requests)

    card.toggle_reply_button.click()
    assert not card.floor_expanded()
    assert card.toggle_reply_button.text() == "共2条回复 ▾"


def test_card_expands_floor_by_fetching_replies_with_pagination(qtbot) -> None:
    preview = [_comment("1003", "小刚")]
    replies_page1 = [_comment("1003", "小刚")] + [
        _comment(f"200{n}", f"层内{n}", parent_uname="小刚") for n in range(1, 3)
    ]
    loader = FakeLoader(
        payloads=[
            _main_payload([_comment("1001", "小明", rcount=32, preview=preview)]),
            {"count": 32, "page": 1, "replies": replies_page1},
            {"count": 32, "page": 2, "replies": [_comment("3001", "第三页回复")]},
        ]
    )
    dialog = _make_dialog(qtbot, loader)
    _wait_until_cards(qtbot, dialog, 1)

    card = dialog.card_by_rpid("1001")
    card.toggle_reply_button.click()
    qtbot.waitUntil(lambda: card.more_replies_button() is not None, timeout=5000)

    assert loader.requests[1] == {"kind": "replies", "bvid": "BV1xx411c7mD", "root": "1001", "page": 1}
    rows = card.floor_rows()
    assert len(rows) == 3
    assert rows[0].message_label.text() == "内容"
    assert rows[1].message_label.text() == "回复 @小刚：内容"
    floor_layout = card._floor_widget.layout()
    assert all(floor_layout.indexOf(row) >= 0 for row in rows)

    card.more_replies_button().click()
    qtbot.waitUntil(lambda: len(card.floor_rows()) == 4, timeout=5000)
    assert loader.requests[2]["page"] == 2


def test_dialog_surfaces_loader_error_and_recovers_on_mode_switch(qtbot) -> None:
    loader = FakeLoader(error=RuntimeError("评论区已关闭"))
    dialog = _make_dialog(qtbot, loader)
    qtbot.waitUntil(lambda: dialog.status_label.text() != "加载中...", timeout=5000)

    assert dialog.status_label.text() == "加载失败:评论区已关闭"
    assert not dialog.cards()

    loader.error = None
    loader.payloads = [_main_payload([_comment("1001", "小明")], is_end=True)]
    dialog.mode_latest_button.click()
    _wait_until_cards(qtbot, dialog, 1)
    assert dialog.status_label.text() == ""


def test_dialog_shows_empty_state_without_comments(qtbot) -> None:
    loader = FakeLoader(payloads=[_main_payload([], count=0, is_end=True)])
    dialog = _make_dialog(qtbot, loader)
    qtbot.waitUntil(lambda: dialog.status_label.text() != "加载中...", timeout=5000)

    assert dialog.status_label.text() == "暂无评论"
    assert not dialog.load_more_button.isVisibleTo(dialog)


def test_dialog_marks_top_and_up_comments(qtbot) -> None:
    loader = FakeLoader(
        payloads=[_main_payload([_comment("1001", "UP主", top=True, is_up=True), _comment("1002", "小明")])]
    )
    dialog = _make_dialog(qtbot, loader)
    _wait_until_cards(qtbot, dialog, 2)

    top_card = dialog.card_by_rpid("1001")
    assert "[置顶]" in top_card.meta_label.text()
    assert "[作者]" in top_card.meta_label.text()
    assert "[置顶]" not in dialog.card_by_rpid("1002").meta_label.text()


def test_dialog_ignores_stale_replies_response_after_mode_reset(qtbot) -> None:
    release_first = threading.Event()
    started_first = threading.Event()

    def slow_loader(request: dict) -> dict:
        if request.get("kind") == "replies" and request.get("root") == "1001":
            started_first.set()
            release_first.wait(timeout=5)
            return {"count": 1, "page": 1, "replies": [_comment("9001", "迟到回复")]}
        if request.get("kind") == "replies":
            return {"count": 1, "page": 1, "replies": [_comment("8001", "新楼回复")]}
        if request.get("mode") == 2:
            return _main_payload([_comment("2001", "最新小明", rcount=1)], is_end=True)
        return _main_payload([_comment("1001", "小明", rcount=1)], is_end=True)

    dialog = BilibiliCommentsDialog("BV1xx411c7mD", slow_loader)
    qtbot.addWidget(dialog)
    _wait_until_cards(qtbot, dialog, 1)

    old_card = dialog.card_by_rpid("1001")
    old_card.toggle_reply_button.click()
    assert started_first.wait(timeout=5000)
    dialog.mode_latest_button.click()
    qtbot.waitUntil(lambda: dialog.card_by_rpid("2001") is not None, timeout=5000)
    release_first.set()

    new_card = dialog.card_by_rpid("2001")
    new_card.toggle_reply_button.click()
    qtbot.waitUntil(
        lambda: bool(new_card.floor_rows()) and new_card.floor_rows()[0].comment.uname == "新楼回复", timeout=5000
    )
    # 旧 card 已随重置销毁:迟到响应仅被丢弃,不炸不串楼
    assert new_card.floor_rows()[0].comment.rpid == "8001"


@pytest.mark.parametrize("raw,expected", [("962", "962"), (12000, "1.2万"), (0, "0")])
def test_format_stat_value_formats_wan(raw, expected) -> None:
    from atv_player.ui.bilibili_comments_dialog import _format_stat_value

    assert _format_stat_value(raw) == expected


def test_card_like_button_round_trips_on_and_off(qtbot) -> None:
    loader = FakeLoader(
        payloads=[
            _main_payload([_comment("1001", "小明", like=10, liked=False)]),
            {"liked": True},
            {"liked": False},
        ]
    )
    dialog = _make_dialog(qtbot, loader)
    _wait_until_cards(qtbot, dialog, 1)

    card = dialog.card_by_rpid("1001")
    assert card.like_button.text() == "👍 10"
    card.like_button.click()
    qtbot.waitUntil(lambda: card.like_button.text() == "已赞 11", timeout=5000)

    assert loader.requests[1] == {"kind": "like", "bvid": "BV1xx411c7mD", "rpid": "1001", "on": True}
    assert card.comment.like == 11 and card.comment.liked is True

    card.like_button.click()
    qtbot.waitUntil(lambda: card.like_button.text() == "👍 10", timeout=5000)
    assert loader.requests[2]["on"] is False
    assert card.comment.like == 10 and card.comment.liked is False


def test_reply_row_like_button_requests_like(qtbot) -> None:
    preview = [_comment("1003", "小刚", like=5)]
    loader = FakeLoader(
        payloads=[
            _main_payload([_comment("1001", "小明", rcount=1, preview=preview)]),
            {"liked": True},
        ]
    )
    dialog = _make_dialog(qtbot, loader)
    _wait_until_cards(qtbot, dialog, 1)

    card = dialog.card_by_rpid("1001")
    card.toggle_reply_button.click()
    qtbot.waitUntil(lambda: bool(card.floor_rows()), timeout=5000)
    row = card.floor_rows()[0]
    row.like_button.click()
    qtbot.waitUntil(lambda: row.like_button.text() == "已赞 6", timeout=5000)

    assert loader.requests[1] == {"kind": "like", "bvid": "BV1xx411c7mD", "rpid": "1003", "on": True}


def test_like_failure_restores_button_and_surfaces_error(qtbot) -> None:
    payloads = [_main_payload([_comment("1001", "小明", like=10)])]

    def failing_like(request: dict) -> dict:
        if request.get("kind") == "like":
            raise RuntimeError("未登录 B站,请先在设置中配置 Cookie")
        return payloads.pop(0)

    dialog = BilibiliCommentsDialog("BV1xx411c7mD", failing_like)
    qtbot.addWidget(dialog)
    _wait_until_cards(qtbot, dialog, 1)

    card = dialog.card_by_rpid("1001")
    card.like_button.click()
    qtbot.waitUntil(lambda: not card.like_button.isEnabled() or "点赞失败" in dialog.status_label.text(), timeout=5000)
    qtbot.waitUntil(lambda: card.like_button.isEnabled(), timeout=5000)

    assert "点赞失败" in dialog.status_label.text()
    assert "未登录" in dialog.status_label.text()
    assert card.like_button.text() == "👍 10"
    assert card.like_button.isEnabled()


def _reply_payload(rpid: str = "9999", uname: str = "我", message: str = "我的回复") -> dict:
    return {"comment": _comment(rpid, uname, message=message)}


def test_reply_to_main_comment_appends_floor_without_fetch(qtbot) -> None:
    loader = FakeLoader(
        payloads=[
            _main_payload([_comment("1001", "小明", rcount=1, preview=[_comment("1003", "小刚")])]),
            _reply_payload(),
        ]
    )
    dialog = _make_dialog(qtbot, loader)
    _wait_until_cards(qtbot, dialog, 1)

    card = dialog.card_by_rpid("1001")
    assert not card.floor_expanded()
    card.reply_button.click()
    assert dialog._active_composer is not None
    dialog._active_composer.edit.setText("我的回复")
    dialog._active_composer.send_button.click()

    qtbot.waitUntil(lambda: dialog.status_label.text() == "回复成功", timeout=5000)

    assert loader.requests[1] == {
        "kind": "reply", "bvid": "BV1xx411c7mD", "root": "1001", "parent": "1001", "message": "我的回复",
    }
    # 未展开过的楼中楼直接本地展开:预览行 + 新回复行,免请求
    assert card.floor_expanded()
    assert [row.comment.rpid for row in card.floor_rows()] == ["1003", "9999"]
    floor_layout = card._floor_widget.layout()
    assert all(floor_layout.indexOf(row) >= 0 for row in card.floor_rows())
    assert card.comment.rcount == 2
    assert card.toggle_reply_button.text() == "收起 ▴"
    assert dialog._active_composer is None


def test_reply_inside_floor_targets_parent_row(qtbot) -> None:
    preview = [_comment("1003", "小刚")]
    loader = FakeLoader(
        payloads=[
            _main_payload([_comment("1001", "小明", rcount=1, preview=preview)]),
            _reply_payload(rpid="8888", message="层内回你"),
        ]
    )
    dialog = _make_dialog(qtbot, loader)
    _wait_until_cards(qtbot, dialog, 1)

    card = dialog.card_by_rpid("1001")
    card.toggle_reply_button.click()
    qtbot.waitUntil(lambda: bool(card.floor_rows()), timeout=5000)
    row = card.floor_rows()[0]
    row.reply_button.click()
    dialog._active_composer.edit.setText("层内回你")
    dialog._active_composer.send_button.click()

    qtbot.waitUntil(lambda: dialog.status_label.text() == "回复成功", timeout=5000)

    assert loader.requests[1] == {
        "kind": "reply", "bvid": "BV1xx411c7mD", "root": "1001", "parent": "1003", "message": "层内回你",
    }
    new_row = card.floor_rows()[-1]
    assert new_row.comment.rpid == "8888"
    assert new_row.message_label.text() == "回复 @小刚：层内回你"


def test_reply_failure_keeps_composer_with_message(qtbot) -> None:
    payloads = [_main_payload([_comment("1001", "小明", rcount=0)])]

    def failing_reply(request: dict) -> dict:
        if request.get("kind") == "reply":
            raise RuntimeError("评论内容包含敏感信息")
        return payloads.pop(0)

    dialog = BilibiliCommentsDialog("BV1xx411c7mD", failing_reply)
    qtbot.addWidget(dialog)
    _wait_until_cards(qtbot, dialog, 1)

    card = dialog.card_by_rpid("1001")
    card.reply_button.click()
    composer = dialog._active_composer
    composer.edit.setText("说点什么")
    composer.send_button.click()
    qtbot.waitUntil(lambda: "回复失败" in dialog.status_label.text(), timeout=5000)

    assert "敏感信息" in dialog.status_label.text()
    assert dialog._active_composer is composer
    assert not composer.send_button.isEnabled() or composer.send_button.text() == "发送"
    qtbot.waitUntil(lambda: composer.send_button.isEnabled(), timeout=5000)
    assert composer.message() == "说点什么"


def test_reply_composer_is_singleton_across_targets(qtbot) -> None:
    loader = FakeLoader(payloads=[_main_payload([_comment("1001", "小明"), _comment("1002", "小红")])])
    dialog = _make_dialog(qtbot, loader)
    _wait_until_cards(qtbot, dialog, 2)

    dialog.card_by_rpid("1001").reply_button.click()
    first = dialog._active_composer
    dialog.card_by_rpid("1002").reply_button.click()

    assert dialog._active_composer is not first
