from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from atv_player.api import ApiError, UnauthorizedError


class _DoubanSignals(QObject):
    categories_loaded = Signal(int, object)
    items_loaded = Signal(int, object, int)
    failed = Signal(str, int)
    unauthorized = Signal(int)


class DoubanPage(QWidget):
    search_requested = Signal(str)
    unauthorized = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.category_list = QListWidget()
        self.status_label = QLabel("")
        self.prev_page_button = QPushButton("上一页")
        self.next_page_button = QPushButton("下一页")
        self.page_label = QLabel("第 1 / 1 页")
        self.cards_widget = QWidget()
        self.cards_layout = QGridLayout(self.cards_widget)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(16)
        self.cards_scroll = QScrollArea()
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setWidget(self.cards_widget)
        self.card_buttons: list[QPushButton] = []
        self.categories = []
        self.items = []
        self.selected_category_id = ""
        self.current_page = 1
        self.page_size = 35
        self.total_items = 0
        self._categories_request_id = 0
        self._items_request_id = 0
        self._signals = _DoubanSignals()
        self._signals.categories_loaded.connect(self._handle_categories_loaded)
        self._signals.items_loaded.connect(self._handle_items_loaded)
        self._signals.failed.connect(self._handle_failed)
        self._signals.unauthorized.connect(self._handle_unauthorized)

        right = QVBoxLayout()
        right.addWidget(self.status_label)
        right.addWidget(self.cards_scroll, 1)
        paging = QHBoxLayout()
        paging.addStretch(1)
        paging.addWidget(self.prev_page_button)
        paging.addWidget(self.page_label)
        paging.addWidget(self.next_page_button)
        right.addLayout(paging)

        layout = QHBoxLayout(self)
        layout.addWidget(self.category_list, 1)
        layout.addLayout(right, 4)

        self.category_list.currentRowChanged.connect(self._handle_category_row_changed)
        self.prev_page_button.clicked.connect(self.previous_page)
        self.next_page_button.clicked.connect(self.next_page)

        self.reload_categories()

    def reload_categories(self) -> None:
        self._categories_request_id += 1
        request_id = self._categories_request_id
        self.status_label.setText("加载分类中...")

        def run() -> None:
            try:
                categories = self.controller.load_categories()
            except UnauthorizedError:
                self._signals.unauthorized.emit(request_id)
                return
            except ApiError as exc:
                self._signals.failed.emit(str(exc), request_id)
                return
            self._signals.categories_loaded.emit(request_id, categories)

        threading.Thread(target=run, daemon=True).start()

    def load_items(self, category_id: str, page: int) -> None:
        self._items_request_id += 1
        request_id = self._items_request_id
        self.status_label.setText("加载电影中...")

        def run() -> None:
            try:
                items, total = self.controller.load_items(category_id, page)
            except UnauthorizedError:
                self._signals.unauthorized.emit(request_id)
                return
            except ApiError as exc:
                self._signals.failed.emit(str(exc), request_id)
                return
            self._signals.items_loaded.emit(request_id, items, total)

        threading.Thread(target=run, daemon=True).start()

    def _handle_categories_loaded(self, request_id: int, categories) -> None:
        if request_id != self._categories_request_id:
            return
        self.categories = list(categories)
        self.category_list.clear()
        for category in self.categories:
            self.category_list.addItem(category.type_name)
        if not self.categories:
            self.status_label.setText("暂无豆瓣分类")
            self._update_pagination()
            return
        self.category_list.setCurrentRow(0)

    def _handle_category_row_changed(self, row: int) -> None:
        if not (0 <= row < len(self.categories)):
            return
        self.selected_category_id = self.categories[row].type_id
        self.current_page = 1
        self.load_items(self.selected_category_id, self.current_page)

    def _handle_items_loaded(self, request_id: int, items, total: int) -> None:
        if request_id != self._items_request_id:
            return
        self.items = list(items)
        self.total_items = total
        self.status_label.setText("" if self.items else "当前分类暂无内容")
        self._render_cards()
        self._update_pagination()

    def _handle_failed(self, message: str, _request_id: int) -> None:
        self.status_label.setText(message)
        self._update_pagination()

    def _handle_unauthorized(self, _request_id: int) -> None:
        self.unauthorized.emit()

    def _render_cards(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.card_buttons = []
        for index, item in enumerate(self.items):
            text = item.vod_name if not item.vod_remarks else f"{item.vod_name}\n{item.vod_remarks}"
            button = QPushButton(text)
            button.clicked.connect(lambda _checked=False, keyword=item.vod_name: self.search_requested.emit(keyword))
            self.card_buttons.append(button)
            self.cards_layout.addWidget(button, index // 4, index % 4)

    def _update_pagination(self) -> None:
        total_pages = max(1, (self.total_items + self.page_size - 1) // self.page_size)
        self.page_label.setText(f"第 {self.current_page} / {total_pages} 页")
        self.prev_page_button.setEnabled(self.current_page > 1)
        self.next_page_button.setEnabled(self.current_page < total_pages)

    def previous_page(self) -> None:
        if self.current_page <= 1 or not self.selected_category_id:
            return
        self.current_page -= 1
        self.load_items(self.selected_category_id, self.current_page)

    def next_page(self) -> None:
        total_pages = max(1, (self.total_items + self.page_size - 1) // self.page_size)
        if self.current_page >= total_pages or not self.selected_category_id:
            return
        self.current_page += 1
        self.load_items(self.selected_category_id, self.current_page)
