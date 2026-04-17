from __future__ import annotations

from PySide6.QtWidgets import QDialog, QTableWidget, QTableWidgetItem, QVBoxLayout


class ManualLiveSourceDialog(QDialog):
    def __init__(self, manager, source_id: int, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.source_id = source_id
        self.setWindowTitle("管理频道")
        self.resize(760, 420)
        self.entry_table = QTableWidget(0, 3, self)
        self.entry_table.setHorizontalHeaderLabels(["分组", "频道名", "地址"])
        layout = QVBoxLayout(self)
        layout.addWidget(self.entry_table)

    def reload_entries(self) -> None:
        entries = self.manager.list_manual_entries(self.source_id)
        self.entry_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self.entry_table.setItem(row, 0, QTableWidgetItem(entry.group_name))
            self.entry_table.setItem(row, 1, QTableWidgetItem(entry.channel_name))
            self.entry_table.setItem(row, 2, QTableWidgetItem(entry.stream_url))
            self.entry_table.item(row, 0).setData(256, entry.id)
