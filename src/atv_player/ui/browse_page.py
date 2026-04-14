from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from atv_player.api import ApiError, UnauthorizedError
from atv_player.models import VodItem


class BrowsePage(QWidget):
    open_requested = Signal(object)
    unauthorized = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.path_label = QLabel("/")
        self.refresh_button = QPushButton("刷新")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["类型", "名称", "时间"])
        self.current_items: list[VodItem] = []
        self.current_path = "/"

        self.refresh_button.clicked.connect(self.reload)
        self.table.cellDoubleClicked.connect(self._handle_open)

        layout = QVBoxLayout(self)
        layout.addWidget(self.path_label)
        layout.addWidget(self.refresh_button)
        layout.addWidget(self.table)

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
