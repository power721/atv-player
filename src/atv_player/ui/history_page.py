from __future__ import annotations

from datetime import datetime

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
from atv_player.ui.table_utils import configure_table_columns


class HistoryPage(QWidget):
    open_detail_requested = Signal(str)
    unauthorized = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.delete_button = QPushButton("删除")
        self.clear_button = QPushButton("清空")
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["标题", "集数", "当前播放", "进度", "时间"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        configure_table_columns(self.table, stretch_column=0)
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
            self.table.setItem(row, 1, QTableWidgetItem(self._format_episode(record.episode)))
            self.table.setItem(row, 2, QTableWidgetItem(record.vod_remarks))
            self.table.setItem(row, 3, QTableWidgetItem(self._format_duration(record.position)))
            self.table.setItem(row, 4, QTableWidgetItem(self._format_timestamp(record.create_time)))

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

    def _format_episode(self, episode: int) -> str:
        return str(episode + 1) if episode >= 0 else ""

    def _format_duration(self, milliseconds: int) -> str:
        total_seconds = max(milliseconds // 1000, 0)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _format_timestamp(self, milliseconds: int) -> str:
        return datetime.fromtimestamp(milliseconds / 1000).strftime("%Y-%m-%d %H:%M:%S")
