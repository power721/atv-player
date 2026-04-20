from __future__ import annotations
from pathlib import Path
from urllib.parse import unquote, urlparse

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from atv_player.time_utils import format_refresh_timestamp
from atv_player.ui.manual_live_source_dialog import ManualLiveSourceDialog


def _display_source_type(source_type: str) -> str:
    return {
        "remote": "远程",
        "local": "本地",
        "manual": "手动",
    }.get(source_type, source_type)


class _EpgRefreshSignals(QObject):
    completed = Signal()


class _EpgRefreshWorker(QObject):
    finished = Signal()

    def __init__(self, manager) -> None:
        super().__init__()
        self._manager = manager

    def run(self) -> None:
        try:
            self._manager.refresh_epg()
        finally:
            self.finished.emit()


class LiveSourceManagerDialog(QDialog):
    def __init__(self, manager, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("直播源管理")
        self.resize(920, 520)
        self._epg_refresh_thread: QThread | None = None
        self._epg_refresh_worker: _EpgRefreshWorker | None = None
        self._epg_refresh_signals = _EpgRefreshSignals(self)
        self._epg_refresh_signals.completed.connect(self._load_epg_config)
        self.epg_url_edit = QPlainTextEdit()
        self.epg_url_edit.setPlaceholderText(
            "https://example.com/epg.xml\nhttps://example.com/backup.xml.gz"
        )
        self.epg_url_edit.setFixedHeight(72)
        self.save_epg_button = QPushButton("保存")
        self.refresh_epg_button = QPushButton("立即更新")
        self.epg_status_label = QLabel("")
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
        self.rename_button = QPushButton("重命名")
        self.delete_button = QPushButton("删除")
        self.toggle_button = QPushButton("启用/禁用")
        self.manage_channels_button = QPushButton("管理频道")
        self.refresh_button = QPushButton("刷新")
        actions = QHBoxLayout()
        for button in (
            self.add_remote_button,
            self.add_local_button,
            self.add_manual_button,
            self.rename_button,
            self.delete_button,
            self.toggle_button,
            self.manage_channels_button,
            self.refresh_button,
        ):
            actions.addWidget(button)
        layout = QVBoxLayout(self)
        epg_row = QHBoxLayout()
        epg_row.addWidget(QLabel("EPG URL（每行一个）"))
        epg_row.addWidget(self.epg_url_edit, 1)
        epg_row.addWidget(self.save_epg_button)
        epg_row.addWidget(self.refresh_epg_button)
        layout.addLayout(epg_row)
        layout.addWidget(self.epg_status_label)
        layout.addLayout(actions)
        layout.addWidget(self.source_table)
        self.save_epg_button.clicked.connect(self._save_epg_url)
        self.refresh_epg_button.clicked.connect(self._refresh_epg)
        self.add_remote_button.clicked.connect(self._add_remote_source)
        self.add_local_button.clicked.connect(self._add_local_source)
        self.add_manual_button.clicked.connect(self._add_manual_source)
        self.rename_button.clicked.connect(self._rename_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        self.toggle_button.clicked.connect(self._toggle_selected_enabled)
        self.manage_channels_button.clicked.connect(self._manage_selected_channels)
        self.refresh_button.clicked.connect(self._refresh_selected)
        self.source_table.itemSelectionChanged.connect(self._sync_action_state)
        self._load_epg_config()
        self.reload_sources()

    def _load_epg_config(self) -> None:
        config = self.manager.load_epg_config()
        self.epg_url_edit.setPlainText(config.epg_url)
        self.epg_status_label.setText(config.last_error or format_refresh_timestamp(config.last_refreshed_at))

    def _save_epg_url(self) -> None:
        self.manager.save_epg_url(self._normalized_epg_url_text())
        self._load_epg_config()

    def _normalized_epg_url_text(self) -> str:
        lines: list[str] = []
        for line in self.epg_url_edit.toPlainText().splitlines():
            value = line.strip()
            if not value:
                continue
            lines.append(value)
        return "\n".join(lines)

    def reload_sources(self) -> None:
        sources = self.manager.list_sources()
        self.source_table.setRowCount(len(sources))
        for row, source in enumerate(sources):
            name_item = QTableWidgetItem(source.display_name)
            name_item.setData(256, source.id)
            name_item.setData(257, source.source_type)
            self.source_table.setItem(row, 0, name_item)
            self.source_table.setItem(row, 1, QTableWidgetItem(_display_source_type(source.source_type)))
            self.source_table.setItem(row, 2, QTableWidgetItem(source.source_value))
            self.source_table.setItem(row, 3, QTableWidgetItem("是" if source.enabled else "否"))
            self.source_table.setItem(row, 4, QTableWidgetItem(source.last_error or "正常"))
            self.source_table.setItem(row, 5, QTableWidgetItem(format_refresh_timestamp(source.last_refreshed_at)))
        self._sync_action_state()

    def _selected_source_id(self) -> int | None:
        if not self._has_selection():
            return None
        row = self.source_table.currentRow()
        if row < 0:
            return None
        item = self.source_table.item(row, 0)
        if item is None:
            return None
        return int(item.data(256))

    def _selected_source_type(self) -> str:
        if not self._has_selection():
            return ""
        row = self.source_table.currentRow()
        if row < 0:
            return ""
        item = self.source_table.item(row, 0)
        if item is None:
            return ""
        return str(item.data(257) or "")

    def _selected_source_name(self) -> str:
        if not self._has_selection():
            return ""
        row = self.source_table.currentRow()
        if row < 0:
            return ""
        item = self.source_table.item(row, 0)
        return item.text() if item is not None else ""

    def _has_selection(self) -> bool:
        selection_model = self.source_table.selectionModel()
        return bool(selection_model is not None and selection_model.hasSelection())

    def _sync_action_state(self) -> None:
        has_selection = self._has_selection()
        source_type = self._selected_source_type()
        self.rename_button.setEnabled(has_selection)
        self.delete_button.setEnabled(has_selection)
        self.toggle_button.setEnabled(has_selection)
        self.refresh_button.setEnabled(has_selection)
        self.manage_channels_button.setEnabled(source_type == "manual")

    def _name_from_local_source_path(self, path: str) -> str:
        return Path(path).stem

    def _name_from_remote_source_url(self, url: str) -> str:
        parsed = urlparse(url)
        path = unquote(parsed.path or "")
        if not path or path.endswith("/"):
            return "直播源"
        segment = Path(path).name
        name = Path(segment).stem if segment else ""
        return name or "直播源"

    def _prompt_remote_source(self) -> str:
        url, accepted = QInputDialog.getText(self, "添加远程源", "直播源 URL")
        return url.strip() if accepted else ""

    def _pick_local_source(self) -> str:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择直播源文件",
            "",
            "Live Source Files (*.m3u *.m3u8 *.txt)",
        )
        return path.strip()

    def _prompt_manual_source(self) -> str:
        display_name, accepted = QInputDialog.getText(self, "添加手动源", "显示名称")
        return display_name.strip() if accepted else ""

    def _prompt_rename_source(self, current_name: str) -> str:
        display_name, accepted = QInputDialog.getText(self, "重命名直播源", "显示名称", text=current_name)
        return display_name.strip() if accepted else ""

    def _confirm_delete_source(self, source_name: str) -> bool:
        return (
            QMessageBox.question(
                self,
                "删除直播源",
                f"确定删除直播源“{source_name}”吗？",
            )
            == QMessageBox.StandardButton.Yes
        )

    def _add_remote_source(self) -> None:
        url = self._prompt_remote_source()
        if not url:
            return
        self.manager.add_remote_source(url, self._name_from_remote_source_url(url))
        self.reload_sources()

    def _add_local_source(self) -> None:
        path = self._pick_local_source()
        if not path:
            return
        self.manager.add_local_source(path, self._name_from_local_source_path(path))
        self.reload_sources()

    def _add_manual_source(self) -> None:
        display_name = self._prompt_manual_source()
        if not display_name:
            return
        self.manager.add_manual_source(display_name)
        self.reload_sources()

    def _rename_selected(self) -> None:
        source_id = self._selected_source_id()
        if source_id is None:
            return
        display_name = self._prompt_rename_source(self._selected_source_name())
        if not display_name:
            return
        self.manager.rename_source(source_id, display_name)
        self.reload_sources()

    def _delete_selected(self) -> None:
        source_id = self._selected_source_id()
        if source_id is None:
            return
        source_name = self._selected_source_name()
        if not self._confirm_delete_source(source_name):
            return
        self.manager.delete_source(source_id)
        self.reload_sources()

    def _toggle_selected_enabled(self) -> None:
        source_id = self._selected_source_id()
        if source_id is None:
            return
        row = self.source_table.currentRow()
        enabled_item = self.source_table.item(row, 3)
        if enabled_item is None:
            return
        enabled_text = enabled_item.text()
        self.manager.set_source_enabled(source_id, enabled_text != "是")
        self.reload_sources()

    def _manage_selected_channels(self) -> None:
        source_id = self._selected_source_id()
        if source_id is None or self._selected_source_type() != "manual":
            return
        dialog = ManualLiveSourceDialog(self.manager, source_id=source_id, parent=self)
        dialog.reload_entries()
        dialog.exec()
        self.reload_sources()

    def _refresh_selected(self) -> None:
        source_id = self._selected_source_id()
        if source_id is None:
            return
        self.manager.refresh_source(source_id)
        self.reload_sources()

    def _refresh_epg(self) -> None:
        self.epg_status_label.setText("更新中...")
        thread = QThread(self)
        worker = _EpgRefreshWorker(self.manager)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(self._epg_refresh_signals.completed.emit)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_epg_refresh_state)
        self._epg_refresh_thread = thread
        self._epg_refresh_worker = worker
        thread.start()

    def _clear_epg_refresh_state(self) -> None:
        self._epg_refresh_thread = None
        self._epg_refresh_worker = None
