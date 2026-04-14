from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QLineEdit,
)

from atv_player.api import ApiError, UnauthorizedError
from atv_player.controllers.browse_controller import filter_search_results
from atv_player.models import VodItem
from atv_player.ui.table_utils import configure_table_columns


class BrowsePage(QWidget):
    open_requested = Signal(object)
    unauthorized = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.keyword_edit = QLineEdit()
        self.search_button = QPushButton("搜索")
        self.filter_combo = QComboBox()
        self.clear_button = QPushButton("清空")
        self.results_table = QTableWidget(0, 2)
        self.results_table.setHorizontalHeaderLabels(["来源", "名称"])
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        configure_table_columns(self.results_table, stretch_column=1)
        self.status_label = QLabel("")
        self.breadcrumb_bar = QWidget()
        self.breadcrumb_layout = QHBoxLayout(self.breadcrumb_bar)
        self.breadcrumb_layout.setContentsMargins(0, 0, 0, 0)
        self.breadcrumb_layout.setSpacing(4)
        self.breadcrumb_buttons: list[QPushButton] = []
        self.refresh_button = QPushButton("刷新")
        self.prev_page_button = QPushButton("上一页")
        self.next_page_button = QPushButton("下一页")
        self.page_label = QLabel("第 1 / 1 页")
        self.page_size_combo = QComboBox()
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["类型", "名称", "大小", "豆瓣ID", "评分", "时间"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        configure_table_columns(self.table, stretch_column=1)
        self.current_items: list[VodItem] = []
        self.current_path = "/"
        self.current_page = 1
        self.page_size = 50
        self.total_items = 0
        self._page_state_by_path: dict[str, tuple[int, int]] = {}
        self._results: list[VodItem] = []
        self._filtered_results: list[VodItem] = []

        self.filter_combo.addItem("全部", "")
        self.filter_combo.addItem("📀 0", "0")
        self.filter_combo.addItem("💾 3", "3")
        self.filter_combo.addItem("🚀 5", "5")
        self.filter_combo.addItem("🌞 7", "7")
        self.filter_combo.addItem("📡 8", "8")
        self.filter_combo.addItem("☁ 9", "9")
        for size in ("20", "30", "50", "100"):
            self.page_size_combo.addItem(size, int(size))
        self.page_size_combo.setCurrentText(str(self.page_size))

        top_search_controls = QHBoxLayout()
        top_search_controls.addWidget(self.keyword_edit)
        top_search_controls.addWidget(self.search_button)

        result_actions = QHBoxLayout()
        result_actions.addWidget(self.filter_combo)
        result_actions.addWidget(self.clear_button)

        search_layout = QVBoxLayout()
        search_layout.addLayout(result_actions)
        search_layout.addWidget(self.status_label)
        search_layout.addWidget(self.results_table)
        self.search_panel = QWidget()
        self.search_panel.setLayout(search_layout)
        self.search_panel.hide()
        self.filter_combo.hide()
        self.clear_button.hide()
        self.status_label.hide()

        file_layout = QVBoxLayout()
        breadcrumb_row = QHBoxLayout()
        breadcrumb_row.addWidget(self.breadcrumb_bar, 1)
        breadcrumb_row.addWidget(self.refresh_button)
        file_layout.addLayout(breadcrumb_row)
        file_layout.addWidget(self.table)
        pagination_row = QHBoxLayout()
        pagination_row.addStretch(1)
        pagination_row.addWidget(self.prev_page_button)
        pagination_row.addWidget(self.page_label)
        pagination_row.addWidget(self.next_page_button)
        pagination_row.addWidget(self.page_size_combo)
        file_layout.addLayout(pagination_row)
        self.file_panel = QWidget()
        self.file_panel.setLayout(file_layout)

        self.content_splitter = QSplitter()
        self.content_splitter.addWidget(self.search_panel)
        self.content_splitter.addWidget(self.file_panel)
        self.content_splitter.setStretchFactor(0, 1)
        self.content_splitter.setStretchFactor(1, 3)
        self.content_splitter.setSizes([0, 1])

        self.search_button.clicked.connect(self.search)
        self.clear_button.clicked.connect(self.clear_results)
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        self.results_table.cellDoubleClicked.connect(self._open_search_result)
        self.refresh_button.clicked.connect(self.reload)
        self.table.cellDoubleClicked.connect(self._handle_open)
        self.prev_page_button.clicked.connect(self.previous_page)
        self.next_page_button.clicked.connect(self.next_page)
        self.page_size_combo.currentIndexChanged.connect(self._change_page_size)

        layout = QVBoxLayout(self)
        layout.addLayout(top_search_controls)
        layout.addWidget(self.content_splitter)
        self._update_pagination_controls()

    def load_path(self, path: str) -> None:
        target_path = path or "/"
        if target_path != self.current_path:
            saved_page, saved_size = self._page_state_by_path.get(target_path, (1, self.page_size))
            self.current_page = saved_page
            self.page_size = saved_size
            self._sync_page_size_combo()
        self.current_path = target_path
        self._update_breadcrumbs()
        try:
            items, total = self.controller.load_folder(self.current_path, page=self.current_page, size=self.page_size)
        except UnauthorizedError:
            self.unauthorized.emit()
            return
        except ApiError as exc:
            self._set_breadcrumb_status(str(exc))
            return
        self.total_items = total
        self._page_state_by_path[self.current_path] = (self.current_page, self.page_size)
        self.current_items = items
        self._populate_table(items)
        self._update_pagination_controls()

    def reload(self) -> None:
        self.load_path(self.current_path)

    def previous_page(self) -> None:
        if self.current_page <= 1:
            return
        self.current_page -= 1
        self.load_path(self.current_path)

    def next_page(self) -> None:
        if self.current_page >= self._total_pages():
            return
        self.current_page += 1
        self.load_path(self.current_path)

    def search(self) -> None:
        keyword = self.keyword_edit.text().strip()
        if not keyword:
            self.status_label.setText("请输入关键词")
            return
        try:
            self._results = self.controller.search(keyword)
        except UnauthorizedError:
            self.unauthorized.emit()
            return
        except ApiError as exc:
            self._show_search_results_panel()
            self.status_label.show()
            self.status_label.setText(str(exc))
            return
        if self._results:
            self._show_search_results_panel()
            self.status_label.show()
        else:
            self._hide_search_results_panel()
            return
        self.status_label.setText(f"{len(self._results)} 条结果")
        self._apply_filter()

    def clear_results(self) -> None:
        self._results = []
        self._filtered_results = []
        self.results_table.setRowCount(0)
        self.status_label.clear()
        self._hide_search_results_panel()

    def _apply_filter(self) -> None:
        drive_type = self.filter_combo.currentData()
        self._filtered_results = filter_search_results(self._results, drive_type or "")
        self.results_table.setRowCount(len(self._filtered_results))
        for row, item in enumerate(self._filtered_results):
            self.results_table.setItem(row, 0, QTableWidgetItem(item.type_name))
            self.results_table.setItem(row, 1, QTableWidgetItem(item.vod_name))
        if self._filtered_results:
            self._show_search_results_panel()
            self.status_label.show()
        else:
            self._hide_search_results_panel()

    def _open_search_result(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self._filtered_results)):
            return
        item = self._filtered_results[row]
        try:
            path = self.controller.resolve_search_result(item)
        except UnauthorizedError:
            self.unauthorized.emit()
            return
        except ApiError as exc:
            self._show_search_results_panel()
            self.status_label.show()
            self.status_label.setText(str(exc))
            return
        self.load_path(path)

    def _show_search_results_panel(self) -> None:
        self.search_panel.show()
        self.filter_combo.show()
        self.clear_button.show()
        total = max(self.content_splitter.width(), self.width(), 1200)
        left = max(total // 4, 220)
        self.content_splitter.setSizes([left, max(total - left, left)])

    def _hide_search_results_panel(self) -> None:
        self.search_panel.hide()
        self.filter_combo.hide()
        self.clear_button.hide()
        self.status_label.hide()
        self.content_splitter.setSizes([0, max(self.content_splitter.width(), self.width(), 1)])

    def _change_page_size(self) -> None:
        page_size = self.page_size_combo.currentData()
        if page_size is None:
            return
        page_size = int(page_size)
        if page_size == self.page_size:
            return
        self.page_size = page_size
        self.current_page = 1
        self.load_path(self.current_path)

    def _sync_page_size_combo(self) -> None:
        index = self.page_size_combo.findData(self.page_size)
        if index < 0:
            return
        self.page_size_combo.blockSignals(True)
        self.page_size_combo.setCurrentIndex(index)
        self.page_size_combo.blockSignals(False)

    def _total_pages(self) -> int:
        return max(1, (self.total_items + self.page_size - 1) // self.page_size)

    def _update_pagination_controls(self) -> None:
        total_pages = self._total_pages()
        self.page_label.setText(f"第 {self.current_page} / {total_pages} 页")
        self.prev_page_button.setEnabled(self.current_page > 1)
        self.next_page_button.setEnabled(self.current_page < total_pages)

    def _clear_breadcrumbs(self) -> None:
        while self.breadcrumb_layout.count():
            item = self.breadcrumb_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.breadcrumb_buttons = []

    def _update_breadcrumbs(self) -> None:
        self._clear_breadcrumbs()
        segments = [segment for segment in self.current_path.split("/") if segment]
        entries = [("🏠首页", "/")]
        current = ""
        for segment in segments:
            current = f"{current}/{segment}" if current else f"/{segment}"
            entries.append((segment, current))
        for index, (label, path) in enumerate(entries):
            button = QPushButton(label)
            button.setFlat(True)
            button.clicked.connect(lambda _checked=False, target=path: self.load_path(target))
            self.breadcrumb_layout.addWidget(button)
            self.breadcrumb_buttons.append(button)
            if index < len(entries) - 1:
                self.breadcrumb_layout.addWidget(QLabel("/"))
        self.breadcrumb_layout.addStretch(1)

    def _set_breadcrumb_status(self, message: str) -> None:
        self._clear_breadcrumbs()
        self.breadcrumb_layout.addWidget(QLabel(f"{self.current_path} | {message}"))
        self.breadcrumb_layout.addStretch(1)

    def _populate_table(self, items: list[VodItem]) -> None:
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            self.table.setItem(row, 0, QTableWidgetItem(self._item_kind(item)))
            self.table.setItem(row, 1, QTableWidgetItem(item.vod_name))
            self.table.setItem(row, 2, QTableWidgetItem(self._item_size(item)))
            self.table.setItem(row, 3, QTableWidgetItem(self._item_dbid(item)))
            self.table.setItem(row, 4, QTableWidgetItem(self._item_rating(item)))
            self.table.setItem(row, 5, QTableWidgetItem(item.vod_time))

    def _item_kind(self, item: VodItem) -> str:
        if item.type == 1:
            return "文件夹"
        if item.type == 2:
            return "视频"
        if item.type == 9:
            return "播放列表"
        return f"类型{item.type}"

    def _item_size(self, item: VodItem) -> str:
        return item.vod_remarks if getattr(item, "vod_tag", "") == "file" else "-"

    def _item_dbid(self, item: VodItem) -> str:
        dbid = getattr(item, "dbid", 0)
        return str(dbid) if dbid else ""

    def _item_rating(self, item: VodItem) -> str:
        return item.vod_remarks if getattr(item, "vod_tag", "") == "folder" else ""

    def _handle_open(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self.current_items)):
            return
        item = self.current_items[row]
        if item.type == 1:
            self.load_path(item.path)
            return
        try:
            if item.type == 2:
                request = self.controller.build_request_from_folder_item(item, self.current_items)
            else:
                request = self.controller.build_request_from_detail(item.vod_id)
        except UnauthorizedError:
            self.unauthorized.emit()
            return
        except ApiError as exc:
            self.path_label.setText(f"{self.current_path} | {exc}")
            return
        self.open_requested.emit(request)
