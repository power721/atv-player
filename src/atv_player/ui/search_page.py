from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from atv_player.api import ApiError, UnauthorizedError
from atv_player.controllers.browse_controller import filter_search_results
from atv_player.models import VodItem
from atv_player.ui.table_utils import configure_table_columns


class SearchPage(QWidget):
    browse_requested = Signal(str)
    unauthorized = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.keyword_edit = QLineEdit()
        self.filter_combo = QComboBox()
        self.search_button = QPushButton("搜索")
        self.clear_button = QPushButton("清空")
        self.results_table = QTableWidget(0, 2)
        self.results_table.setHorizontalHeaderLabels(["来源", "名称"])
        configure_table_columns(self.results_table, stretch_column=1)
        self.status_label = QLineEdit()
        self.status_label.setReadOnly(True)
        self.status_label.setPlaceholderText("搜索电报资源")
        self._results: list[VodItem] = []

        self.filter_combo.addItem("全部", "")
        self.filter_combo.addItem("📀 0", "0")
        self.filter_combo.addItem("💾 3", "3")
        self.filter_combo.addItem("🚀 5", "5")
        self.filter_combo.addItem("🌞 7", "7")
        self.filter_combo.addItem("📡 8", "8")
        self.filter_combo.addItem("☁ 9", "9")

        top = QHBoxLayout()
        top.addWidget(self.keyword_edit)
        top.addWidget(self.filter_combo)
        top.addWidget(self.search_button)
        top.addWidget(self.clear_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.status_label)
        layout.addWidget(self.results_table)

        self.search_button.clicked.connect(self.search)
        self.clear_button.clicked.connect(self.clear_results)
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        self.results_table.cellDoubleClicked.connect(self._open_selected)

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
            self.status_label.setText(str(exc))
            return
        self.status_label.setText(f"{len(self._results)} 条结果")
        self._apply_filter()

    def clear_results(self) -> None:
        self._results = []
        self.results_table.setRowCount(0)
        self.status_label.clear()

    def _apply_filter(self) -> None:
        drive_type = self.filter_combo.currentData()
        filtered = filter_search_results(self._results, drive_type or "")
        self.results_table.setRowCount(len(filtered))
        for row, item in enumerate(filtered):
            self.results_table.setItem(row, 0, QTableWidgetItem(item.type_name))
            self.results_table.setItem(row, 1, QTableWidgetItem(item.vod_name))
        self._filtered_results = filtered

    def _open_selected(self, row: int, _column: int) -> None:
        if not hasattr(self, "_filtered_results") or not (0 <= row < len(self._filtered_results)):
            return
        item = self._filtered_results[row]
        try:
            path = self.controller.resolve_search_result(item)
        except UnauthorizedError:
            self.unauthorized.emit()
            return
        except ApiError as exc:
            self.status_label.setText(str(exc))
            return
        self.browse_requested.emit(path)
