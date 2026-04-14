from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from atv_player.api import ApiError, UnauthorizedError
from atv_player.models import HistoryRecord
from atv_player.ui.table_utils import configure_table_columns


class HistoryPage(QWidget):
    open_detail_requested = Signal(str)
    unauthorized = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.delete_button = QPushButton("删除")
        self.clear_button = QPushButton("清空")
        self.prev_page_button = QPushButton("上一页")
        self.next_page_button = QPushButton("下一页")
        self.page_label = QLabel("第 1 / 1 页")
        self.page_size_combo = QComboBox()
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["标题", "当前播放", "进度", "时间"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        configure_table_columns(self.table, stretch_column=0)
        self.records: list[HistoryRecord] = []
        self.current_page = 1
        self.page_size = 100
        self.total_items = 0
        for size in ("20", "30", "50", "100"):
            self.page_size_combo.addItem(size, int(size))
        self.page_size_combo.setCurrentText(str(self.page_size))

        actions = QHBoxLayout()
        actions.addWidget(self.delete_button)
        actions.addWidget(self.clear_button)
        actions.addStretch(1)
        actions.addWidget(self.prev_page_button)
        actions.addWidget(self.page_label)
        actions.addWidget(self.next_page_button)
        actions.addWidget(self.page_size_combo)

        layout = QVBoxLayout(self)
        layout.addLayout(actions)
        layout.addWidget(self.table)

        self.delete_button.clicked.connect(self.delete_selected)
        self.clear_button.clicked.connect(self.clear_all)
        self.table.cellDoubleClicked.connect(self._open_selected)
        self.prev_page_button.clicked.connect(self.previous_page)
        self.next_page_button.clicked.connect(self.next_page)
        self.page_size_combo.currentIndexChanged.connect(self._change_page_size)
        self._update_pagination_controls()

    def load_history(self) -> None:
        try:
            records, total = self.controller.load_page(page=self.current_page, size=self.page_size)
        except UnauthorizedError:
            self.unauthorized.emit()
            return
        except ApiError:
            return
        self.total_items = total
        self.records = records
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            self.table.setItem(row, 0, QTableWidgetItem(record.vod_name))
            self.table.setItem(row, 1, QTableWidgetItem(record.vod_remarks))
            self.table.setItem(row, 2, QTableWidgetItem(str(record.position // 1000)))
            self.table.setItem(row, 3, QTableWidgetItem(str(record.create_time)))
        self._update_pagination_controls()

    def delete_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows:
            return
        ids = [self.records[row].id for row in rows]
        try:
            if len(ids) == 1:
                self.controller.delete_one(ids[0])
            else:
                self.controller.delete_many(ids)
        except UnauthorizedError:
            self.unauthorized.emit()
            return
        except ApiError:
            return
        if len(ids) == len(self.records) and self.current_page > 1:
            self.current_page -= 1
        self.load_history()

    def clear_all(self) -> None:
        try:
            self.controller.clear_all()
        except UnauthorizedError:
            self.unauthorized.emit()
            return
        except ApiError:
            return
        self.load_history()

    def _open_selected(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self.records)):
            return
        self.open_detail_requested.emit(self.records[row].key)

    def previous_page(self) -> None:
        if self.current_page <= 1:
            return
        self.current_page -= 1
        self.load_history()

    def next_page(self) -> None:
        if self.current_page >= self._total_pages():
            return
        self.current_page += 1
        self.load_history()

    def _change_page_size(self) -> None:
        page_size = self.page_size_combo.currentData()
        if page_size is None:
            return
        page_size = int(page_size)
        if page_size == self.page_size:
            return
        self.page_size = page_size
        self.current_page = 1
        self.load_history()

    def _total_pages(self) -> int:
        return max(1, (self.total_items + self.page_size - 1) // self.page_size)

    def _update_pagination_controls(self) -> None:
        total_pages = self._total_pages()
        self.page_label.setText(f"第 {self.current_page} / {total_pages} 页")
        self.prev_page_button.setEnabled(self.current_page > 1)
        self.next_page_button.setEnabled(self.current_page < total_pages)
