from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class LiveSourceManagerDialog(QDialog):
    def __init__(self, manager, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("直播源管理")
        self.resize(920, 520)
        self.source_table = QTableWidget(0, 6, self)
        self.source_table.setHorizontalHeaderLabels(["名称", "类型", "地址", "启用", "状态", "最近刷新"])
        self.source_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.source_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.source_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.add_remote_button = QPushButton("添加远程源")
        self.add_local_button = QPushButton("添加本地源")
        self.add_manual_button = QPushButton("添加手动源")
        self.manage_channels_button = QPushButton("管理频道")
        self.refresh_button = QPushButton("刷新")
        actions = QHBoxLayout()
        for button in (
            self.add_remote_button,
            self.add_local_button,
            self.add_manual_button,
            self.manage_channels_button,
            self.refresh_button,
        ):
            actions.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addLayout(actions)
        layout.addWidget(self.source_table)
        self.add_remote_button.clicked.connect(self._add_remote_source)
        self.add_local_button.clicked.connect(self._add_local_source)
        self.add_manual_button.clicked.connect(self._add_manual_source)
        self.refresh_button.clicked.connect(self._refresh_selected)
        self.source_table.itemSelectionChanged.connect(self._sync_action_state)
        self.reload_sources()

    def reload_sources(self) -> None:
        sources = self.manager.list_sources()
        self.source_table.setRowCount(len(sources))
        for row, source in enumerate(sources):
            self.source_table.setItem(row, 0, QTableWidgetItem(source.display_name))
            self.source_table.setItem(row, 1, QTableWidgetItem(source.source_type))
            self.source_table.setItem(row, 2, QTableWidgetItem(source.source_value))
            self.source_table.setItem(row, 3, QTableWidgetItem("是" if source.enabled else "否"))
            self.source_table.setItem(row, 4, QTableWidgetItem(source.last_error or "正常"))
            self.source_table.setItem(row, 5, QTableWidgetItem(str(source.last_refreshed_at or "")))
            self.source_table.item(row, 0).setData(256, source.id)
            self.source_table.item(row, 0).setData(257, source.source_type)
        self._sync_action_state()

    def _selected_source_id(self) -> int | None:
        row = self.source_table.currentRow()
        if row < 0:
            return None
        item = self.source_table.item(row, 0)
        if item is None:
            return None
        return int(item.data(256))

    def _selected_source_type(self) -> str:
        row = self.source_table.currentRow()
        if row < 0:
            return ""
        item = self.source_table.item(row, 0)
        if item is None:
            return ""
        return str(item.data(257) or "")

    def _sync_action_state(self) -> None:
        source_type = self._selected_source_type()
        self.manage_channels_button.setEnabled(source_type == "manual")

    def _prompt_remote_source(self) -> tuple[str, str]:
        url, accepted = QInputDialog.getText(self, "添加远程源", "M3U URL")
        if not accepted:
            return "", ""
        display_name, accepted = QInputDialog.getText(self, "添加远程源", "显示名称")
        return url.strip(), display_name.strip() if accepted else ""

    def _pick_local_source(self) -> tuple[str, str]:
        path, _ = QFileDialog.getOpenFileName(self, "选择 M3U 文件", "", "M3U Files (*.m3u *.m3u8)")
        if not path:
            return "", ""
        display_name, accepted = QInputDialog.getText(self, "添加本地源", "显示名称")
        return path.strip(), display_name.strip() if accepted else ""

    def _prompt_manual_source(self) -> str:
        display_name, accepted = QInputDialog.getText(self, "添加手动源", "显示名称")
        return display_name.strip() if accepted else ""

    def _add_remote_source(self) -> None:
        url, display_name = self._prompt_remote_source()
        if not url or not display_name:
            return
        self.manager.add_remote_source(url, display_name)
        self.reload_sources()

    def _add_local_source(self) -> None:
        path, display_name = self._pick_local_source()
        if not path or not display_name:
            return
        self.manager.add_local_source(path, display_name)
        self.reload_sources()

    def _add_manual_source(self) -> None:
        display_name = self._prompt_manual_source()
        if not display_name:
            return
        self.manager.add_manual_source(display_name)
        self.reload_sources()

    def _refresh_selected(self) -> None:
        source_id = self._selected_source_id()
        if source_id is None:
            return
        self.manager.refresh_source(source_id)
        self.reload_sources()
