from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from atv_player.api import ApiError, UnauthorizedError
from atv_player.models import HistoryRecord


class HistoryPage(QWidget):
    open_detail_requested = Signal(str)
    unauthorized = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.delete_button = QPushButton("删除")
        self.clear_button = QPushButton("清空")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["标题", "当前播放", "进度", "时间"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.records: list[HistoryRecord] = []

        actions = QHBoxLayout()
        actions.addWidget(self.delete_button)
        actions.addWidget(self.clear_button)

        layout = QVBoxLayout(self)
        layout.addLayout(actions)
        layout.addWidget(self.table)

        self.delete_button.clicked.connect(self.delete_selected)
        self.clear_button.clicked.connect(self.clear_all)
        self.table.cellDoubleClicked.connect(self._open_selected)

    def load_history(self) -> None:
        try:
            records, _total = self.controller.load_page(page=1, size=100)
        except UnauthorizedError:
            self.unauthorized.emit()
            return
        except ApiError:
            return
        self.records = records
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            self.table.setItem(row, 0, QTableWidgetItem(record.vod_name))
            self.table.setItem(row, 1, QTableWidgetItem(record.vod_remarks))
            self.table.setItem(row, 2, QTableWidgetItem(str(record.position // 1000)))
            self.table.setItem(row, 3, QTableWidgetItem(str(record.create_time)))
        self.table.resizeColumnsToContents()

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
