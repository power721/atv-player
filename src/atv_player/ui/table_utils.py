from __future__ import annotations

from PySide6.QtWidgets import QHeaderView, QTableWidget


def configure_table_columns(table: QTableWidget, stretch_column: int) -> None:
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    for column in range(table.columnCount()):
        mode = QHeaderView.ResizeMode.Stretch if column == stretch_column else QHeaderView.ResizeMode.ResizeToContents
        header.setSectionResizeMode(column, mode)
