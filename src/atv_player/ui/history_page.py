from __future__ import annotations

from datetime import datetime
import inspect
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
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
from atv_player.ui.theme import FlatComboBox, build_pill_button_qss, build_search_line_edit_qss, current_tokens


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
    global_search_requested = Signal(str)
    favorite_requested = Signal(object)
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
        self.page_size_combo = FlatComboBox()
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["标题", "集数", "当前播放", "进度", "时间", "来源"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        configure_table_columns(self.table, stretch_column=0)
        self.records: list[HistoryRecord] = []
        self.current_page = 1
        self.page_size = 100
        self.total_items = 0
        self._load_request_id = 0
        self._mutation_request_id = 0
        self._keyword = ""
        self._source_kind = ""
        self._time_range = ""
        self._continue_watching = False
        self._external_results_active = False
        self._external_page_loader = None
        self._external_loading = False
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

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索标题...")
        self.search_edit.setClearButtonEnabled(True)
        tokens = current_tokens()
        self.search_edit.setStyleSheet(build_search_line_edit_qss(tokens))

        self.source_combo = FlatComboBox()
        for label, value in (
            ("全部来源", ""),
            ("追剧", "msub"),
            ("远程", "remote"),
            ("AList", "browse"),
            ("电报影视", "telegram"),
            ("电报频道", "telegram_channel"),
            ("插件", "spider_plugin"),
            ("Emby", "emby"),
            ("B站", "bilibili"),
            ("Jellyfin", "jellyfin"),
            ("飞牛影视", "feiniu"),
            ("全局解析", "direct_parse"),
        ):
            self.source_combo.addItem(label, value)

        self.time_combo = FlatComboBox()
        self.time_combo.addItem("全部时间", "")
        self.time_combo.addItem("最近7天", "7d")
        self.time_combo.addItem("最近30天", "30d")
        self._configure_filter_combo(self.source_combo, minimum_contents_length=8)
        self._configure_filter_combo(self.time_combo, minimum_contents_length=6)

        self.continue_button = QPushButton("继续观看")
        self.continue_button.setCheckable(True)
        self.continue_button.setStyleSheet(build_pill_button_qss(tokens, checked_accent=True))

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)

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

        filters = QHBoxLayout()
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(self.source_combo)
        filters.addWidget(self.time_combo)
        filters.addWidget(self.continue_button)
        content_layout.addLayout(filters)

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
        self.table.customContextMenuRequested.connect(self._handle_table_context_menu_requested)
        self.table.itemSelectionChanged.connect(self._sync_action_state)
        self.prev_page_button.clicked.connect(self.previous_page)
        self.next_page_button.clicked.connect(self.next_page)
        self.page_size_combo.currentIndexChanged.connect(self._change_page_size)
        self._search_timer.timeout.connect(self._apply_search)
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        self.time_combo.currentIndexChanged.connect(self._on_time_changed)
        self.continue_button.toggled.connect(self._on_continue_watching_toggled)
        self._update_pagination_controls()
        self._sync_action_state()

    def _configure_filter_combo(self, combo: QComboBox, *, minimum_contents_length: int) -> None:
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(minimum_contents_length)
        combo.setMaxVisibleItems(12)
        longest_label_width = max(combo.fontMetrics().horizontalAdvance(combo.itemText(index)) for index in range(combo.count()))
        left_padding = int(combo.property("flat_combo_left_padding") or 12)
        indicator_padding = int(combo.property("flat_combo_indicator_padding") or 40)
        combo.setMinimumWidth(longest_label_width + left_padding + indicator_padding)
        combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def _apply_theme(self) -> None:
        tokens = current_tokens()
        self.search_edit.setStyleSheet(build_search_line_edit_qss(tokens))
        self.continue_button.setStyleSheet(build_pill_button_qss(tokens, checked_accent=True))

    def ensure_loaded(self) -> None:
        if self._initial_load_started:
            return
        self._initial_load_started = True
        self.load_history()

    def load_history(self) -> None:
        self._initial_load_started = True
        self._external_results_active = False
        self._external_page_loader = None
        self._external_loading = False
        self._load_request_id += 1
        request_id = self._load_request_id
        page = self.current_page
        size = self.page_size
        keyword = self._keyword
        source_kind = self._source_kind
        time_range = self._time_range
        continue_watching = self._continue_watching

        def run() -> None:
            try:
                records, total = self._load_page_from_controller(
                    page=page,
                    size=size,
                    keyword=keyword,
                    source_kind=source_kind,
                    time_range=time_range,
                    continue_watching=continue_watching,
                )
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

    def _load_page_from_controller(
        self,
        *,
        page: int,
        size: int,
        keyword: str,
        source_kind: str,
        time_range: str,
        continue_watching: bool,
    ) -> tuple[list[HistoryRecord], int]:
        load_page = self.controller.load_page
        parameters = inspect.signature(load_page).parameters.values()
        accepts_var_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
        if accepts_var_kwargs:
            return load_page(
                page=page,
                size=size,
                keyword=keyword,
                source_kind=source_kind,
                time_range=time_range,
                continue_watching=continue_watching,
            )
        supported_keys = {parameter.name for parameter in parameters}
        kwargs = {"page": page, "size": size}
        optional_kwargs = {
            "keyword": keyword,
            "source_kind": source_kind,
            "time_range": time_range,
            "continue_watching": continue_watching,
        }
        for key, value in optional_kwargs.items():
            if key in supported_keys:
                kwargs[key] = value
        return load_page(**kwargs)

    def delete_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows:
            return
        records = [self.records[row] for row in rows]
        self._delete_records(records)

    def _delete_records(self, records: list[HistoryRecord]) -> None:
        if not records:
            return
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

    def _build_row_context_menu(self, row: int) -> QMenu:
        del row
        menu = QMenu(self)
        open_action = menu.addAction("打开播放")
        open_action.setData("open")
        search_action = menu.addAction("全局搜索")
        search_action.setData("search")
        favorite_action = menu.addAction("加入收藏")
        favorite_action.setData("favorite")
        delete_action = menu.addAction("删除记录")
        delete_action.setData("delete")
        return menu

    def _handle_table_context_menu_requested(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if not (0 <= row < len(self.records)):
            return
        menu = self._build_row_context_menu(row)
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        self._handle_row_context_action(row, str(chosen.data() or ""))

    def _handle_row_context_action(self, row: int, action_id: str) -> None:
        if not (0 <= row < len(self.records)):
            return
        record = self.records[row]
        if action_id == "open":
            self.open_detail_requested.emit(record)
            return
        if action_id == "search":
            keyword = str(record.vod_name or "").strip()
            if keyword:
                self.global_search_requested.emit(keyword)
            return
        if action_id == "favorite":
            self.favorite_requested.emit(record)
            return
        if action_id == "delete":
            self._delete_records([record])

    def _on_search_text_changed(self) -> None:
        self._search_timer.start()

    def _apply_search(self) -> None:
        keyword = self.search_edit.text().strip()
        if keyword == self._keyword:
            return
        self._keyword = keyword
        self.current_page = 1
        self.load_history()

    def _on_source_changed(self) -> None:
        self._source_kind = self.source_combo.currentData() or ""
        self.current_page = 1
        self.load_history()

    def _on_time_changed(self) -> None:
        self._time_range = self.time_combo.currentData() or ""
        self.current_page = 1
        self.load_history()

    def _on_continue_watching_toggled(self, checked: bool) -> None:
        self._continue_watching = checked
        self.current_page = 1
        self.load_history()

    def _source_label(self, record: HistoryRecord) -> str:
        if record.source_kind == "msub":
            return "追剧"
        if record.source_kind == "telegram":
            return record.source_name or "电报影视"
        if record.source_kind == "browse":
            return record.source_name or "AList"
        if record.source_kind == "telegram_channel":
            return record.source_name or "电报频道"
        if record.source_kind == "spider_plugin":
            return record.source_name or record.source_plugin_name or "插件"
        if record.source_kind == "emby":
            return record.source_name or "Emby"
        if record.source_kind == "bilibili":
            return record.source_name or "B站"
        if record.source_kind == "youtube":
            return record.source_name or "YouTube"
        if record.source_kind == "jellyfin":
            return record.source_name or "Jellyfin"
        if record.source_kind == "feiniu":
            return record.source_name or "飞牛影视"
        if record.source_kind == "direct_parse":
            return record.source_name or "全局解析"
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
        if self._external_results_active:
            if self._external_page_loader is None or self._external_loading or self.current_page <= 1:
                return
            self._external_loading = True
            self._update_pagination_controls()
            self._external_page_loader(self.current_page - 1)
            return
        if self.current_page <= 1:
            return
        self.current_page -= 1
        self.load_history()

    def next_page(self) -> None:
        if self._external_results_active:
            if self._external_page_loader is None or self._external_loading or self.current_page >= self._total_pages():
                return
            self._external_loading = True
            self._update_pagination_controls()
            self._external_page_loader(self.current_page + 1)
            return
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
        if self._external_results_active:
            pagination_enabled = self._external_page_loader is not None and not self._external_loading
            self.prev_page_button.setEnabled(pagination_enabled and self.current_page > 1)
            self.next_page_button.setEnabled(pagination_enabled and self.current_page < total_pages)
            return
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
        self._render_records(records, total, page=page)

    def show_external_results(self, records: list[HistoryRecord], total: int, page: int = 1, page_loader=None) -> None:
        self._external_results_active = True
        self._external_page_loader = page_loader
        self._external_loading = False
        self._render_records(records, total, page=page)

    def clear_external_results(self) -> None:
        if not self._external_results_active:
            return
        self._external_results_active = False
        self._external_page_loader = None
        self._external_loading = False
        self.records = []
        self.total_items = 0
        self.current_page = 1
        self.table.setRowCount(0)
        self._sync_action_state()
        self._update_pagination_controls()

    def _render_records(self, records: list[HistoryRecord], total: int, *, page: int) -> None:
        self.current_page = page
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
