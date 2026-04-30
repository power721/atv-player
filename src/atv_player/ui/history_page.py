from __future__ import annotations

from datetime import datetime
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from atv_player.api import ApiError, UnauthorizedError
from atv_player.models import HistoryRecord
from atv_player.ui.async_guard import AsyncGuardMixin
from atv_player.ui.table_utils import configure_table_columns


class _HistoryLoadSignals(QObject):
    succeeded = Signal(int, int, int, object, int)
    failed = Signal(int)
    unauthorized = Signal(int)


class _HistoryMutationSignals(QObject):
    succeeded = Signal(int, int)
    failed = Signal(int)
    unauthorized = Signal(int)


class HistoryPage(QWidget, AsyncGuardMixin):
    open_detail_requested = Signal(object)
    unauthorized = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self._init_async_guard()
        self.controller = controller
        self._initial_load_started = False
        self.delete_button = QPushButton("删除")
        self.clear_button = QPushButton("清空")
        self.refresh_button = QPushButton("刷新")
        self.prev_page_button = QPushButton("上一页")
        self.next_page_button = QPushButton("下一页")
        self.page_label = QLabel("第 1 / 1 页")
        self.page_size_combo = QComboBox()
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["标题", "集数", "当前播放", "进度", "时间", "来源"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        configure_table_columns(self.table, stretch_column=0)
        self.records: list[HistoryRecord] = []
        self.current_page = 1
        self.page_size = 100
        self.total_items = 0
        self._load_request_id = 0
        self._mutation_request_id = 0
        self._load_signals = _HistoryLoadSignals()
        self._connect_async_signal(self._load_signals.succeeded, self._handle_load_succeeded)
        self._connect_async_signal(self._load_signals.failed, self._handle_load_failed)
        self._connect_async_signal(self._load_signals.unauthorized, self._handle_load_unauthorized)
        self._mutation_signals = _HistoryMutationSignals()
        self._connect_async_signal(self._mutation_signals.succeeded, self._handle_mutation_succeeded)
        self._connect_async_signal(self._mutation_signals.failed, self._handle_mutation_failed)
        self._connect_async_signal(self._mutation_signals.unauthorized, self._handle_mutation_unauthorized)
        for size in ("20", "30", "50", "100"):
            self.page_size_combo.addItem(size, int(size))
        self.page_size_combo.setCurrentText(str(self.page_size))

        actions = QHBoxLayout()
        actions.addWidget(self.delete_button)
        actions.addWidget(self.clear_button)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        actions.addWidget(self.prev_page_button)
        actions.addWidget(self.page_label)
        actions.addWidget(self.next_page_button)
        actions.addWidget(self.page_size_combo)

        self.content_container = QWidget()
        self.content_container.setMaximumWidth(1800)
        self.content_container.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        content_layout = QVBoxLayout(self.content_container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addLayout(actions)
        content_layout.addWidget(self.table)

        layout = QHBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(self.content_container, 100)
        layout.addStretch(1)

        self.delete_button.clicked.connect(self.delete_selected)
        self.clear_button.clicked.connect(self.clear_all)
        self.refresh_button.clicked.connect(self.load_history)
        self.table.cellDoubleClicked.connect(self._open_selected)
        self.table.itemSelectionChanged.connect(self._sync_action_state)
        self.prev_page_button.clicked.connect(self.previous_page)
        self.next_page_button.clicked.connect(self.next_page)
        self.page_size_combo.currentIndexChanged.connect(self._change_page_size)
        self._update_pagination_controls()
        self._sync_action_state()

    def ensure_loaded(self) -> None:
        if self._initial_load_started:
            return
        self._initial_load_started = True
        self.load_history()

    def load_history(self) -> None:
        self._initial_load_started = True
        self._load_request_id += 1
        request_id = self._load_request_id
        page = self.current_page
        size = self.page_size

        def run() -> None:
            try:
                records, total = self.controller.load_page(page=page, size=size)
            except UnauthorizedError:
                if not self._can_deliver_worker_result():
                    return
                self._load_signals.unauthorized.emit(request_id)
                return
            except ApiError:
                if not self._can_deliver_worker_result():
                    return
                self._load_signals.failed.emit(request_id)
                return
            if not self._can_deliver_worker_result():
                return
            self._load_signals.succeeded.emit(request_id, page, size, records, total)

        threading.Thread(target=run, daemon=True).start()

    def delete_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows:
            return
        records = [self.records[row] for row in rows]
        next_page = (
            self.current_page - 1 if len(records) == len(self.records) and self.current_page > 1 else self.current_page
        )

        def run() -> None:
            try:
                self.controller.delete_many(records)
            except UnauthorizedError:
                if not self._can_deliver_worker_result():
                    return
                self._mutation_signals.unauthorized.emit(request_id)
                return
            except ApiError:
                if not self._can_deliver_worker_result():
                    return
                self._mutation_signals.failed.emit(request_id)
                return
            if not self._can_deliver_worker_result():
                return
            self._mutation_signals.succeeded.emit(request_id, next_page)

        self._mutation_request_id += 1
        request_id = self._mutation_request_id
        threading.Thread(target=run, daemon=True).start()

    def clear_all(self) -> None:
        records = list(self.records)
        self._mutation_request_id += 1
        request_id = self._mutation_request_id

        def run() -> None:
            try:
                self.controller.clear_page(records)
            except UnauthorizedError:
                if not self._can_deliver_worker_result():
                    return
                self._mutation_signals.unauthorized.emit(request_id)
                return
            except ApiError:
                if not self._can_deliver_worker_result():
                    return
                self._mutation_signals.failed.emit(request_id)
                return
            if not self._can_deliver_worker_result():
                return
            self._mutation_signals.succeeded.emit(request_id, 1)

        threading.Thread(target=run, daemon=True).start()

    def _open_selected(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self.records)):
            return
        self.open_detail_requested.emit(self.records[row])

    def _source_label(self, record: HistoryRecord) -> str:
        if record.source_kind == "spider_plugin":
            return record.source_name or record.source_plugin_name or "插件"
        if record.source_kind == "emby":
            return record.source_name or "Emby"
        if record.source_kind == "jellyfin":
            return record.source_name or "Jellyfin"
        if record.source_kind == "feiniu":
            return record.source_name or "飞牛影视"
        return "远程"

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

    def _sync_action_state(self) -> None:
        selection_model = self.table.selectionModel()
        has_selection = bool(selection_model is not None and selection_model.hasSelection())
        self.delete_button.setEnabled(has_selection)
        self.clear_button.setEnabled(bool(self.records))

    def _handle_load_succeeded(
        self,
        request_id: int,
        page: int,
        size: int,
        records: list[HistoryRecord],
        total: int,
    ) -> None:
        if not self._can_deliver_worker_result():
            return
        if request_id != self._load_request_id:
            return
        if page != self.current_page or size != self.page_size:
            return
        self.total_items = total
        self.records = list(records)
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            self.table.setItem(row, 0, QTableWidgetItem(record.vod_name))
            self.table.setItem(row, 1, QTableWidgetItem(self._format_episode(record.episode)))
            self.table.setItem(row, 2, QTableWidgetItem(record.vod_remarks))
            self.table.setItem(row, 3, QTableWidgetItem(self._format_duration(record.position)))
            self.table.setItem(row, 4, QTableWidgetItem(self._format_timestamp(record.create_time)))
            self.table.setItem(row, 5, QTableWidgetItem(self._source_label(record)))
        self._sync_action_state()
        self._update_pagination_controls()

    def _handle_load_failed(self, request_id: int) -> None:
        if not self._can_deliver_worker_result():
            return
        if request_id != self._load_request_id:
            return

    def _handle_load_unauthorized(self, request_id: int) -> None:
        if not self._can_deliver_worker_result():
            return
        if request_id != self._load_request_id:
            return
        self.unauthorized.emit()

    def _handle_mutation_succeeded(self, request_id: int, next_page: int) -> None:
        if not self._can_deliver_worker_result():
            return
        if request_id != self._mutation_request_id:
            return
        self.current_page = next_page
        self.load_history()

    def _handle_mutation_failed(self, request_id: int) -> None:
        if not self._can_deliver_worker_result():
            return
        if request_id != self._mutation_request_id:
            return

    def _handle_mutation_unauthorized(self, request_id: int) -> None:
        if not self._can_deliver_worker_result():
            return
        if request_id != self._mutation_request_id:
            return
        self.unauthorized.emit()

    def _can_deliver_worker_result(self) -> bool:
        return self._can_deliver_async_result()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._deactivate_async_guard()
        super().closeEvent(event)
