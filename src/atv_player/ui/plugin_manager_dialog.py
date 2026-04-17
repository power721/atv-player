from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)


def _display_source_type(source_type: str) -> str:
    return {
        "local": "本地",
        "remote": "远程",
    }.get(source_type, source_type)


class PluginManagerDialog(QDialog):
    def __init__(self, plugin_manager, parent=None) -> None:
        super().__init__(parent)
        self.plugin_manager = plugin_manager
        self.setWindowTitle("插件管理")
        self.resize(920, 520)
        self.warning_label = QLabel("支持TvBox Python爬虫。远程插件会执行本地 Python 代码，请只加载受信任来源。")

        self.plugin_table = QTableWidget(0, 6, self)
        self.plugin_table.setHorizontalHeaderLabels(["名称", "来源", "地址", "启用", "状态", "最近加载"])
        self.plugin_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.plugin_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.plugin_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.add_local_button = QPushButton("添加本地插件")
        self.add_remote_button = QPushButton("添加远程插件")
        self.rename_button = QPushButton("编辑名称")
        self.toggle_button = QPushButton("启用/禁用")
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.refresh_button = QPushButton("刷新")
        self.logs_button = QPushButton("查看日志")
        self.delete_button = QPushButton("删除")

        actions = QHBoxLayout()
        for button in (
            self.add_local_button,
            self.add_remote_button,
            self.rename_button,
            self.toggle_button,
            self.up_button,
            self.down_button,
            self.refresh_button,
            self.logs_button,
            self.delete_button,
        ):
            actions.addWidget(button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.warning_label)
        layout.addLayout(actions)
        layout.addWidget(self.plugin_table)

        self.add_local_button.clicked.connect(self._add_local_plugin)
        self.add_remote_button.clicked.connect(self._add_remote_plugin)
        self.rename_button.clicked.connect(self._rename_selected)
        self.toggle_button.clicked.connect(self._toggle_selected_enabled)
        self.up_button.clicked.connect(lambda: self._move_selected(-1))
        self.down_button.clicked.connect(lambda: self._move_selected(1))
        self.refresh_button.clicked.connect(self._refresh_selected)
        self.logs_button.clicked.connect(self._show_logs)
        self.delete_button.clicked.connect(self._delete_selected)
        self.plugin_table.itemSelectionChanged.connect(self._sync_action_state)

        self.reload_plugins()

    def reload_plugins(self) -> None:
        plugins = self.plugin_manager.list_plugins()
        self.plugin_table.setRowCount(len(plugins))
        for row, plugin in enumerate(plugins):
            name_item = QTableWidgetItem(plugin.display_name or "")
            name_item.setData(256, plugin.id)
            self.plugin_table.setItem(row, 0, name_item)
            self.plugin_table.setItem(row, 1, QTableWidgetItem(_display_source_type(plugin.source_type)))
            self.plugin_table.setItem(row, 2, QTableWidgetItem(plugin.source_value))
            self.plugin_table.setItem(row, 3, QTableWidgetItem("是" if plugin.enabled else "否"))
            self.plugin_table.setItem(row, 4, QTableWidgetItem(plugin.last_error or "正常"))
            loaded_at = ""
            if plugin.last_loaded_at:
                loaded_at = datetime.fromtimestamp(plugin.last_loaded_at).strftime("%Y-%m-%d %H:%M:%S")
            self.plugin_table.setItem(row, 5, QTableWidgetItem(loaded_at))
        self._sync_action_state()

    def _has_selection(self) -> bool:
        selection_model = self.plugin_table.selectionModel()
        return bool(selection_model is not None and selection_model.hasSelection())

    def _sync_action_state(self) -> None:
        has_selection = self._has_selection()
        row = self.plugin_table.currentRow()
        last_row = self.plugin_table.rowCount() - 1
        self.rename_button.setEnabled(has_selection)
        self.toggle_button.setEnabled(has_selection)
        self.up_button.setEnabled(has_selection and row > 0)
        self.down_button.setEnabled(has_selection and row >= 0 and row < last_row)
        self.refresh_button.setEnabled(has_selection)
        self.logs_button.setEnabled(has_selection)
        self.delete_button.setEnabled(has_selection)

    def _selected_plugin_id(self) -> int | None:
        row = self.plugin_table.currentRow()
        if row < 0:
            return None
        item = self.plugin_table.item(row, 0)
        if item is None:
            return None
        return int(item.data(256))

    def _prompt_display_name(self, current: str) -> str:
        value, accepted = QInputDialog.getText(self, "编辑名称", "显示名称", text=current)
        return value.strip() if accepted else ""

    def _pick_local_plugin_path(self) -> str:
        path, _ = QFileDialog.getOpenFileName(self, "选择 Python 插件", "", "Python Files (*.py)")
        return path.strip()

    def _prompt_remote_url(self) -> str:
        value, accepted = QInputDialog.getText(self, "添加远程插件", "Python 文件 URL")
        return value.strip() if accepted else ""

    def _add_local_plugin(self) -> None:
        path = self._pick_local_plugin_path()
        if not path:
            return
        self.plugin_manager.add_local_plugin(path)
        self.reload_plugins()

    def _add_remote_plugin(self) -> None:
        url = self._prompt_remote_url()
        if not url:
            return
        self.plugin_manager.add_remote_plugin(url)
        self.reload_plugins()

    def _rename_selected(self) -> None:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return
        current_item = self.plugin_table.item(self.plugin_table.currentRow(), 0)
        if current_item is None:
            return
        current = current_item.text()
        display_name = self._prompt_display_name(current)
        if not display_name:
            return
        self.plugin_manager.rename_plugin(plugin_id, display_name)
        self.reload_plugins()

    def _toggle_selected_enabled(self) -> None:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return
        enabled_item = self.plugin_table.item(self.plugin_table.currentRow(), 3)
        if enabled_item is None:
            return
        enabled_text = enabled_item.text()
        self.plugin_manager.set_plugin_enabled(plugin_id, enabled_text != "是")
        self.reload_plugins()

    def _move_selected(self, direction: int) -> None:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return
        self.plugin_manager.move_plugin(plugin_id, direction)
        self.reload_plugins()

    def _refresh_selected(self) -> None:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return
        self.plugin_manager.refresh_plugin(plugin_id)
        self.reload_plugins()

    def _delete_selected(self) -> None:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return
        self.plugin_manager.delete_plugin(plugin_id)
        self.reload_plugins()

    def _show_logs(self) -> None:
        plugin_id = self._selected_plugin_id()
        if plugin_id is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("插件日志")
        dialog.resize(680, 420)
        view = QTextEdit(dialog)
        view.setReadOnly(True)
        lines = [f"[{entry.level}] {entry.message}" for entry in self.plugin_manager.list_logs(plugin_id)]
        view.setPlainText("\n".join(lines))
        layout = QVBoxLayout(dialog)
        layout.addWidget(view)
        dialog.exec()
