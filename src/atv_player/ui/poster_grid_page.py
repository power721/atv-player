from __future__ import annotations

import threading
from threading import BoundedSemaphore
from typing import cast

import shiboken6
from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from atv_player.api import ApiError, UnauthorizedError
from atv_player.ui.poster_loader import load_local_poster_image, load_remote_poster_image, normalize_poster_url


class _PosterGridSignals(QObject):
    categories_loaded = Signal(int, object)
    items_loaded = Signal(int, object, int)
    failed = Signal(str, int, str)
    unauthorized = Signal(int, str)
    poster_loaded = Signal(object, object)


class PosterGridPage(QWidget):
    search_requested = Signal(str)
    open_requested = Signal(str)
    item_open_requested = Signal(object)
    folder_breadcrumb_requested = Signal(str, str, int)
    unauthorized = Signal()
    _CARD_WIDTH = 220
    _CARD_HEIGHT = 360
    _CARD_POSTER_SIZE = QSize(200, 285)
    _CARD_SPACING = 16
    _MIN_CARD_COLUMNS = 1
    _MAX_CARD_COLUMNS = 6

    def __init__(
        self,
        controller,
        click_action: str = "search",
        search_enabled: bool = False,
        folder_navigation_enabled: bool = False,
    ) -> None:
        super().__init__()
        self.controller = controller
        self._click_action = click_action
        self._search_enabled = search_enabled
        self._folder_navigation_enabled = folder_navigation_enabled
        self._initial_load_started = False
        self._search_mode = False
        self._search_keyword = ""
        self.category_list = QListWidget()
        self.keyword_edit = QLineEdit()
        self.search_button = QPushButton("搜索")
        self.clear_button = QPushButton("清空")
        self.breadcrumb_bar = QWidget()
        self.breadcrumb_layout = QHBoxLayout(self.breadcrumb_bar)
        self.breadcrumb_layout.setContentsMargins(0, 0, 0, 0)
        self.breadcrumb_layout.setSpacing(4)
        self.breadcrumb_buttons: list[QPushButton] = []
        self.status_label = QLabel("")
        self.prev_page_button = QPushButton("上一页")
        self.next_page_button = QPushButton("下一页")
        self.page_label = QLabel("第 1 / 1 页")
        self.cards_widget = QWidget()
        self.cards_layout = QGridLayout(self.cards_widget)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(self._CARD_SPACING)
        self.cards_scroll = QScrollArea()
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setWidget(self.cards_widget)
        self.card_buttons: list[QToolButton] = []
        self._folder_breadcrumbs: list[dict[str, str]] = []
        self.categories = []
        self.items = []
        self.selected_category_id = ""
        self.current_page = 1
        self.page_size = 30
        self.total_items = 0
        self._current_card_columns = self._MIN_CARD_COLUMNS
        self._categories_request_id = 0
        self._items_request_id = 0
        self._poster_generation = 0
        self._poster_semaphore = BoundedSemaphore(value=6)
        self._signals = _PosterGridSignals(self)
        self._signals.categories_loaded.connect(self._handle_categories_loaded)
        self._signals.items_loaded.connect(self._handle_items_loaded)
        self._signals.failed.connect(self._handle_failed)
        self._signals.unauthorized.connect(self._handle_unauthorized)
        self._signals.poster_loaded.connect(self._handle_poster_loaded)

        self.category_list.setMinimumWidth(180)
        self.status_label.setWordWrap(True)
        self.breadcrumb_bar.setVisible(self._folder_navigation_enabled)

        right = QVBoxLayout()
        if self._search_enabled:
            search_row = QHBoxLayout()
            search_row.addWidget(self.keyword_edit, 1)
            search_row.addWidget(self.search_button)
            search_row.addWidget(self.clear_button)
            right.addLayout(search_row)
        else:
            self.keyword_edit.hide()
            self.search_button.hide()
            self.clear_button.hide()
        right.addWidget(self.breadcrumb_bar)
        right.addWidget(self.status_label)
        right.addWidget(self.cards_scroll, 1)
        paging = QHBoxLayout()
        paging.addStretch(1)
        paging.addWidget(self.prev_page_button)
        paging.addWidget(self.page_label)
        paging.addWidget(self.next_page_button)
        right.addLayout(paging)

        self.content_container = QWidget()
        self.content_container.setMaximumWidth(1800)
        self.content_container.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        content_layout = QHBoxLayout(self.content_container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self.category_list, 1)
        content_layout.addLayout(right, 4)

        layout = QHBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(self.content_container, 100)
        layout.addStretch(1)

        self.category_list.currentRowChanged.connect(self._handle_category_row_changed)
        self.prev_page_button.clicked.connect(self.previous_page)
        self.next_page_button.clicked.connect(self.next_page)
        if self._search_enabled:
            self.search_button.clicked.connect(self.search)
            self.clear_button.clicked.connect(self.clear_search)
            self.keyword_edit.returnPressed.connect(self.search)

    def _is_widget_alive(self) -> bool:
        return shiboken6.isValid(self)

    def ensure_loaded(self) -> None:
        if self._initial_load_started:
            return
        self._initial_load_started = True
        self.reload_categories()

    def reload_categories(self) -> None:
        self._categories_request_id += 1
        request_id = self._categories_request_id
        self.status_label.setText("加载分类中...")

        def run() -> None:
            try:
                categories = self.controller.load_categories()
            except UnauthorizedError:
                if self._is_widget_alive():
                    self._signals.unauthorized.emit(request_id, "categories")
                return
            except ApiError as exc:
                if self._is_widget_alive():
                    self._signals.failed.emit(str(exc), request_id, "categories")
                return
            if self._is_widget_alive():
                self._signals.categories_loaded.emit(request_id, categories)

        threading.Thread(target=run, daemon=True).start()

    def load_items(self, category_id: str, page: int) -> None:
        self._items_request_id += 1
        request_id = self._items_request_id
        self.status_label.setText("加载中...")

        def run() -> None:
            try:
                items, total = self.controller.load_items(category_id, page)
            except UnauthorizedError:
                if self._is_widget_alive():
                    self._signals.unauthorized.emit(request_id, "items")
                return
            except ApiError as exc:
                if self._is_widget_alive():
                    self._signals.failed.emit(str(exc), request_id, "items")
                return
            if self._is_widget_alive():
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
            self.status_label.setText("暂无分类")
            self._update_pagination()
            return
        self.category_list.setCurrentRow(0)

    def _handle_category_row_changed(self, row: int) -> None:
        if not (0 <= row < len(self.categories)):
            return
        self.selected_category_id = self.categories[row].type_id
        self.current_page = 1
        self.reset_folder_breadcrumbs_to_root()
        if self._search_mode:
            return
        self.load_items(self.selected_category_id, self.current_page)

    def _handle_items_loaded(self, request_id: int, items, total: int) -> None:
        if request_id != self._items_request_id:
            return
        self.show_items(items, total)

    def _handle_failed(self, message: str, request_id: int, request_kind: str) -> None:
        if request_kind == "categories" and request_id != self._categories_request_id:
            return
        if request_kind == "items" and request_id != self._items_request_id:
            return
        self.status_label.setText(message)
        self._update_pagination()

    def _handle_unauthorized(self, request_id: int, request_kind: str) -> None:
        if request_kind == "categories" and request_id != self._categories_request_id:
            return
        if request_kind == "items" and request_id != self._items_request_id:
            return
        self.unauthorized.emit()

    def _render_cards(self) -> None:
        self._poster_generation += 1
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item is None:
                continue
            widget = cast(QWidget | None, item.widget())
            if widget is not None:
                widget.deleteLater()
        self.card_buttons = []
        for item in self.items:
            button = self._build_card_button(item)
            self.card_buttons.append(button)
            self._start_card_poster_load(button, item)
        self._relayout_cards()

    def _relayout_cards(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item is None:
                continue
            widget = cast(QWidget | None, item.widget())
            if widget is not None:
                self.cards_layout.removeWidget(widget)
        columns = self._column_count_for_width(self.cards_scroll.viewport().width())
        self._current_card_columns = columns
        for index, button in enumerate(self.card_buttons):
            self.cards_layout.addWidget(button, index // columns, index % columns)

    def _column_count_for_width(self, available_width: int) -> int:
        if available_width <= 0:
            return self._MIN_CARD_COLUMNS
        fit_columns = (available_width + self._CARD_SPACING) // (self._CARD_WIDTH + self._CARD_SPACING)
        fit_columns = max(self._MIN_CARD_COLUMNS, fit_columns)
        return min(fit_columns, self._MAX_CARD_COLUMNS)

    def _update_pagination(self) -> None:
        total_pages = max(1, (self.total_items + self.page_size - 1) // self.page_size)
        self.page_label.setText(f"第 {self.current_page} / {total_pages} 页")
        self.prev_page_button.setEnabled(self.current_page > 1)
        self.next_page_button.setEnabled(self.current_page < total_pages)

    def previous_page(self) -> None:
        if self.current_page <= 1:
            return
        self.current_page -= 1
        if self._search_mode:
            self._search_items(self._search_keyword, self.current_page)
            return
        if not self.selected_category_id:
            return
        self.load_items(self.selected_category_id, self.current_page)

    def next_page(self) -> None:
        total_pages = max(1, (self.total_items + self.page_size - 1) // self.page_size)
        if self.current_page >= total_pages:
            return
        self.current_page += 1
        if self._search_mode:
            self._search_items(self._search_keyword, self.current_page)
            return
        if not self.selected_category_id:
            return
        self.load_items(self.selected_category_id, self.current_page)

    def search(self) -> None:
        if not self._search_enabled:
            return
        keyword = self.keyword_edit.text().strip()
        if not keyword:
            self.clear_search()
            return
        self._search_mode = True
        self._search_keyword = keyword
        self.current_page = 1
        self._search_items(keyword, self.current_page)

    def clear_search(self) -> None:
        if not self._search_enabled:
            return
        self.keyword_edit.clear()
        self._search_mode = False
        self._search_keyword = ""
        self.current_page = 1
        if self.selected_category_id:
            self.load_items(self.selected_category_id, self.current_page)

    def _search_items(self, keyword: str, page: int) -> None:
        self._items_request_id += 1
        request_id = self._items_request_id
        self.status_label.setText("搜索中...")

        def run() -> None:
            try:
                items, total = self.controller.search_items(keyword, page)
            except UnauthorizedError:
                if self._is_widget_alive():
                    self._signals.unauthorized.emit(request_id, "items")
                return
            except ApiError as exc:
                if self._is_widget_alive():
                    self._signals.failed.emit(str(exc), request_id, "items")
                return
            if self._is_widget_alive():
                self._signals.items_loaded.emit(request_id, items, total)

        threading.Thread(target=run, daemon=True).start()

    def show_items(
        self,
        items,
        total: int,
        page: int | None = None,
        empty_message: str = "当前分类暂无内容",
    ) -> None:
        self._items_request_id += 1
        if page is not None:
            self.current_page = page
        self.items = list(items)
        self.total_items = total
        self.status_label.setText("" if self.items else empty_message)
        self._render_cards()
        self._update_pagination()

    def reset_folder_breadcrumbs_to_root(self) -> None:
        if not self._folder_navigation_enabled:
            return
        row = self.category_list.currentRow()
        if not (0 <= row < len(self.categories)):
            self._set_folder_breadcrumbs([])
            return
        category = self.categories[row]
        self._set_folder_breadcrumbs(
            [
                {"id": "", "label": "首页", "kind": "home"},
                {"id": category.type_id, "label": category.type_name, "kind": "category"},
            ]
        )

    def push_folder_breadcrumb(self, breadcrumb_id: str, label: str) -> None:
        if not self._folder_navigation_enabled:
            return
        breadcrumbs = list(self._folder_breadcrumbs)
        breadcrumbs.append({"id": breadcrumb_id, "label": label, "kind": "folder"})
        self._set_folder_breadcrumbs(breadcrumbs)

    def trim_folder_breadcrumbs(self, index: int) -> None:
        if not self._folder_navigation_enabled:
            return
        self._set_folder_breadcrumbs(self._folder_breadcrumbs[: index + 1])

    def _set_folder_breadcrumbs(self, breadcrumbs: list[dict[str, str]]) -> None:
        self._folder_breadcrumbs = list(breadcrumbs)
        self._render_folder_breadcrumbs()

    def _render_folder_breadcrumbs(self) -> None:
        while self.breadcrumb_layout.count():
            item = self.breadcrumb_layout.takeAt(0)
            if item is None:
                continue
            widget = cast(QWidget | None, item.widget())
            if widget is not None:
                widget.deleteLater()
        self.breadcrumb_buttons = []
        self.breadcrumb_bar.setVisible(self._folder_navigation_enabled and bool(self._folder_breadcrumbs))
        if not self._folder_navigation_enabled:
            return
        for index, breadcrumb in enumerate(self._folder_breadcrumbs):
            if index > 0:
                self.breadcrumb_layout.addWidget(QLabel("/"))
            button = QPushButton(breadcrumb["label"])
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFlat(True)
            button.clicked.connect(
                lambda _checked=False, current_index=index: self._handle_folder_breadcrumb_clicked(current_index)
            )
            self.breadcrumb_buttons.append(button)
            self.breadcrumb_layout.addWidget(button)
        self.breadcrumb_layout.addStretch(1)

    def _handle_folder_breadcrumb_clicked(self, index: int) -> None:
        if not (0 <= index < len(self._folder_breadcrumbs)):
            return
        breadcrumb = self._folder_breadcrumbs[index]
        self.folder_breadcrumb_requested.emit(breadcrumb["id"], breadcrumb["kind"], index)

    def _build_card_button(self, item) -> QToolButton:
        text = item.vod_name if not item.vod_remarks else f"{item.vod_name}\n{item.vod_remarks}"
        button = QToolButton()
        button.setText(text)
        button.setFixedSize(self._CARD_WIDTH, self._CARD_HEIGHT)
        button.setToolTip(item.vod_name)
        button.setIconSize(self._CARD_POSTER_SIZE)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet("padding: 10px;")
        button.clicked.connect(lambda _checked=False, current_item=item: self._handle_card_clicked(current_item))
        return button

    def _handle_card_clicked(self, item) -> None:
        if self._click_action == "open":
            self.item_open_requested.emit(item)
            self.open_requested.emit(item.vod_id)
            return
        self.search_requested.emit(item.vod_name)

    def _start_card_poster_load(self, button: QToolButton, item) -> None:
        poster_source = item.vod_pic or ""
        image_url = normalize_poster_url(poster_source)
        if not image_url:
            return

        gen = self._poster_generation

        def load() -> None:
            self._poster_semaphore.acquire()
            try:
                image = load_local_poster_image(poster_source, self._CARD_POSTER_SIZE)
                if image is None:
                    image = load_remote_poster_image(image_url, self._CARD_POSTER_SIZE)
                if image is not None and self._is_widget_alive() and gen == self._poster_generation:
                    self._signals.poster_loaded.emit(button, image)
            finally:
                self._poster_semaphore.release()

        threading.Thread(target=load, daemon=True).start()

    def _handle_poster_loaded(self, button: QToolButton, image) -> None:
        if button not in self.card_buttons:
            return
        pixmap = QPixmap.fromImage(image)
        button.setIcon(QIcon(pixmap))
        button.setIconSize(self._CARD_POSTER_SIZE)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.card_buttons:
            self._relayout_cards()
