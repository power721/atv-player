from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import shiboken6
from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from atv_player.ui.async_guard import AsyncGuardMixin
from atv_player.ui.poster_loader import load_remote_poster_image, poster_load_slot
from atv_player.ui.theme import current_tokens
from atv_player.ui.window_chrome import ThemedDialogBase

# loader 请求/响应协议(由 main_window 用 ApiClient 实现,对话框只认可调用对象):
#   {"kind": "main", "bvid": str, "mode": 3, "next": ""} ->
#       {"count": int, "is_end": bool, "next_offset": str, "comments": [评论dict]}
#   {"kind": "replies", "bvid": str, "root": "rpid", "page": 1} ->
#       {"count": int, "page": int, "replies": [评论dict]}
#   {"kind": "like", "bvid": str, "rpid": str, "on": bool} -> {"liked": bool}
#   {"kind": "reply", "bvid": str, "root": str, "parent": str, "message": str} ->
#       {"comment": 新评论dict}
#   {"kind": "post", "bvid": str, "message": str} -> {"comment": 新评论dict}
# 评论dict字段(后端 /bilibili/{token}/comments 精简输出):
#   rpid/uname/avatar/level/message/like/rcount/ctime/time_desc/location/top/is_up/liked/parent_uname/preview
CommentsLoader = Callable[[dict[str, object]], dict[str, object]]

_AVATAR_SIZE = 36


@dataclass
class BilibiliComment:
    rpid: str = ""
    uname: str = ""
    avatar: str = ""
    level: int = 0
    message: str = ""
    like: int = 0
    rcount: int = 0
    ctime: int = 0
    time_desc: str = ""
    location: str = ""
    top: bool = False
    is_up: bool = False
    liked: bool = False
    parent_uname: str = ""
    preview: list[BilibiliComment] = field(default_factory=list)


def parse_bilibili_comment(payload: object) -> BilibiliComment:
    if not isinstance(payload, dict):
        return BilibiliComment()
    preview_raw = payload.get("preview")
    preview = [parse_bilibili_comment(child) for child in preview_raw] if isinstance(preview_raw, list) else []
    return BilibiliComment(
        rpid=str(payload.get("rpid") or "").strip(),
        uname=str(payload.get("uname") or "").strip(),
        avatar=str(payload.get("avatar") or "").strip(),
        level=int(payload.get("level") or 0),
        message=str(payload.get("message") or "").strip(),
        like=int(payload.get("like") or 0),
        rcount=int(payload.get("rcount") or 0),
        ctime=int(payload.get("ctime") or 0),
        time_desc=str(payload.get("time_desc") or "").strip(),
        location=str(payload.get("location") or "").strip(),
        top=bool(payload.get("top")),
        is_up=bool(payload.get("is_up")),
        liked=bool(payload.get("liked")),
        parent_uname=str(payload.get("parent_uname") or "").strip(),
        preview=preview,
    )


def _format_stat_value(raw_value: object) -> str:
    try:
        numeric_value = float(raw_value)
    except (TypeError, ValueError):
        return str(raw_value or "")
    if numeric_value >= 10000:
        return f"{numeric_value / 10000:.1f}万"
    if numeric_value.is_integer():
        return str(int(numeric_value))
    return str(raw_value)


def _circular_pixmap(image: QImage, size: int) -> QPixmap:
    source = QPixmap.fromImage(image).scaled(
        size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    clip = QPainterPath()
    clip.addEllipse(0, 0, size, size)
    painter.setClipPath(clip)
    painter.drawPixmap(0, 0, source)
    painter.end()
    return pixmap


def _format_ctime(ctime: int) -> str:
    """完整时间(本地时区):2026-07-13 11:31;ctime 缺失返回空回退上游相对文案。"""
    if ctime <= 0:
        return ""
    return datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %H:%M")


def _meta_html(comment: BilibiliComment) -> str:
    tokens = current_tokens()
    parts: list[str] = []
    name_color = tokens.accent if comment.is_up else tokens.text_primary
    parts.append(f'<span style="font-weight:600; color:{name_color};">{comment.uname}</span>')
    if comment.top:
        parts.append(f'<span style="color:{tokens.accent};">[置顶]</span>')
    if comment.is_up:
        parts.append(f'<span style="color:{tokens.accent};">[作者]</span>')
    if comment.level > 0:
        parts.append(f'<span style="color:{tokens.text_secondary};">Lv{comment.level}</span>')
    time_text = _format_ctime(comment.ctime) or comment.time_desc
    if time_text:
        parts.append(f'<span style="color:{tokens.text_secondary};">{time_text}</span>')
    if comment.location:
        parts.append(f'<span style="color:{tokens.text_secondary};">{comment.location}</span>')
    return " ".join(parts)


class _LoaderSignals(QObject):
    succeeded = Signal(int, object, object)  # epoch, payload, context
    failed = Signal(int, str, object)  # epoch, error message, context


class _AvatarSignals(QObject):
    loaded = Signal(object, object)  # QLabel, QImage


class _LikeButton(QPushButton):
    """评论点赞按钮(主评论与楼中楼行共用):点赞中禁用防连点,状态文案由 dialog 回填。"""

    def __init__(self, comment: BilibiliComment, dialog: BilibiliCommentsDialog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.comment = comment
        self.setObjectName("bilibiliCommentLikeButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setCheckable(True)
        self.clicked.connect(lambda _checked=False: dialog.request_like(self))
        self.refresh_state()

    def refresh_state(self) -> None:
        count_text = _format_stat_value(self.comment.like) if self.comment.like else "赞"
        self.setText(f"已赞 {count_text}" if self.comment.liked else f"👍 {count_text}")
        self.setChecked(self.comment.liked)
        self.setEnabled(True)


class _ReplyComposer(QFrame):
    """行内回复输入区:输入框(Enter 发送)+发送/取消;发送中置忙防连点。

    cancellable=False 用于对话框顶部常驻的发表框(无取消按钮)。
    """

    submitted = Signal(str)  # message
    cancelled = Signal()

    def __init__(self, placeholder: str, parent: QWidget | None = None, cancellable: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("bilibiliReplyComposer")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText(placeholder)
        self.edit.returnPressed.connect(self._emit_submitted)
        self.send_button = QPushButton("发送", self)
        self.send_button.setObjectName("bilibiliReplySendButton")
        self.send_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_button.clicked.connect(self._emit_submitted)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.send_button)
        if cancellable:
            cancel_button = QPushButton("取消", self)
            cancel_button.setObjectName("bilibiliReplyCancelButton")
            cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
            cancel_button.clicked.connect(self.cancelled.emit)
            layout.addWidget(cancel_button)

    def message(self) -> str:
        return self.edit.text().strip()

    def set_busy(self, busy: bool) -> None:
        self.edit.setReadOnly(busy)
        self.send_button.setEnabled(not busy)
        self.send_button.setText("发送中..." if busy else "发送")

    def focus_input(self) -> None:
        self.edit.setFocus()

    def _emit_submitted(self) -> None:
        text = self.message()
        if text and not self.edit.isReadOnly():
            self.submitted.emit(text)


class _ReplyRow(QWidget):
    """楼中楼单行:昵称行 + 「回复 @xxx：内容」正文 + 点赞/回复。"""

    def __init__(
        self,
        comment: BilibiliComment,
        dialog: BilibiliCommentsDialog,
        card: _CommentCard,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.comment = comment
        self._dialog = dialog
        self.card = card
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(1)
        self.meta_label = QLabel(_meta_html(comment))
        self.meta_label.setTextFormat(Qt.TextFormat.RichText)
        self.meta_label.setWordWrap(True)
        message_parts = []
        if comment.parent_uname:
            message_parts.append(f"回复 @{comment.parent_uname}：")
        message_parts.append(comment.message)
        self.message_label = QLabel("".join(message_parts))
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.meta_label)
        layout.addWidget(self.message_label)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(16)
        self.like_button = _LikeButton(comment, dialog, self)
        actions.addWidget(self.like_button)
        self.reply_button = QPushButton("回复")
        self.reply_button.setObjectName("bilibiliCommentReplyButton")
        self.reply_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reply_button.setFlat(True)
        self.reply_button.clicked.connect(
            lambda _checked=False: dialog.show_reply_composer(card, self)
        )
        actions.addWidget(self.reply_button)
        actions.addStretch(1)
        layout.addLayout(actions)


class _CommentCard(QFrame):
    replies_requested = Signal(object, int)  # card, page

    def __init__(self, comment: BilibiliComment, dialog: BilibiliCommentsDialog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.comment = comment
        self._dialog = dialog
        self._floor_widget: QWidget | None = None
        self._floor_expanded = False
        self._floor_rows: list[_ReplyRow] = []
        self._floor_pages = 0
        self._floor_complete = False
        self._floor_status_label: QLabel | None = None
        self._more_replies_button: QPushButton | None = None
        self.setObjectName("bilibiliCommentCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)
        self.avatar_label = QLabel(comment.uname[:1] if comment.uname else "")
        self.avatar_label.setFixedSize(_AVATAR_SIZE, _AVATAR_SIZE)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(self.avatar_label)

        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(2)
        self.meta_label = QLabel(_meta_html(comment))
        self.meta_label.setTextFormat(Qt.TextFormat.RichText)
        self.meta_label.setWordWrap(True)
        self.message_label = QLabel(comment.message)
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content.addWidget(self.meta_label)
        content.addWidget(self.message_label)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(16)
        self.like_button = _LikeButton(comment, dialog, self)
        footer.addWidget(self.like_button)
        self.toggle_reply_button = QPushButton(self._toggle_button_text())
        self.toggle_reply_button.setObjectName("bilibiliCommentRepliesButton")
        self.toggle_reply_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_reply_button.setFlat(True)
        self.toggle_reply_button.setVisible(comment.rcount > 0)
        self.toggle_reply_button.clicked.connect(self._toggle_floor)
        footer.addWidget(self.toggle_reply_button)
        self.reply_button = QPushButton("回复")
        self.reply_button.setObjectName("bilibiliCommentReplyButton")
        self.reply_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reply_button.setFlat(True)
        self.reply_button.clicked.connect(
            lambda _checked=False: dialog.show_reply_composer(self, None)
        )
        footer.addWidget(self.reply_button)
        footer.addStretch(1)
        content.addLayout(footer)

        body.addLayout(content, 1)
        layout.addLayout(body)

        dialog.start_avatar_load(self.avatar_label, comment.avatar)

    # --- 楼中楼展开状态 -------------------------------------------------

    def floor_expanded(self) -> bool:
        return self._floor_expanded

    def floor_rows(self) -> list[_ReplyRow]:
        return list(self._floor_rows)

    def more_replies_button(self) -> QPushButton | None:
        return self._more_replies_button

    def apply_replies(self, replies: list[BilibiliComment], page: int, count: int) -> None:
        floor = self._floor_widget
        if floor is None:
            return
        existing = {row.comment.rpid for row in self._floor_rows}
        for reply in replies:
            if reply.rpid and reply.rpid not in existing:
                self._append_floor_row(reply)
                existing.add(reply.rpid)
        self._floor_pages = max(self._floor_pages, page)
        self._floor_complete = len(self._floor_rows) >= max(count, self.comment.rcount) or not replies
        self._set_floor_status("")
        self._sync_more_button()
        self.toggle_reply_button.setText(self._toggle_button_text())

    def append_own_reply(self, reply: BilibiliComment) -> None:
        """本地插入自己刚发出的回复:楼中楼未展开则免请求直接展开(先补预览行)。"""
        self.comment.rcount += 1
        if self._floor_widget is None:
            self._floor_widget = self._create_floor_widget()
            self.layout().addWidget(self._floor_widget)
            self._floor_expanded = True
            for preview in self.comment.preview:
                self._append_floor_row(preview)
        else:
            self._floor_widget.setVisible(True)
            self._floor_expanded = True
        self._append_floor_row(reply)
        self._floor_complete = len(self._floor_rows) >= self.comment.rcount
        self._set_floor_status("")
        self._sync_more_button()
        self.toggle_reply_button.setVisible(True)
        self.toggle_reply_button.setText(self._toggle_button_text())

    def _toggle_button_text(self) -> str:
        if self.floor_expanded():
            return "收起 ▴"
        return f"共{_format_stat_value(self.comment.rcount)}条回复 ▾"

    def _toggle_floor(self) -> None:
        if self._floor_widget is None:
            self._floor_widget = self._create_floor_widget()
            self.layout().addWidget(self._floor_widget)
            if len(self.comment.preview) >= max(self.comment.rcount, 1):
                # 预览已覆盖全部子回复,直接展开免请求
                for reply in self.comment.preview:
                    self._append_floor_row(reply)
                self._floor_complete = True
            else:
                self._set_floor_status("加载中...")
                self.replies_requested.emit(self, 1)
        else:
            self._floor_widget.setVisible(not self._floor_expanded)
        self._floor_expanded = not self._floor_expanded
        self.toggle_reply_button.setText(self._toggle_button_text())

    def _append_floor_row(self, reply: BilibiliComment) -> None:
        # parent 构造不会自动入布局,必须显式 addWidget 否则行不显示
        floor = self._floor_widget
        if floor is None:
            return
        row = _ReplyRow(reply, self._dialog, self, floor)
        self._floor_rows.append(row)
        floor.layout().addWidget(row)

    def _create_floor_widget(self) -> QWidget:
        floor = QFrame()
        floor.setObjectName("bilibiliCommentFloor")
        layout = QVBoxLayout(floor)
        layout.setContentsMargins(14, 6, 6, 6)
        layout.setSpacing(4)
        return floor

    def _set_floor_status(self, text: str) -> None:
        floor = self._floor_widget
        if floor is None:
            return
        if not text:
            if self._floor_status_label is not None:
                self._floor_status_label.deleteLater()
                self._floor_status_label = None
            if not self._floor_rows and self._floor_complete:
                self._floor_status_label = QLabel("暂无回复")
                floor.layout().addWidget(self._floor_status_label)
            return
        if self._floor_status_label is None:
            self._floor_status_label = QLabel(text)
            floor.layout().addWidget(self._floor_status_label)
        else:
            self._floor_status_label.setText(text)

    def _sync_more_button(self) -> None:
        floor = self._floor_widget
        if floor is None:
            return
        if self._more_replies_button is not None:
            self._more_replies_button.deleteLater()
            self._more_replies_button = None
        if self._floor_rows and not self._floor_complete:
            button = QPushButton("展开更多回复")
            button.setObjectName("bilibiliCommentMoreButton")
            button.setFlat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            next_page = self._floor_pages + 1
            button.clicked.connect(
                lambda _checked=False, page=next_page: self.replies_requested.emit(self, page)
            )
            floor.layout().addWidget(button)
            self._more_replies_button = button


class BilibiliCommentsDialog(ThemedDialogBase, AsyncGuardMixin):
    """B站评论对话框:热门/最新切换、游标分页、楼中楼行内展开(左侧竖线)。"""

    def __init__(self, bvid: str, loader: CommentsLoader, parent: QWidget | None = None) -> None:
        super().__init__(title="评论", parent=parent, resizable=True)
        self._init_async_guard()
        self.bvid = bvid
        self._loader = loader
        self._mode = 3
        self._next_offset = ""
        self._is_end = True
        self._count: int | None = None
        self._epoch = 0
        self._avatar_semaphore = poster_load_slot()
        self._cards: list[_CommentCard] = []
        self._active_composer: _ReplyComposer | None = None
        self._signals = _LoaderSignals()
        self._connect_async_signal(self._signals.succeeded, self._handle_loaded)
        self._connect_async_signal(self._signals.failed, self._handle_failed)
        self._avatar_signals = _AvatarSignals()
        self._connect_async_signal(self._avatar_signals.loaded, self._handle_avatar_loaded)

        self.title_label = QLabel("评论")
        self.title_label.setObjectName("bilibiliCommentsTitle")
        self.mode_hot_button = QPushButton("热门")
        self.mode_latest_button = QPushButton("最新")
        for button, mode in ((self.mode_hot_button, 3), (self.mode_latest_button, 2)):
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, next_mode=mode: self._set_mode(next_mode))
        self.mode_hot_button.setChecked(True)

        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.mode_hot_button)
        header.addWidget(self.mode_latest_button)

        # 顶部常驻发表框:直接评论视频(root/parent 空)
        self.post_composer = _ReplyComposer("发表评论...", self, cancellable=False)
        self.post_composer.submitted.connect(self._submit_post)

        self.status_label = QLabel("")
        self.status_label.setObjectName("bilibiliCommentsStatus")

        self.comments_widget = QWidget()
        self.comments_layout = QVBoxLayout(self.comments_widget)
        self.comments_layout.setContentsMargins(0, 0, 0, 0)
        self.comments_layout.setSpacing(12)
        self.comments_layout.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.comments_widget)

        self.load_more_button = QPushButton("加载更多")
        self.load_more_button.setObjectName("bilibiliCommentsLoadMore")
        self.load_more_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.load_more_button.clicked.connect(lambda _checked=False: self._load_main(reset=False))
        self.load_more_button.setVisible(False)

        layout = QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addWidget(self.post_composer)
        layout.addWidget(self.status_label)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.load_more_button)
        self._window_chrome_content_layout.addLayout(layout)
        self.resize(760, 760)
        self._apply_theme()
        self._load_main(reset=True)

    # --- 对外查询(测试/接线) -------------------------------------------

    def cards(self) -> list[_CommentCard]:
        return list(self._cards)

    def card_by_rpid(self, rpid: str) -> _CommentCard | None:
        for card in self._cards:
            if card.comment.rpid == rpid:
                return card
        return None

    def start_avatar_load(self, label: QLabel, url: str) -> None:
        if not url:
            return

        def load() -> None:
            self._avatar_semaphore.acquire()
            try:
                image = load_remote_poster_image(url, QSize(_AVATAR_SIZE * 2, _AVATAR_SIZE * 2))
                if image is not None and self._can_deliver_async_result():
                    self._avatar_signals.loaded.emit(label, image)
            finally:
                self._avatar_semaphore.release()

        threading.Thread(target=load, daemon=True).start()

    # --- 数据加载 -------------------------------------------------------

    def _set_mode(self, mode: int) -> None:
        checked = self.mode_hot_button if mode == 3 else self.mode_latest_button
        other = self.mode_latest_button if mode == 3 else self.mode_hot_button
        checked.setChecked(True)
        other.setChecked(False)
        if mode == self._mode:
            return
        self._mode = mode
        self._load_main(reset=True)

    def _load_main(self, reset: bool) -> None:
        if reset:
            self._epoch += 1
            self._next_offset = ""
            self._is_end = True
            self._clear_cards()
            self.status_label.setText("加载中...")
        self.load_more_button.setEnabled(False)
        request = {"kind": "main", "bvid": self.bvid, "mode": self._mode, "next": self._next_offset}
        self._start_load(request, {"kind": "main"})

    def _request_replies(self, card: _CommentCard, page: int) -> None:
        button = card.more_replies_button()
        if button is not None:
            button.setEnabled(False)
        request = {"kind": "replies", "bvid": self.bvid, "root": card.comment.rpid, "page": page}
        self._start_load(request, {"kind": "replies", "card": card, "page": page})

    def request_like(self, button: _LikeButton) -> None:
        """评论点赞/取消(主评论与楼中楼行共用);响应后回填按钮状态,防连点。"""
        button.setEnabled(False)
        turn_on = not button.comment.liked
        request = {"kind": "like", "bvid": self.bvid, "rpid": button.comment.rpid, "on": turn_on}
        self._start_load(request, {"kind": "like", "button": button, "turn_on": turn_on})

    # --- 回复评论 -------------------------------------------------------

    def show_reply_composer(self, card: _CommentCard, row: _ReplyRow | None) -> None:
        """行内回复输入区:同一时刻只保留一个;主评论的插在卡片内,楼中楼行的插在楼层末尾。"""
        self._dismiss_active_composer()
        target = row.comment if row is not None else card.comment
        composer = _ReplyComposer(f"回复 @{target.uname}：", card)
        composer.submitted.connect(
            lambda message, c=card, r=row, comp=composer: self._submit_reply(comp, c, r, message)
        )
        composer.cancelled.connect(self._dismiss_active_composer)
        self._active_composer = composer
        if row is None:
            card.layout().insertWidget(1, composer)
        else:
            floor_layout = card._floor_widget.layout() if card._floor_widget is not None else None
            if floor_layout is None:
                return
            more = card.more_replies_button()
            if more is not None:
                floor_layout.insertWidget(floor_layout.indexOf(more), composer)
            else:
                floor_layout.addWidget(composer)
        composer.focus_input()

    def _dismiss_active_composer(self) -> None:
        composer = self._active_composer
        self._active_composer = None
        if composer is not None and shiboken6.isValid(composer):
            composer.deleteLater()

    def _submit_reply(
        self, composer: _ReplyComposer, card: _CommentCard, row: _ReplyRow | None, message: str
    ) -> None:
        composer.set_busy(True)
        parent_comment = row.comment if row is not None else card.comment
        request = {
            "kind": "reply",
            "bvid": self.bvid,
            "root": card.comment.rpid,
            "parent": parent_comment.rpid,
            "message": message,
        }
        self._start_load(request, {"kind": "reply", "card": card, "row": row, "composer": composer})

    def _submit_post(self, message: str) -> None:
        """顶部发表框:直接评论视频;成功后新评论插到列表首。"""
        self.post_composer.set_busy(True)
        request = {"kind": "post", "bvid": self.bvid, "message": message}
        self._start_load(request, {"kind": "post"})

    def _handle_post_loaded(self, payload: dict[str, object]) -> None:
        comment = parse_bilibili_comment(payload.get("comment"))
        if not comment.rpid:
            self.status_label.setText("评论失败:上游未返回新评论")
            self.post_composer.set_busy(False)
            return
        card = _CommentCard(comment, self, self.comments_widget)
        card.replies_requested.connect(self._request_replies)
        self._cards.insert(0, card)
        self.comments_layout.insertWidget(0, card)
        if self._count is not None:
            self._count += 1
            self.title_label.setText(f"评论 · {_format_stat_value(self._count)}条")
        self.status_label.setText("")
        self.post_composer.edit.clear()
        self.post_composer.set_busy(False)
        self.post_composer.focus_input()

    def _handle_reply_loaded(self, payload: dict[str, object], context: dict[str, object]) -> None:
        card = context.get("card")
        row = context.get("row")
        composer = context.get("composer")
        if not isinstance(card, _CommentCard) or not shiboken6.isValid(card):
            return
        new_reply = parse_bilibili_comment(payload.get("comment"))
        if not new_reply.rpid:
            self.status_label.setText("回复失败:上游未返回新评论")
            if isinstance(composer, _ReplyComposer) and shiboken6.isValid(composer):
                composer.set_busy(False)
            return
        if isinstance(row, _ReplyRow) and shiboken6.isValid(row):
            new_reply.parent_uname = row.comment.uname
        card.append_own_reply(new_reply)
        self._active_composer = None
        if isinstance(composer, _ReplyComposer) and shiboken6.isValid(composer):
            composer.deleteLater()
        self.status_label.setText("回复成功")

    def _start_load(self, request: dict[str, object], context: dict[str, object]) -> None:
        epoch = self._epoch

        def work() -> None:
            try:
                payload = self._loader(request)
                self._signals.succeeded.emit(epoch, payload, context)
            except Exception as exc:  # noqa: BLE001 - loader 网络错误统一转文案
                self._signals.failed.emit(epoch, str(exc) or type(exc).__name__, context)

        threading.Thread(target=work, daemon=True).start()

    def _handle_loaded(self, epoch: int, payload: object, context: object) -> None:
        if epoch != self._epoch or not isinstance(payload, dict):
            return
        if isinstance(context, dict) and context.get("kind") == "replies":
            card = context.get("card")
            if not isinstance(card, _CommentCard) or not shiboken6.isValid(card):
                return
            replies = [parse_bilibili_comment(entry) for entry in payload.get("replies") or []]
            card.apply_replies(replies, int(context.get("page") or 1), int(payload.get("count") or 0))
            return
        if isinstance(context, dict) and context.get("kind") == "like":
            button = context.get("button")
            if not isinstance(button, _LikeButton) or not shiboken6.isValid(button):
                return
            liked = bool(payload.get("liked"))
            comment = button.comment
            if liked != comment.liked:
                comment.like += 1 if liked else -1
                comment.like = max(0, comment.like)
            comment.liked = liked
            button.refresh_state()
            return
        if isinstance(context, dict) and context.get("kind") == "reply":
            self._handle_reply_loaded(payload, context)
            return
        if isinstance(context, dict) and context.get("kind") == "post":
            self._handle_post_loaded(payload)
            return
        self._apply_main_payload(payload)

    def _handle_failed(self, epoch: int, message: str, context: object) -> None:
        if epoch != self._epoch:
            return
        if isinstance(context, dict) and context.get("kind") == "like":
            button = context.get("button")
            if isinstance(button, _LikeButton) and shiboken6.isValid(button):
                button.refresh_state()
            self.status_label.setText(f"点赞失败:{message}")
            return
        if isinstance(context, dict) and context.get("kind") == "reply":
            composer = context.get("composer")
            if isinstance(composer, _ReplyComposer) and shiboken6.isValid(composer):
                composer.set_busy(False)
                composer.focus_input()
            self.status_label.setText(f"回复失败:{message}")
            return
        if isinstance(context, dict) and context.get("kind") == "post":
            self.post_composer.set_busy(False)
            self.post_composer.focus_input()
            self.status_label.setText(f"评论失败:{message}")
            return
        if not self._cards:
            self.status_label.setText(f"加载失败:{message}")
            self.load_more_button.setVisible(False)
            return
        self.status_label.setText(f"加载更多失败:{message}")
        self.load_more_button.setEnabled(True)

    def _apply_main_payload(self, payload: dict[str, object]) -> None:
        if isinstance(payload.get("count"), int):
            self._count = int(payload["count"])
        self._is_end = bool(payload.get("is_end"))
        self._next_offset = str(payload.get("next_offset") or "")
        self.title_label.setText(f"评论 · {_format_stat_value(self._count)}条" if self._count is not None else "评论")
        comments = [parse_bilibili_comment(entry) for entry in payload.get("comments") or []]
        if comments or self._cards:
            if self.status_label.text() == "加载中..." or self.status_label.text().startswith("加载更多失败"):
                self.status_label.setText("")
        else:
            self.status_label.setText("暂无评论")
        for comment in comments:
            card = _CommentCard(comment, self, self.comments_widget)
            card.replies_requested.connect(self._request_replies)
            self._cards.append(card)
            self.comments_layout.insertWidget(self.comments_layout.count() - 1, card)
        self.load_more_button.setVisible(bool(self._cards) and not self._is_end)
        self.load_more_button.setEnabled(True)

    def _clear_cards(self) -> None:
        self._cards = []
        self._active_composer = None
        while self.comments_layout.count() > 1:
            item = self.comments_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    # --- 主题 -----------------------------------------------------------

    def _apply_theme(self) -> None:
        tokens = current_tokens()
        self.setStyleSheet(
            f"""
            QLabel#bilibiliCommentsTitle {{
                color: {tokens.text_primary};
                font-size: 16px;
                font-weight: 700;
            }}
            QLabel#bilibiliCommentsStatus {{
                color: {tokens.text_secondary};
                font-size: 13px;
            }}
            QFrame#bilibiliCommentCard {{
                background: {tokens.panel_bg};
                border: 1px solid {tokens.border_subtle};
                border-radius: 8px;
                padding: 10px;
            }}
            QFrame#bilibiliCommentFloor {{
                background: {tokens.panel_alt_bg};
                border-left: 2px solid {tokens.accent};
                border-radius: 4px;
            }}
            QFrame#bilibiliCommentCard QLabel {{
                color: {tokens.text_primary};
                font-size: 13px;
                background: transparent;
                border: none;
            }}
            QFrame#bilibiliCommentFloor QLabel {{
                color: {tokens.text_primary};
                font-size: 12px;
                background: transparent;
                border: none;
            }}
            QPushButton#bilibiliCommentRepliesButton, QPushButton#bilibiliCommentMoreButton {{
                color: {tokens.accent};
                background: transparent;
                border: none;
                padding: 2px 4px;
                font-size: 12px;
                font-weight: 600;
                text-align: left;
            }}
            QPushButton#bilibiliCommentRepliesButton:hover, QPushButton#bilibiliCommentMoreButton:hover {{
                color: {tokens.accent_hover};
            }}
            QPushButton#bilibiliCommentLikeButton {{
                color: {tokens.text_secondary};
                background: transparent;
                border: none;
                padding: 2px 4px;
                font-size: 12px;
                text-align: left;
            }}
            QPushButton#bilibiliCommentLikeButton:hover {{
                color: {tokens.text_primary};
            }}
            QPushButton#bilibiliCommentLikeButton:checked {{
                color: {tokens.accent};
                font-weight: 600;
            }}
            QPushButton#bilibiliCommentLikeButton:disabled {{
                color: {tokens.text_secondary};
            }}
            QPushButton#bilibiliCommentReplyButton {{
                color: {tokens.text_secondary};
                background: transparent;
                border: none;
                padding: 2px 4px;
                font-size: 12px;
                text-align: left;
            }}
            QPushButton#bilibiliCommentReplyButton:hover {{
                color: {tokens.text_primary};
            }}
            QFrame#bilibiliReplyComposer QLineEdit {{
                color: {tokens.text_primary};
                background: {tokens.panel_bg};
                border: 1px solid {tokens.border_subtle};
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 13px;
            }}
            QFrame#bilibiliReplyComposer QLineEdit:focus {{
                border-color: {tokens.accent};
            }}
            QFrame#bilibiliReplyComposer QPushButton#bilibiliReplySendButton {{
                color: {tokens.text_primary};
                background: {tokens.accent};
                border: none;
                border-radius: 6px;
                padding: 4px 12px;
                font-size: 12px;
                font-weight: 600;
            }}
            QFrame#bilibiliReplyComposer QPushButton#bilibiliReplySendButton:disabled {{
                background: {tokens.panel_alt_bg};
                color: {tokens.text_secondary};
            }}
            QFrame#bilibiliReplyComposer QPushButton#bilibiliReplyCancelButton {{
                color: {tokens.text_secondary};
                background: transparent;
                border: 1px solid {tokens.border_subtle};
                border-radius: 6px;
                padding: 4px 12px;
                font-size: 12px;
            }}
            QPushButton#bilibiliCommentsLoadMore {{
                color: {tokens.text_primary};
                background: {tokens.panel_bg};
                border: 1px solid {tokens.border_subtle};
                border-radius: 6px;
                padding: 6px;
            }}
            QPushButton#bilibiliCommentsLoadMore:hover {{
                border-color: {tokens.input_hover_border};
            }}
            QPushButton#bilibiliCommentsLoadMore:disabled {{
                color: {tokens.text_secondary};
            }}
            """
        )
        mode_button_qss = self._mode_button_qss()
        self.mode_hot_button.setStyleSheet(mode_button_qss)
        self.mode_latest_button.setStyleSheet(mode_button_qss)
        for card in self._cards:
            card.meta_label.setText(_meta_html(card.comment))
            card.toggle_reply_button.setText(card._toggle_button_text())
            for row in card.floor_rows():
                row.meta_label.setText(_meta_html(row.comment))

    def _mode_button_qss(self) -> str:
        tokens = current_tokens()
        return (
            f"QPushButton {{ color: {tokens.text_secondary}; background: transparent;"
            f" border: 1px solid {tokens.border_subtle}; border-radius: 6px; padding: 3px 12px;"
            f" font-size: 12px; }}\n"
            f"QPushButton:checked {{ color: {tokens.text_primary}; background: {tokens.panel_alt_bg};"
            f" border-color: {tokens.accent}; font-weight: 600; }}\n"
            f"QPushButton:hover {{ border-color: {tokens.input_hover_border}; }}"
        )

    def _handle_avatar_loaded(self, label: object, image: object) -> None:
        if not isinstance(label, QLabel) or not shiboken6.isValid(label) or not isinstance(image, QImage):
            return
        label.setText("")
        label.setPixmap(_circular_pixmap(image, _AVATAR_SIZE))
