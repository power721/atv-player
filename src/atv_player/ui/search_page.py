from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from atv_player.api import ApiError, UnauthorizedError
from atv_player.controllers.browse_controller import filter_search_results
from atv_player.models import VodItem
from atv_player.ui.filter_options import SEARCH_DRIVE_FILTER_OPTIONS
from atv_player.ui.table_utils import configure_table_columns


class _SearchSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)
    unauthorized = Signal(int)


class _ResolveSignals(QObject):
    succeeded = Signal(int, str)
    failed = Signal(int, str)
    unauthorized = Signal(int)


class SearchPage(QWidget):
    browse_requested = Signal(str)
    unauthorized = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.keyword_edit = QLineEdit()
        self.filter_combo = QComboBox()
        self.search_button = QPushButton("搜索")
        self.clear_button = QPushButton("清空")
        self.results_table = QTableWidget(0, 2)
        self.results_table.setHorizontalHeaderLabels(["来源", "名称"])
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        configure_table_columns(self.results_table, stretch_column=1)
        self.status_label = QLineEdit()
        self.status_label.setReadOnly(True)
        self.status_label.setPlaceholderText("搜索电报资源")
        self._results: list[VodItem] = []
        self._filtered_results: list[VodItem] = []
        self._search_request_id = 0
        self._resolve_request_id = 0
        self._search_signals = _SearchSignals(self)
        self._search_signals.succeeded.connect(self._handle_search_succeeded)
        self._search_signals.failed.connect(self._handle_search_failed)
        self._search_signals.unauthorized.connect(self._handle_search_unauthorized)
        self._resolve_signals = _ResolveSignals(self)
        self._resolve_signals.succeeded.connect(self._handle_resolve_succeeded)
        self._resolve_signals.failed.connect(self._handle_resolve_failed)
        self._resolve_signals.unauthorized.connect(self._handle_resolve_unauthorized)

        for label, value in SEARCH_DRIVE_FILTER_OPTIONS:
            self.filter_combo.addItem(label, value)

        top = QHBoxLayout()
        top.addWidget(self.keyword_edit)
        top.addWidget(self.filter_combo)
        top.addWidget(self.search_button)
        top.addWidget(self.clear_button)

        self.content_container = QWidget()
        self.content_container.setMaximumWidth(1400)
        self.content_container.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        content_layout = QVBoxLayout(self.content_container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addLayout(top)
        content_layout.addWidget(self.status_label)
        content_layout.addWidget(self.results_table)

        outer_layout = QHBoxLayout(self)
        outer_layout.addStretch(1)
        outer_layout.addWidget(self.content_container, 100)
        outer_layout.addStretch(1)

        self.search_button.clicked.connect(self.search)
        self.clear_button.clicked.connect(self.clear_results)
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        self.results_table.cellDoubleClicked.connect(self._open_selected)

    def search(self) -> None:
        keyword = self.keyword_edit.text().strip()
        self._search_request_id += 1
        self._resolve_request_id += 1
        request_id = self._search_request_id
        self._results = []
        self._filtered_results = []
        self.results_table.setRowCount(0)
        self._set_search_loading(True)

        def run() -> None:
            try:
                results = self.controller.search(keyword)
            except UnauthorizedError:
                self._search_signals.unauthorized.emit(request_id)
                return
            except ApiError as exc:
                self._search_signals.failed.emit(request_id, str(exc))
                return
            self._search_signals.succeeded.emit(request_id, results)

        threading.Thread(target=run, daemon=True).start()

    def clear_results(self) -> None:
        self._search_request_id += 1
        self._resolve_request_id += 1
        self.keyword_edit.clear()
        self._results = []
        self._filtered_results = []
        self.results_table.setRowCount(0)
        self.status_label.clear()
        self._set_search_loading(False)

    def _apply_filter(self) -> None:
        drive_type = self.filter_combo.currentData()
        self._filtered_results = filter_search_results(self._results, drive_type or "")
        self.results_table.setRowCount(len(self._filtered_results))
        for row, item in enumerate(self._filtered_results):
            self.results_table.setItem(row, 0, QTableWidgetItem(item.type_name))
            name_item = QTableWidgetItem(item.vod_name)
            name_item.setToolTip(item.vod_name)
            self.results_table.setItem(row, 1, name_item)

    def _open_selected(self, row: int, _column: int) -> None:
        if not hasattr(self, "_filtered_results") or not (0 <= row < len(self._filtered_results)):
            return
        item = self._filtered_results[row]
        self._resolve_request_id += 1
        request_id = self._resolve_request_id
        self.status_label.setText("打开中...")

        def run() -> None:
            try:
                path = self.controller.resolve_search_result(item)
            except UnauthorizedError:
                self._resolve_signals.unauthorized.emit(request_id)
                return
            except ApiError as exc:
                self._resolve_signals.failed.emit(request_id, str(exc))
                return
            self._resolve_signals.succeeded.emit(request_id, path)

        threading.Thread(target=run, daemon=True).start()

    def _set_search_loading(self, loading: bool) -> None:
        self.keyword_edit.setEnabled(not loading)
        self.search_button.setEnabled(not loading)
        self.filter_combo.setEnabled(not loading)
        self.clear_button.setEnabled(not loading)
        if loading:
            self.status_label.setText("搜索中...")

    def _handle_search_succeeded(self, request_id: int, results: list[VodItem]) -> None:
        if request_id != self._search_request_id:
            return
        self._set_search_loading(False)
        self._results = list(results)
        self.status_label.setText(f"{len(self._results)} 条结果")
        self._apply_filter()

    def _handle_search_failed(self, request_id: int, message: str) -> None:
        if request_id != self._search_request_id:
            return
        self._set_search_loading(False)
        self.status_label.setText(message)

    def _handle_search_unauthorized(self, request_id: int) -> None:
        if request_id != self._search_request_id:
            return
        self._set_search_loading(False)
        self.unauthorized.emit()

    def _handle_resolve_succeeded(self, request_id: int, path: str) -> None:
        if request_id != self._resolve_request_id:
            return
        self.browse_requested.emit(path)

    def _handle_resolve_failed(self, request_id: int, message: str) -> None:
        if request_id != self._resolve_request_id:
            return
        self.status_label.setText(message)

    def _handle_resolve_unauthorized(self, request_id: int) -> None:
        if request_id != self._resolve_request_id:
            return
        self.unauthorized.emit()
