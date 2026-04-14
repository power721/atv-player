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
        self.status_label = QLabel("")
        self.path_label = QLabel("/")
        self.refresh_button = QPushButton("刷新")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["类型", "名称", "时间"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.current_items: list[VodItem] = []
        self.current_path = "/"
        self._results: list[VodItem] = []
        self._filtered_results: list[VodItem] = []

        self.filter_combo.addItem("全部", "")
        self.filter_combo.addItem("📀 0", "0")
        self.filter_combo.addItem("💾 3", "3")
        self.filter_combo.addItem("🚀 5", "5")
        self.filter_combo.addItem("🌞 7", "7")
        self.filter_combo.addItem("📡 8", "8")
        self.filter_combo.addItem("☁ 9", "9")

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
        file_layout.addWidget(self.path_label)
        file_layout.addWidget(self.refresh_button)
        file_layout.addWidget(self.table)
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

        layout = QVBoxLayout(self)
        layout.addLayout(top_search_controls)
        layout.addWidget(self.content_splitter)

    def load_path(self, path: str) -> None:
        self.current_path = path or "/"
        self.path_label.setText(self.current_path)
        try:
            items, _total = self.controller.load_folder(self.current_path)
        except UnauthorizedError:
            self.unauthorized.emit()
            return
        except ApiError as exc:
            self.path_label.setText(f"{self.current_path} | {exc}")
            return
        self.current_items = items
        self._populate_table(items)

    def reload(self) -> None:
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
        self.results_table.resizeColumnsToContents()
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

    def _populate_table(self, items: list[VodItem]) -> None:
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            self.table.setItem(row, 0, QTableWidgetItem(self._item_kind(item)))
            self.table.setItem(row, 1, QTableWidgetItem(item.vod_name))
            self.table.setItem(row, 2, QTableWidgetItem(item.vod_time))
        self.table.resizeColumnsToContents()

    def _item_kind(self, item: VodItem) -> str:
        if item.type == 1:
            return "文件夹"
        if item.type == 2:
            return "视频"
        if item.type == 9:
            return "播放列表"
        return f"类型{item.type}"

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
