from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import shiboken6
from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
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
#   {"kind": "main", "mode": 3, "next": ""} ->
#       {"count": int, "is_end": bool, "next_offset": str, "comments": [评论dict]}
#   {"kind": "replies", "root": "rpid", "page": 1} ->
#       {"count": int, "page": int, "replies": [评论dict]}
# 评论dict字段(后端 /bilibili/{token}/comments 精简输出):
#   rpid/uname/avatar/level/message/like/rcount/ctime/time_desc/location/top/is_up/parent_uname/preview
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
    time_desc: str = ""
    location: str = ""
    top: bool = False
    is_up: bool = False
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
        time_desc=str(payload.get("time_desc") or "").strip(),
        location=str(payload.get("location") or "").strip(),
        top=bool(payload.get("top")),
        is_up=bool(payload.get("is_up")),
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
    if comment.time_desc:
        parts.append(f'<span style="color:{tokens.text_secondary};">{comment.time_desc}</span>')
    if comment.location:
        parts.append(f'<span style="color:{tokens.text_secondary};">{comment.location}</span>')
    return " ".join(parts)


class _LoaderSignals(QObject):
    succeeded = Signal(int, object, object)  # epoch, payload, context
    failed = Signal(int, str)  # epoch, error message


class _AvatarSignals(QObject):
    loaded = Signal(object, object)  # QLabel, QImage


class _ReplyRow(QWidget):
    """楼中楼单行:昵称行 + 「回复 @xxx：内容」正文。"""

    def __init__(self, comment: BilibiliComment, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.comment = comment
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


class _CommentCard(QFrame):
    replies_requested = Signal(object, int)  # card, page

    def __init__(self, comment: BilibiliComment, dialog: BilibiliCommentsDialog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.comment = comment
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
        self.like_label = QLabel(f"👍 {_format_stat_value(comment.like) if comment.like else '赞'}")
        footer.addWidget(self.like_label)
        self.toggle_reply_button = QPushButton(self._toggle_button_text())
        self.toggle_reply_button.setObjectName("bilibiliCommentRepliesButton")
        self.toggle_reply_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_reply_button.setFlat(True)
        self.toggle_reply_button.setVisible(comment.rcount > 0)
        self.toggle_reply_button.clicked.connect(self._toggle_floor)
        footer.addWidget(self.toggle_reply_button)
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
        row = _ReplyRow(reply, floor)
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

    def _start_load(self, request: dict[str, object], context: dict[str, object]) -> None:
        epoch = self._epoch

        def work() -> None:
            try:
                payload = self._loader(request)
                self._signals.succeeded.emit(epoch, payload, context)
            except Exception as exc:  # noqa: BLE001 - loader 网络错误统一转文案
                self._signals.failed.emit(epoch, str(exc) or type(exc).__name__)

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
        self._apply_main_payload(payload)

    def _handle_failed(self, epoch: int, message: str) -> None:
        if epoch != self._epoch:
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
