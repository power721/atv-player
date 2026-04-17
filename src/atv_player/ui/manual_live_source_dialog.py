from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class _ManualEntryFormDialog(QDialog):
    def __init__(
        self,
        *,
        group_name: str = "",
        channel_name: str = "",
        stream_url: str = "",
        logo_url: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("频道信息")
        self.group_edit = QLineEdit(group_name, self)
        self.channel_edit = QLineEdit(channel_name, self)
        self.url_edit = QLineEdit(stream_url, self)
        self.logo_edit = QLineEdit(logo_url, self)
        form = QFormLayout()
        form.addRow("分组", self.group_edit)
        form.addRow("频道名", self.channel_edit)
        form.addRow("地址", self.url_edit)
        form.addRow("Logo URL", self.logo_edit)
        self.ok_button = QPushButton("确定", self)
        self.cancel_button = QPushButton("取消", self)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.ok_button)
        actions.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(actions)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

    def values(self) -> tuple[str, str, str, str]:
        return (
            self.group_edit.text().strip(),
            self.channel_edit.text().strip(),
            self.url_edit.text().strip(),
            self.logo_edit.text().strip(),
        )


class ManualLiveSourceDialog(QDialog):
    def __init__(self, manager, source_id: int, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.source_id = source_id
        self.setWindowTitle("管理频道")
        self.resize(760, 420)
        self.entry_table = QTableWidget(0, 4, self)
        self.entry_table.setHorizontalHeaderLabels(["分组", "频道名", "地址", "Logo"])
        self.entry_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.entry_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.entry_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.add_button = QPushButton("添加频道", self)
        self.edit_button = QPushButton("编辑频道", self)
        self.delete_button = QPushButton("删除频道", self)
        self.up_button = QPushButton("上移", self)
        self.down_button = QPushButton("下移", self)
        actions = QHBoxLayout()
        for button in (
            self.add_button,
            self.edit_button,
            self.delete_button,
            self.up_button,
            self.down_button,
        ):
            actions.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addLayout(actions)
        layout.addWidget(self.entry_table)
        self.add_button.clicked.connect(self._add_entry)
        self.edit_button.clicked.connect(self._edit_selected_entry)
        self.delete_button.clicked.connect(self._delete_selected_entry)
        self.up_button.clicked.connect(lambda: self._move_selected_entry(-1))
        self.down_button.clicked.connect(lambda: self._move_selected_entry(1))
        self.entry_table.itemSelectionChanged.connect(self._sync_action_state)
        self.reload_entries()

    def reload_entries(self) -> None:
        entries = self.manager.list_manual_entries(self.source_id)
        self.entry_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self.entry_table.setItem(row, 0, QTableWidgetItem(entry.group_name))
            self.entry_table.setItem(row, 1, QTableWidgetItem(entry.channel_name))
            self.entry_table.setItem(row, 2, QTableWidgetItem(entry.stream_url))
            self.entry_table.setItem(row, 3, QTableWidgetItem(entry.logo_url))
            self.entry_table.item(row, 0).setData(256, entry.id)
        self._sync_action_state()

    def _has_selection(self) -> bool:
        selection_model = self.entry_table.selectionModel()
        return bool(selection_model is not None and selection_model.hasSelection())

    def _selected_entry_id(self) -> int | None:
        if not self._has_selection():
            return None
        row = self.entry_table.currentRow()
        if row < 0:
            return None
        item = self.entry_table.item(row, 0)
        if item is None:
            return None
        return int(item.data(256))

    def _selected_values(self) -> tuple[str, str, str, str]:
        if not self._has_selection():
            return "", "", "", ""
        row = self.entry_table.currentRow()
        group_name = self.entry_table.item(row, 0).text() if self.entry_table.item(row, 0) is not None else ""
        channel_name = self.entry_table.item(row, 1).text() if self.entry_table.item(row, 1) is not None else ""
        stream_url = self.entry_table.item(row, 2).text() if self.entry_table.item(row, 2) is not None else ""
        logo_url = self.entry_table.item(row, 3).text() if self.entry_table.item(row, 3) is not None else ""
        return group_name, channel_name, stream_url, logo_url

    def _sync_action_state(self) -> None:
        has_selection = self._has_selection()
        self.edit_button.setEnabled(has_selection)
        self.delete_button.setEnabled(has_selection)
        self.up_button.setEnabled(has_selection)
        self.down_button.setEnabled(has_selection)

    def _prompt_entry(
        self,
        *,
        group_name: str = "",
        channel_name: str = "",
        stream_url: str = "",
        logo_url: str = "",
    ) -> tuple[str, str, str, str]:
        dialog = _ManualEntryFormDialog(
            group_name=group_name,
            channel_name=channel_name,
            stream_url=stream_url,
            logo_url=logo_url,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return "", "", "", ""
        return dialog.values()

    def _confirm_delete_entry(self, channel_name: str) -> bool:
        return (
            QMessageBox.question(
                self,
                "删除频道",
                f"确定删除频道“{channel_name}”吗？",
            )
            == QMessageBox.StandardButton.Yes
        )

    def _add_entry(self) -> None:
        group_name, channel_name, stream_url, logo_url = self._prompt_entry()
        if not channel_name or not stream_url:
            return
        self.manager.add_manual_entry(
            self.source_id,
            group_name=group_name,
            channel_name=channel_name,
            stream_url=stream_url,
            logo_url=logo_url,
        )
        self.reload_entries()

    def _edit_selected_entry(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None:
            return
        group_name, channel_name, stream_url, logo_url = self._prompt_entry(
            group_name=self._selected_values()[0],
            channel_name=self._selected_values()[1],
            stream_url=self._selected_values()[2],
            logo_url=self._selected_values()[3],
        )
        if not channel_name or not stream_url:
            return
        self.manager.update_manual_entry(
            entry_id,
            group_name=group_name,
            channel_name=channel_name,
            stream_url=stream_url,
            logo_url=logo_url,
        )
        self.reload_entries()

    def _delete_selected_entry(self) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None:
            return
        channel_name = self._selected_values()[1]
        if not self._confirm_delete_entry(channel_name):
            return
        self.manager.delete_manual_entry(entry_id)
        self.reload_entries()

    def _move_selected_entry(self, direction: int) -> None:
        entry_id = self._selected_entry_id()
        if entry_id is None:
            return
        self.manager.move_manual_entry(entry_id, direction)
        self.reload_entries()
