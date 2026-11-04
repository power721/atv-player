import threading
import types

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QAbstractItemView, QComboBox, QHeaderView, QLabel, QSizePolicy

from atv_player.models import (
    SpiderPluginAction,
    SpiderPluginConfig,
    SpiderPluginImportCancelled,
    SpiderPluginImportProgress,
    SpiderPluginImportResult,
    SpiderPluginLogEntry,
)
import atv_player.ui.plugin_manager_dialog as plugin_manager_dialog_module
from atv_player.ui.plugin_manager_dialog import PluginManagerDialog
from atv_player.ui.theme import FlatComboBox, current_tokens


class _StubSignal:
    """假进度对话框用的 canceled 信号桩:批量导入已改后台线程,只发信号不碰控件。"""

    def connect(self, *args, **kwargs) -> None:
        pass


def _select_rows(dialog: PluginManagerDialog, *rows: int) -> None:
    dialog.plugin_table.clearSelection()
    selection_model = dialog.plugin_table.selectionModel()
    assert selection_model is not None
    for row in rows:
        index = dialog.plugin_table.model().index(row, 0)
        selection_model.select(
            index,
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )


def _visible_plugin_ids(dialog: PluginManagerDialog) -> list[int]:
    plugin_ids: list[int] = []
    for row in range(dialog.plugin_table.rowCount()):
        item = dialog.plugin_table.item(row, 0)
        assert item is not None
        plugin_ids.append(int(item.data(256)))
    return plugin_ids


class FakePluginManager:
    def __init__(self) -> None:
        self.plugins = [
            SpiderPluginConfig(
                id=1,
                source_type="local",
                source_value="/plugins/a.py",
                display_name="本地A",
                enabled=True,
                sort_order=0,
                config_text="token=local",
                plugin_version=3,
            ),
            SpiderPluginConfig(
                id=2,
                source_type="remote",
                source_value="https://example.com/b.py",
                display_name="远程B",
                enabled=False,
                sort_order=1,
                last_error="下载失败",
                config_text="token=remote\ncookie=1\n",
                plugin_version=7,
            ),
        ]
        self.logs = {
            2: [SpiderPluginLogEntry(id=1, plugin_id=2, level="error", message="下载失败", created_at=1713206400)]
        }
        self.rename_calls: list[tuple[int, str]] = []
        self.config_calls: list[tuple[int, str]] = []
        self.toggle_calls: list[tuple[int, bool]] = []
        self.move_calls: list[tuple[int, int]] = []
        self.refresh_calls: list[int] = []
        self.add_local_calls: list[str] = []
        self.add_remote_calls: list[str] = []
        self.import_calls: list[str] = []
        self.delete_calls: list[int] = []
        self.action_calls: list[tuple[int, str, object]] = []
        self.progress_events: list[tuple[str, int, int, str]] = []
        self.action_query_calls: list[int] = []
        self.cancel_callback_checks = 0
        self.cancel_result = SpiderPluginImportResult(imported_count=1, updated_count=0, skipped_count=0)
        self.import_result = SpiderPluginImportResult(imported_count=2, updated_count=1, skipped_count=3)
        self.actions = {
            1: [SpiderPluginAction(id="qr_login", label="扫码登录")],
            2: [
                SpiderPluginAction(
                    id="refresh_cookie",
                    label="刷新 Cookie",
                    enabled=False,
                    tooltip="需要先扫码登录",
                )
            ],
        }

    def list_plugins(self):
        return list(self.plugins)

    def add_local_plugin(self, path: str) -> None:
        self.add_local_calls.append(path)

    def add_remote_plugin(self, url: str) -> None:
        self.add_remote_calls.append(url)

    def import_plugins(self, source_url: str, *, progress_callback=None, cancel_callback=None):
        self.import_calls.append(source_url)
        if progress_callback is not None:
            for event in (
                SpiderPluginImportProgress(stage="resolve_repo", message="正在解析仓库信息"),
                SpiderPluginImportProgress(stage="fetch_manifest", message="正在读取 spiders_v2.json"),
                SpiderPluginImportProgress(stage="import_plugin", current=1, total=2, message="正在导入 py/a.txt"),
                SpiderPluginImportProgress(stage="import_plugin", current=2, total=2, message="正在导入 py/b.txt"),
            ):
                self.progress_events.append((event.stage, event.current, event.total, event.message))
                progress_callback(event)
                if cancel_callback is not None:
                    self.cancel_callback_checks += 1
                    if cancel_callback():
                        raise SpiderPluginImportCancelled(self.cancel_result)
        return self.import_result

    def rename_plugin(self, plugin_id: int, display_name: str) -> None:
        self.rename_calls.append((plugin_id, display_name))

    def set_plugin_config(self, plugin_id: int, config_text: str) -> None:
        self.config_calls.append((plugin_id, config_text))

    def set_plugin_enabled(self, plugin_id: int, enabled: bool) -> None:
        self.toggle_calls.append((plugin_id, enabled))
        for plugin in self.plugins:
            if int(plugin.id) == int(plugin_id):
                plugin.enabled = enabled
                break

    def move_plugin(self, plugin_id: int, direction: int) -> None:
        self.move_calls.append((plugin_id, direction))

    def reorder_plugins(self, plugin_ids_in_order: list[int]) -> None:
        by_id = {plugin.id: plugin for plugin in self.plugins}
        self.plugins = [by_id[plugin_id] for plugin_id in plugin_ids_in_order]
        for sort_order, plugin in enumerate(self.plugins):
            plugin.sort_order = sort_order

    def refresh_plugin(self, plugin_id: int) -> None:
        self.refresh_calls.append(plugin_id)

    def delete_plugin(self, plugin_id: int) -> None:
        self.delete_calls.append(plugin_id)
        self.plugins = [plugin for plugin in self.plugins if int(plugin.id) != int(plugin_id)]

    def list_logs(self, plugin_id: int):
        return self.logs.get(plugin_id, [])

    def list_plugin_actions(self, plugin_id: int):
        self.action_query_calls.append(plugin_id)
        return list(self.actions.get(plugin_id, []))

    def run_plugin_action(self, plugin_id: int, action_id: str, parent=None) -> None:
        self.action_calls.append((plugin_id, action_id, parent))


def test_plugin_manager_dialog_uses_custom_title_bar(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)

    assert dialog.title_bar().title_label.text() == "源管理"


def test_plugin_manager_dialog_hides_maximize_button(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)

    assert dialog.title_bar().maximize_button.isHidden() is True


def test_plugin_manager_dialog_has_builtin_and_plugin_tabs(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager(), builtin_tabs=[], save_builtin_tab_overrides=lambda _json: None)
    qtbot.addWidget(dialog)

    assert dialog.management_tabs.tabText(0) == "内置源管理"
    assert dialog.management_tabs.tabText(1) == "插件源管理"
    assert dialog.management_tabs.currentWidget() is dialog.plugin_tab_page


def test_builtin_tab_manager_places_actions_above_list(qtbot) -> None:
    dialog = PluginManagerDialog(
        FakePluginManager(),
        builtin_tabs=[{"key": "douban", "title": "豆瓣电影"}],
        save_builtin_tab_overrides=lambda _json: None,
    )
    qtbot.addWidget(dialog)
    dialog.management_tabs.setCurrentWidget(dialog.builtin_tab_page)
    dialog.show()
    qtbot.wait(50)

    assert dialog.builtin_top_button.y() < dialog.builtin_tab_list.y()
    assert dialog.builtin_save_button.y() < dialog.builtin_tab_list.y()


def test_plugin_manager_dialog_reduces_tab_top_spacing(qtbot) -> None:
    dialog = PluginManagerDialog(
        FakePluginManager(),
        builtin_tabs=[{"key": "douban", "title": "豆瓣电影"}],
        save_builtin_tab_overrides=lambda _json: None,
    )
    qtbot.addWidget(dialog)
    dialog.management_tabs.setCurrentWidget(dialog.builtin_tab_page)
    dialog.show()
    qtbot.wait(50)

    assert dialog.management_tabs.y() - dialog.title_bar().height() <= 12


def test_builtin_tab_manager_saves_hidden_renamed_ordered_rows(qtbot) -> None:
    saved: list[str] = []
    dialog = PluginManagerDialog(
        FakePluginManager(),
        builtin_tabs=[
            {"key": "douban", "title": "豆瓣电影"},
            {"key": "telegram", "title": "电报影视"},
            {"key": "history", "title": "播放记录"},
        ],
        builtin_tab_overrides_json='{"order":["history","douban","telegram"],"hidden":["telegram"],"renames":{"douban":"电影"}}',
        save_builtin_tab_overrides=saved.append,
    )
    qtbot.addWidget(dialog)

    assert [dialog.builtin_tab_list.item(index).text() for index in range(dialog.builtin_tab_list.count())] == [
        "播放记录",
        "豆瓣电影 -> 电影",
        "电报影视（已隐藏）",
    ]

    dialog.builtin_tab_list.setCurrentRow(1)
    dialog._move_builtin_tab_to_top()
    dialog.builtin_tab_list.setCurrentRow(1)
    dialog._toggle_builtin_tab_hidden()
    dialog._save_builtin_tabs()

    assert saved == ['{"order":["douban","history","telegram"],"hidden":["history","telegram"],"renames":{"douban":"电影"}}']
    assert dialog.builtin_tabs_dirty is True


def test_builtin_tab_manager_shows_original_name_for_renamed_rows(qtbot) -> None:
    dialog = PluginManagerDialog(
        FakePluginManager(),
        builtin_tabs=[{"key": "douban", "title": "豆瓣电影"}],
        builtin_tab_overrides_json='{"renames":{"douban":"豆瓣"}}',
        save_builtin_tab_overrides=lambda _json: None,
    )
    qtbot.addWidget(dialog)

    assert dialog.builtin_tab_list.item(0).text() == "豆瓣电影 -> 豆瓣"


def test_plugin_manager_copy_mentions_javascript(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)

    assert "Python/JavaScript" in dialog.warning_label.text()


def test_plugin_manager_local_picker_accepts_js(qtbot, monkeypatch) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    seen: dict[str, str] = {}

    def fake_get_open_file_name(parent, title, directory, file_filter):
        seen["title"] = title
        seen["filter"] = file_filter
        return "/tmp/plugin.js", ""

    monkeypatch.setattr(
        "atv_player.ui.plugin_manager_dialog.QFileDialog.getOpenFileName",
        fake_get_open_file_name,
    )

    assert dialog._pick_local_plugin_path() == "/tmp/plugin.js"
    assert seen["title"] == "选择爬虫插件"
    assert "*.js" in seen["filter"]
    assert "*.py" in seen["filter"]


def test_plugin_manager_dialog_shows_health_badge_from_store(qtbot, tmp_path) -> None:
    from atv_player.source_health import PLAY_OUTCOME_FAIL, PLAY_OUTCOME_SUCCESS, SourceHealthStore

    store = SourceHealthStore(tmp_path / "source-health.json")
    for _ in range(5):
        store.record_play("search:plugin:1", "success")
    for _ in range(6):
        store.record_play("search:plugin:2", "fail", error="x")
    del PLAY_OUTCOME_SUCCESS, PLAY_OUTCOME_FAIL

    dialog = PluginManagerDialog(
        FakePluginManager(),
        source_health_store=store,
    )
    qtbot.addWidget(dialog)
    dialog.show()

    health_0 = dialog.plugin_table.item(0, 7)
    health_1 = dialog.plugin_table.item(1, 7)
    assert health_0 is not None and health_0.text() == "●"
    assert health_1 is not None and health_1.text() == "●"
    # 第一行(健康)绿色,第二行(异常)红色
    from PySide6.QtGui import QColor

    assert health_0.foreground().color() == QColor("#0B8043")
    assert health_1.foreground().color() == QColor("#C62828")
    assert "搜索 成功" in health_0.toolTip()


def test_plugin_manager_dialog_renders_rows_and_status(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    assert dialog.plugin_table.rowCount() == 2
    assert dialog.plugin_table.item(0, 0).text() == "本地A"
    assert dialog.plugin_table.item(0, 1).text() == "本地"
    assert dialog.plugin_table.item(0, 2).text() == "3"
    assert dialog.plugin_table.item(1, 1).text() == "远程"
    assert dialog.plugin_table.item(1, 2).text() == "7"
    assert dialog.plugin_table.item(1, 5).text() == "下载失败"


def test_plugin_manager_dialog_renders_search_filter_sort_controls(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    tokens = current_tokens()

    assert dialog.search_input.placeholderText() == "搜索名称或地址"
    assert isinstance(dialog.enabled_filter_combo, FlatComboBox)
    assert dialog.enabled_filter_combo.currentText() == "全部"
    assert dialog.enabled_filter_combo.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    assert dialog.enabled_filter_combo.minimumContentsLength() == 6
    assert dialog.enabled_filter_combo.maxVisibleItems() == 12
    assert dialog.enabled_filter_combo.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Preferred
    assert dialog.enabled_filter_combo.styleSheet() != ""
    assert dialog.enabled_filter_combo.property("flat_combo_field_bg") == tokens.input_bg
    assert dialog.enabled_filter_combo.property("flat_combo_border_color") == tokens.input_border
    assert dialog.enabled_filter_combo.property("flat_combo_height") == 26
    assert isinstance(dialog.sort_combo, FlatComboBox)
    assert dialog.sort_combo.currentText() == "当前顺序"
    assert dialog.sort_combo.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    assert dialog.sort_combo.minimumContentsLength() == 6
    assert dialog.sort_combo.maxVisibleItems() == 12
    assert dialog.sort_combo.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Preferred
    assert dialog.sort_combo.styleSheet() != ""
    assert dialog.sort_combo.property("flat_combo_field_bg") == tokens.input_bg
    assert dialog.sort_combo.property("flat_combo_border_color") == tokens.input_border
    assert dialog.sort_combo.property("flat_combo_height") == 26
    assert dialog.clear_filters_button.text() == "清空"
    assert dialog.filters_layout.spacing() == 12


def test_plugin_manager_dialog_renders_bulk_import_button(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)

    assert dialog.import_github_button.text() == "批量导入"


def test_plugin_manager_dialog_filter_combos_reserve_minimum_width_for_longest_label(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)

    for combo in (dialog.enabled_filter_combo, dialog.sort_combo):
        longest_label_width = max(combo.fontMetrics().horizontalAdvance(combo.itemText(index)) for index in range(combo.count()))
        left_padding = int(combo.property("flat_combo_left_padding") or 12)
        indicator_padding = int(combo.property("flat_combo_indicator_padding") or 40)
        assert combo.minimumWidth() >= longest_label_width + left_padding + indicator_padding


def test_plugin_manager_dialog_uses_history_style_search_field_and_spacing(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(50)

    assert dialog.search_input.isClearButtonEnabled() is True
    assert dialog.search_input.styleSheet() != ""
    assert dialog.filters_layout.spacing() == 12
    assert abs(dialog.search_input.height() - dialog.enabled_filter_combo.height()) <= 2
    assert abs(dialog.search_input.height() - dialog.sort_combo.height()) <= 2


def test_plugin_manager_dialog_filter_bar_keeps_search_input_wider_than_filter_combos(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.resize(1200, 520)
    dialog.show()
    qtbot.wait(50)

    assert dialog.search_input.width() > dialog.enabled_filter_combo.width()
    assert dialog.search_input.width() > dialog.sort_combo.width()


def test_plugin_manager_dialog_places_filter_bar_between_plugin_actions_and_table(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(50)

    assert dialog.search_input.y() > dialog.plugin_actions_widget.y()
    assert dialog.search_input.y() < dialog.plugin_table.y()


def test_plugin_manager_dialog_searches_name_and_source_case_insensitively(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.search_input.setText("远程b")
    assert _visible_plugin_ids(dialog) == [2]

    dialog.search_input.setText(" EXAMPLE.COM/B.PY ")
    assert _visible_plugin_ids(dialog) == [2]


def test_plugin_manager_dialog_filters_enabled_state(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.enabled_filter_combo.setCurrentText("仅启用")
    assert _visible_plugin_ids(dialog) == [1]

    dialog.enabled_filter_combo.setCurrentText("仅禁用")
    assert _visible_plugin_ids(dialog) == [2]


def test_plugin_manager_dialog_sorts_by_name_without_mutating_manager_order(qtbot) -> None:
    manager = FakePluginManager()
    manager.plugins = [
        SpiderPluginConfig(
            id=1,
            source_type="local",
            source_value="/plugins/zeta.py",
            display_name="Zeta",
            enabled=True,
            sort_order=0,
            plugin_version=1,
        ),
        SpiderPluginConfig(
            id=2,
            source_type="remote",
            source_value="https://example.com/alpha.py",
            display_name="Alpha",
            enabled=True,
            sort_order=1,
            plugin_version=1,
        ),
        SpiderPluginConfig(
            id=3,
            source_type="local",
            source_value="/plugins/beta.py",
            display_name="Beta",
            enabled=False,
            sort_order=2,
            plugin_version=1,
        ),
    ]
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.sort_combo.setCurrentText("名称")

    assert _visible_plugin_ids(dialog) == [2, 3, 1]
    assert [plugin.id for plugin in manager.plugins] == [1, 2, 3]


def test_plugin_manager_dialog_sorts_by_recently_loaded_descending(qtbot) -> None:
    manager = FakePluginManager()
    manager.plugins[0].last_loaded_at = 1713206400
    manager.plugins[1].last_loaded_at = 1713292800
    manager.plugins.append(
        SpiderPluginConfig(
            id=3,
            source_type="local",
            source_value="/plugins/c.py",
            display_name="本地C",
            enabled=True,
            sort_order=2,
            plugin_version=1,
        )
    )
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.sort_combo.setCurrentText("最近加载")

    assert _visible_plugin_ids(dialog) == [2, 1, 3]


def test_plugin_manager_dialog_disables_move_buttons_when_view_sort_is_not_current_order(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(0)

    dialog.sort_combo.setCurrentText("名称")
    dialog._sync_action_state()

    assert dialog.up_button.isEnabled() is False
    assert dialog.down_button.isEnabled() is False
    assert dialog.reorder_button.isEnabled() is True


def test_plugin_manager_dialog_clear_button_restores_default_conditions(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.search_input.setText("远程")
    dialog.enabled_filter_combo.setCurrentText("仅禁用")
    dialog.sort_combo.setCurrentText("名称")
    dialog.clear_filters_button.click()

    assert dialog.search_input.text() == ""
    assert dialog.enabled_filter_combo.currentText() == "全部"
    assert dialog.sort_combo.currentText() == "当前顺序"
    assert _visible_plugin_ids(dialog) == [1, 2]


def test_plugin_manager_dialog_reload_preserves_active_view_conditions(qtbot) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.search_input.setText("远程")
    manager.plugins.append(
        SpiderPluginConfig(
            id=3,
            source_type="local",
            source_value="/plugins/c.py",
            display_name="本地C",
            enabled=True,
            sort_order=2,
            plugin_version=1,
        )
    )
    dialog.reload_plugins()

    assert dialog.search_input.text() == "远程"
    assert dialog.enabled_filter_combo.currentText() == "全部"
    assert dialog.sort_combo.currentText() == "当前顺序"
    assert _visible_plugin_ids(dialog) == [2]


def test_plugin_manager_dialog_restores_single_selection_when_item_remains_visible(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(1)

    dialog.search_input.setText("远程")

    assert dialog._selected_plugin_id() == 2


def test_plugin_manager_dialog_clears_selection_and_shows_empty_state_when_no_rows_match(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(0)

    dialog.search_input.setText("not-found")

    assert dialog.plugin_table.rowCount() == 0
    assert dialog._selected_plugin_id() is None
    assert dialog.empty_state_label.isHidden() is False
    assert dialog.rename_button.isEnabled() is False
    assert dialog.delete_button.isEnabled() is False


def test_plugin_manager_dialog_stretches_source_column_to_fill_width(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.resize(1200, 520)
    dialog.show()
    qtbot.wait(50)

    header = dialog.plugin_table.horizontalHeader()

    assert header.sectionResizeMode(3) == QHeaderView.ResizeMode.Stretch
    assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert header.sectionResizeMode(1) == QHeaderView.ResizeMode.Fixed
    assert header.sectionResizeMode(2) == QHeaderView.ResizeMode.Fixed
    assert header.sectionResizeMode(4) == QHeaderView.ResizeMode.Fixed
    assert header.sectionResizeMode(5) == QHeaderView.ResizeMode.Interactive
    assert header.sectionResizeMode(6) == QHeaderView.ResizeMode.Fixed
    assert dialog.plugin_table.columnWidth(1) >= 60
    assert dialog.plugin_table.columnWidth(2) >= 50
    assert dialog.plugin_table.columnWidth(4) >= 50
    assert dialog.plugin_table.columnWidth(5) >= 120
    assert dialog.plugin_table.columnWidth(6) >= 150
    assert dialog.plugin_table.viewport().width() >= 900


def test_plugin_manager_dialog_uses_read_only_row_selection_for_actionable_table(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)

    assert dialog.plugin_table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert dialog.plugin_table.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows
    assert dialog.plugin_table.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection


def test_plugin_manager_dialog_disables_row_actions_without_selection(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.plugin_table.clearSelection()

    assert dialog.rename_button.isEnabled() is False
    assert dialog.config_button.isEnabled() is False
    assert dialog.enable_button.isEnabled() is False
    assert dialog.disable_button.isEnabled() is False
    assert dialog.up_button.isEnabled() is False
    assert dialog.down_button.isEnabled() is False
    assert dialog.refresh_button.isEnabled() is False
    assert dialog.logs_button.isEnabled() is False
    assert dialog.delete_button.isEnabled() is False


def test_plugin_manager_dialog_exposes_explicit_enable_and_disable_buttons(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)

    assert dialog.enable_button.text() == "启用"
    assert dialog.disable_button.text() == "禁用"


def test_plugin_manager_dialog_disables_bulk_buttons_without_selection(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.plugin_table.clearSelection()
    dialog._sync_action_state()

    assert dialog.enable_button.isEnabled() is False
    assert dialog.disable_button.isEnabled() is False
    assert dialog.refresh_button.isEnabled() is False
    assert dialog.delete_button.isEnabled() is False


def test_plugin_manager_dialog_enables_bulk_buttons_based_on_selected_states(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    _select_rows(dialog, 0, 1)
    dialog._sync_action_state()

    assert dialog.enable_button.isEnabled() is True
    assert dialog.disable_button.isEnabled() is True
    assert dialog.refresh_button.isEnabled() is True
    assert dialog.delete_button.isEnabled() is True


def test_plugin_manager_dialog_keeps_single_item_actions_disabled_for_multi_selection(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    _select_rows(dialog, 0, 1)
    dialog._sync_action_state()

    assert dialog.rename_button.isEnabled() is False
    assert dialog.config_button.isEnabled() is False
    assert dialog.category_button.isEnabled() is False
    assert dialog.logs_button.isEnabled() is False


def test_plugin_manager_dialog_limits_non_delete_actions_to_single_selection(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    _select_rows(dialog, 0, 1)
    dialog._sync_action_state()

    assert dialog.rename_button.isEnabled() is False
    assert dialog.config_button.isEnabled() is False
    assert dialog.enable_button.isEnabled() is True
    assert dialog.disable_button.isEnabled() is True
    assert dialog.up_button.isEnabled() is False
    assert dialog.down_button.isEnabled() is False
    assert dialog.refresh_button.isEnabled() is True
    assert dialog.logs_button.isEnabled() is False
    assert dialog.delete_button.isEnabled() is True


def test_plugin_manager_dialog_exposes_category_management_button(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)

    assert dialog.category_button.text() == "分类管理"


def test_plugin_manager_dialog_enables_category_management_only_for_single_selection(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.plugin_table.clearSelection()
    dialog._sync_action_state()
    assert dialog.category_button.isEnabled() is False

    _select_rows(dialog, 0)
    dialog._sync_action_state()
    assert dialog.category_button.isEnabled() is True

    _select_rows(dialog, 0, 1)
    dialog._sync_action_state()
    assert dialog.category_button.isEnabled() is False


def test_plugin_manager_dialog_opens_category_manager_and_marks_tabs_dirty_on_accept(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(0)
    reload_calls: list[str] = []

    class FakeCategoryDialog:
        def __init__(self, plugin_manager, plugin_id: int, parent=None) -> None:
            assert plugin_manager is manager
            assert plugin_id == 1
            assert parent is dialog

        def exec(self) -> int:
            manager.plugins[0].category_overrides_json = '{"order":["tv"]}'
            return PluginManagerDialog.DialogCode.Accepted

    monkeypatch.setattr(dialog, "reload_plugins", lambda: reload_calls.append("reload"))
    monkeypatch.setattr(plugin_manager_dialog_module, "PluginCategoryManagerDialog", FakeCategoryDialog)

    dialog._open_category_manager_dialog()

    assert dialog.plugin_tabs_dirty is True
    assert reload_calls == ["reload"]


def test_plugin_manager_dialog_shows_empty_custom_action_state_without_selection(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.plugin_table.clearSelection()
    dialog._sync_action_state()

    assert [button.text() for button in dialog.plugin_action_buttons] == ["无动作"]
    assert isinstance(dialog.plugin_action_buttons[0], QLabel)
    assert dialog.plugin_action_buttons[0].testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents) is True


def test_plugin_manager_dialog_shows_disabled_no_action_button_when_plugin_has_no_custom_actions(qtbot) -> None:
    manager = FakePluginManager()
    manager.actions[1] = []
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(0)
    qtbot.wait(100)

    assert [button.text() for button in dialog.plugin_action_buttons] == ["无动作"]
    assert isinstance(dialog.plugin_action_buttons[0], QLabel)
    assert dialog.plugin_action_buttons[0].testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents) is True


def test_plugin_manager_dialog_does_not_accumulate_placeholder_action_widgets_across_refreshes(qtbot) -> None:
    manager = FakePluginManager()
    manager.actions[1] = []
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.plugin_table.selectRow(0)
    for _ in range(3):
        dialog._sync_action_state()
        qtbot.wait(100)

    placeholders = [widget for widget in dialog.plugin_actions_widget.findChildren(QLabel) if widget.text() == "无动作"]

    assert len(placeholders) == 1
    assert len(dialog.plugin_action_buttons) == 1
    assert dialog.plugin_action_buttons[0].text() == "无动作"


def test_plugin_manager_dialog_disables_move_buttons_at_table_edges(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.plugin_table.selectRow(0)
    dialog._sync_action_state()
    assert dialog.up_button.isEnabled() is False
    assert dialog.down_button.isEnabled() is True

    dialog.plugin_table.selectRow(1)
    dialog._sync_action_state()
    assert dialog.up_button.isEnabled() is True
    assert dialog.down_button.isEnabled() is False


def test_plugin_manager_dialog_exposes_reorder_button(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)

    assert dialog.reorder_button.text() == "调整顺序"


def test_plugin_manager_dialog_opens_reorder_dialog_and_reloads_on_accept(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    reload_calls: list[str] = []
    original_reload = dialog.reload_plugins

    def tracked_reload() -> None:
        reload_calls.append("reload")
        original_reload()

    class FakeReorderDialog:
        def __init__(self, plugin_manager, parent=None) -> None:
            assert plugin_manager is manager
            assert parent is dialog

        def exec(self) -> int:
            manager.reorder_plugins([2, 1])
            return PluginManagerDialog.DialogCode.Accepted

    monkeypatch.setattr(dialog, "reload_plugins", tracked_reload)
    monkeypatch.setattr(plugin_manager_dialog_module, "PluginReorderDialog", FakeReorderDialog)

    dialog._open_reorder_dialog()

    assert reload_calls == ["reload"]
    assert [plugin.id for plugin in manager.plugins] == [2, 1]


def test_plugin_manager_dialog_does_not_reload_when_reorder_dialog_is_cancelled(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    reload_calls: list[str] = []

    class FakeReorderDialog:
        def __init__(self, plugin_manager, parent=None) -> None:
            pass

        def exec(self) -> int:
            return PluginManagerDialog.DialogCode.Rejected

    monkeypatch.setattr(dialog, "reload_plugins", lambda: reload_calls.append("reload"))
    monkeypatch.setattr(plugin_manager_dialog_module, "PluginReorderDialog", FakeReorderDialog)

    dialog._open_reorder_dialog()

    assert reload_calls == []


def test_plugin_manager_dialog_renders_dynamic_plugin_action_buttons(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.plugin_table.selectRow(1)
    qtbot.wait(100)

    assert [button.text() for button in dialog.plugin_action_buttons] == ["刷新 Cookie"]
    assert dialog.plugin_action_buttons[0].isEnabled() is False
    assert dialog.plugin_action_buttons[0].toolTip() == "需要先扫码登录"
    assert dialog.plugin_action_buttons[0].autoDefault() is False
    assert dialog.plugin_action_buttons[0].isDefault() is False


def test_plugin_manager_dialog_actions_call_manager(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(1)

    monkeypatch.setattr(dialog, "_prompt_display_name", lambda current: "远程重命名")
    monkeypatch.setattr(dialog, "_pick_local_plugin_path", lambda: "/plugins/红果短剧.py")
    monkeypatch.setattr(dialog, "_prompt_remote_url", lambda: "https://example.com/红果短剧.py")
    dialog._add_local_plugin()
    dialog._add_remote_plugin()
    qtbot.waitUntil(
        lambda: manager.add_remote_calls == ["https://example.com/红果短剧.py"]
        and not dialog._add_remote_in_progress,
        timeout=5000,
    )
    dialog._rename_selected()
    dialog._toggle_selected_enabled()
    dialog._move_selected(-1)
    dialog._delete_selected()
    qtbot.waitUntil(lambda: manager.delete_calls == [2], timeout=1000)

    assert manager.add_local_calls == ["/plugins/红果短剧.py"]
    assert manager.add_remote_calls == ["https://example.com/红果短剧.py"]
    assert manager.rename_calls == [(2, "远程重命名")]
    assert manager.toggle_calls == [(2, True)]
    assert manager.move_calls == [(2, -1)]
    assert manager.delete_calls == [2]


def test_plugin_manager_dialog_bulk_enable_only_updates_disabled_plugins(qtbot) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    _select_rows(dialog, 0, 1)

    dialog._enable_selected()

    assert manager.toggle_calls == [(2, True)]


def test_plugin_manager_dialog_bulk_disable_only_updates_enabled_plugins(qtbot) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    _select_rows(dialog, 0, 1)

    dialog._disable_selected()

    assert manager.toggle_calls == [(1, False)]


def test_plugin_manager_dialog_bulk_disable_restores_visible_multi_selection(qtbot) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    _select_rows(dialog, 0, 1)

    dialog._disable_selected()

    assert manager.toggle_calls == [(1, False)]
    assert dialog._selected_plugin_ids() == [1, 2]


def test_plugin_manager_dialog_bulk_enable_preserves_filter_and_visible_selection(qtbot) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.enabled_filter_combo.setCurrentText("仅禁用")
    _select_rows(dialog, 0)

    dialog._enable_selected()

    assert dialog.enabled_filter_combo.currentText() == "仅禁用"
    assert _visible_plugin_ids(dialog) == []
    assert dialog._selected_plugin_ids() == []


def test_plugin_manager_dialog_refresh_runs_in_background_and_reloads_on_completion(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(1)

    reload_calls: list[str] = []

    def tracked_reload(selected_plugin_ids=None) -> None:
        reload_calls.append("reload")
        if selected_plugin_ids is None:
            PluginManagerDialog.reload_plugins(dialog)
        else:
            PluginManagerDialog.reload_plugins(dialog, selected_plugin_ids)

    monkeypatch.setattr(dialog, "reload_plugins", tracked_reload)

    started_threads: list[object] = []

    class FakeThread:
        def __init__(self, *, target, daemon) -> None:
            self.target = target
            self.daemon = daemon
            self.started = False
            started_threads.append(self)

        def start(self) -> None:
            self.started = True

    monkeypatch.setattr(
        plugin_manager_dialog_module,
        "threading",
        types.SimpleNamespace(Thread=FakeThread),
        raising=False,
    )

    dialog._refresh_selected()

    assert manager.refresh_calls == []
    assert len(started_threads) == 1
    assert started_threads[0].started is True
    assert dialog.refresh_button.isEnabled() is False

    started_threads[0].target()
    qtbot.waitUntil(lambda: manager.refresh_calls == [2], timeout=1000)
    qtbot.waitUntil(lambda: reload_calls == ["reload"], timeout=1000)
    assert dialog.refresh_button.isEnabled() is True


def test_plugin_manager_dialog_bulk_refresh_runs_selected_plugins_in_background(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    _select_rows(dialog, 0, 1)

    reload_calls: list[str] = []
    original_reload = dialog.reload_plugins

    def tracked_reload(selected_plugin_ids=None) -> None:
        reload_calls.append("reload")
        if selected_plugin_ids is None:
            original_reload()
        else:
            original_reload(selected_plugin_ids)

    started_threads: list[object] = []

    class FakeThread:
        def __init__(self, *, target, daemon) -> None:
            self.target = target
            self.daemon = daemon
            self.started = False
            started_threads.append(self)

        def start(self) -> None:
            self.started = True

    monkeypatch.setattr(dialog, "reload_plugins", tracked_reload)
    monkeypatch.setattr(
        plugin_manager_dialog_module,
        "threading",
        types.SimpleNamespace(Thread=FakeThread),
        raising=False,
    )

    dialog._refresh_selected()

    assert len(started_threads) == 1
    assert dialog.refresh_button.isEnabled() is False

    started_threads[0].target()
    qtbot.waitUntil(lambda: manager.refresh_calls == [1, 2], timeout=1000)
    qtbot.waitUntil(lambda: reload_calls == ["reload"], timeout=1000)


def test_plugin_manager_dialog_delete_runs_in_background_and_reloads_on_completion(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    _select_rows(dialog, 0, 1)

    reload_calls: list[str] = []
    original_reload = dialog.reload_plugins

    def tracked_reload(selected_plugin_ids=None) -> None:
        reload_calls.append("reload")
        if selected_plugin_ids is None:
            original_reload()
        else:
            original_reload(selected_plugin_ids)

    started_threads: list[object] = []

    class FakeThread:
        def __init__(self, *, target, daemon) -> None:
            self.target = target
            self.daemon = daemon
            self.started = False
            started_threads.append(self)

        def start(self) -> None:
            self.started = True

    monkeypatch.setattr(dialog, "reload_plugins", tracked_reload)
    monkeypatch.setattr(
        plugin_manager_dialog_module,
        "threading",
        types.SimpleNamespace(Thread=FakeThread),
        raising=False,
    )

    dialog._delete_selected()

    assert manager.delete_calls == []
    assert len(started_threads) == 1
    assert started_threads[0].started is True
    assert dialog.delete_button.isEnabled() is False

    started_threads[0].target()
    qtbot.waitUntil(lambda: manager.delete_calls == [1, 2], timeout=1000)
    qtbot.waitUntil(lambda: reload_calls == ["reload"], timeout=1000)


def test_plugin_manager_dialog_deletes_all_selected_plugins(qtbot) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    _select_rows(dialog, 0, 1)

    dialog._delete_selected()
    qtbot.waitUntil(lambda: manager.delete_calls == [1, 2], timeout=1000)

    assert manager.delete_calls == [1, 2]


def test_plugin_manager_dialog_bulk_delete_keeps_remaining_selected_rows_visible(qtbot) -> None:
    manager = FakePluginManager()
    manager.plugins.append(
        SpiderPluginConfig(
            id=3,
            source_type="local",
            source_value="/plugins/c.py",
            display_name="本地C",
            enabled=False,
            sort_order=2,
            plugin_version=1,
        )
    )
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    _select_rows(dialog, 1, 2)

    dialog._delete_selected()
    qtbot.waitUntil(lambda: manager.delete_calls == [2, 3], timeout=1000)
    qtbot.waitUntil(lambda: _visible_plugin_ids(dialog) == [1], timeout=1000)

    assert manager.delete_calls == [2, 3]
    assert _visible_plugin_ids(dialog) == [1]
    assert dialog._selected_plugin_ids() == []


def test_plugin_manager_dialog_imports_github_repository_with_progress_and_summary(qtbot, monkeypatch) -> None:
    progress_updates: list[tuple[int, int, str]] = []

    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    summary_messages: list[str] = []

    class FakeProgressDialog:
        def __init__(self, *args, **kwargs) -> None:
            self.values: list[int] = []
            self.maximums: list[int] = []
            self.labels: list[str] = []
            self.cancel_button = object()
            self.window_modality = None
            self.canceled = _StubSignal()

        def setWindowTitle(self, title: str) -> None:
            pass

        def setMinimumDuration(self, duration: int) -> None:
            pass

        def setAutoClose(self, auto_close: bool) -> None:
            pass

        def setAutoReset(self, auto_reset: bool) -> None:
            pass

        def setCancelButton(self, button) -> None:
            self.cancel_button = button

        def setWindowModality(self, modality) -> None:
            self.window_modality = modality

        def setRange(self, minimum: int, maximum: int) -> None:
            self.maximums.append(maximum)

        def setValue(self, value: int) -> None:
            self.values.append(value)

        def setLabelText(self, text: str) -> None:
            self.labels.append(text)
            progress_updates.append((self.values[-1] if self.values else 0, self.maximums[-1] if self.maximums else 0, text))

        def wasCanceled(self) -> bool:
            return False

        def show(self) -> None:
            pass

        def close(self) -> None:
            pass

        def deleteLater(self) -> None:
            pass

    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QProgressDialog", FakeProgressDialog)
    monkeypatch.setattr(dialog, "_prompt_import_source_url", lambda: "https://d.har01d.cn/spiders_v2.json")
    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QMessageBox.information", lambda *args: summary_messages.append(args[2]))

    dialog._import_plugins()
    qtbot.waitUntil(lambda: bool(summary_messages), timeout=5000)

    assert manager.import_calls == ["https://d.har01d.cn/spiders_v2.json"]
    assert progress_updates[0] == (0, 0, "正在准备导入...")
    assert progress_updates[-1] == (2, 2, "正在导入 py/b.txt")
    assert summary_messages == ["导入完成：新增 2 个，更新 1 个，跳过 3 个。"]


def test_plugin_manager_dialog_import_progress_is_modal_and_shows_cancel_text(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    captured: dict[str, object] = {}

    class FakeProgressDialog:
        def __init__(self, label_text: str, cancel_text: str, minimum: int, maximum: int, parent) -> None:
            captured["instance"] = self
            self.label_text = label_text
            self.cancel_text = cancel_text
            self.cancel_button = "present"
            self.window_modality = None
            self.canceled = _StubSignal()

        def setWindowTitle(self, title: str) -> None:
            pass

        def setMinimumDuration(self, duration: int) -> None:
            pass

        def setAutoClose(self, auto_close: bool) -> None:
            pass

        def setAutoReset(self, auto_reset: bool) -> None:
            pass

        def setCancelButton(self, button) -> None:
            self.cancel_button = button

        def setWindowModality(self, modality) -> None:
            self.window_modality = modality

        def setRange(self, minimum: int, maximum: int) -> None:
            pass

        def setValue(self, value: int) -> None:
            pass

        def setLabelText(self, text: str) -> None:
            pass

        def wasCanceled(self) -> bool:
            return False

        def show(self) -> None:
            pass

        def close(self) -> None:
            pass

        def deleteLater(self) -> None:
            pass

    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QProgressDialog", FakeProgressDialog)
    monkeypatch.setattr(dialog, "_prompt_import_source_url", lambda: "https://github.com/har01d5/tvbox")
    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QMessageBox.information", lambda *args: None)

    dialog._import_plugins()
    qtbot.waitUntil(lambda: manager.import_calls == ["https://github.com/har01d5/tvbox"], timeout=5000)

    progress = captured["instance"]
    assert progress.label_text == ""
    assert progress.cancel_text == "取消"
    assert progress.cancel_button == "present"
    assert progress.window_modality == Qt.WindowModality.WindowModal


def test_plugin_manager_dialog_reports_cancelled_import_and_reloads_plugins(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    info_messages: list[str] = []
    warning_messages: list[str] = []
    reload_calls: list[str] = []
    original_reload = dialog.reload_plugins

    def tracked_reload() -> None:
        reload_calls.append("reload")
        original_reload()

    class FakeProgressDialog:
        def __init__(self, *args, **kwargs) -> None:
            self.canceled = _StubSignal()

        def setWindowTitle(self, title: str) -> None:
            pass

        def setMinimumDuration(self, duration: int) -> None:
            pass

        def setAutoClose(self, auto_close: bool) -> None:
            pass

        def setAutoReset(self, auto_reset: bool) -> None:
            pass

        def setCancelButton(self, button) -> None:
            pass

        def setWindowModality(self, modality) -> None:
            pass

        def setRange(self, minimum: int, maximum: int) -> None:
            pass

        def setValue(self, value: int) -> None:
            pass

        def setLabelText(self, text: str) -> None:
            pass

        def wasCanceled(self) -> bool:
            return False

        def show(self) -> None:
            pass

        def close(self) -> None:
            pass

        def deleteLater(self) -> None:
            pass

    monkeypatch.setattr(dialog, "reload_plugins", tracked_reload)
    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QProgressDialog", FakeProgressDialog)
    monkeypatch.setattr(dialog, "_prompt_import_source_url", lambda: "https://github.com/har01d5/tvbox")
    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QMessageBox.information", lambda *args: info_messages.append(args[2]))
    monkeypatch.setattr("atv_player.ui.plugin_manager_dialog.QMessageBox.warning", lambda *args: warning_messages.append(args[2]))

    dialog._import_plugins()
    # 模拟用户点"取消":真实路径是进度框 canceled 信号→置位事件,worker 在条目边界感知。
    assert dialog._import_cancel_event is not None
    dialog._import_cancel_event.set()
    qtbot.waitUntil(lambda: bool(info_messages), timeout=5000)

    assert manager.import_calls == ["https://github.com/har01d5/tvbox"]
    assert manager.cancel_callback_checks > 0
    assert reload_calls == ["reload"]
    assert info_messages == ["已取消：新增 1 个，更新 0 个，跳过 0 个。"]
    assert warning_messages == []


def test_plugin_manager_dialog_ignores_reentrant_github_import_requests(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._import_in_progress = True

    monkeypatch.setattr(dialog, "_prompt_import_source_url", lambda: "https://github.com/har01d5/tvbox")

    dialog._import_plugins()

    assert manager.import_calls == []


def test_plugin_manager_dialog_add_remote_plugin_runs_off_ui_thread(qtbot, monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    worker_threads: list[str] = []

    class SlowRemoteManager(FakePluginManager):
        def add_remote_plugin(self, url: str) -> None:
            super().add_remote_plugin(url)
            worker_threads.append(threading.current_thread().name)
            started.set()
            release.wait(5)

    manager = SlowRemoteManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    monkeypatch.setattr(dialog, "_prompt_remote_url", lambda: "https://example.com/slow.py")

    dialog._add_remote_plugin()
    assert started.wait(5000)

    # busy 态:按钮禁用;进行中重复触发不叠加执行。
    assert not dialog.add_remote_button.isEnabled()
    dialog._add_remote_plugin()
    assert manager.add_remote_calls == ["https://example.com/slow.py"]

    release.set()
    qtbot.waitUntil(lambda: dialog.add_remote_button.isEnabled(), timeout=5000)

    assert manager.add_remote_calls == ["https://example.com/slow.py"]
    assert worker_threads and worker_threads[0] != threading.main_thread().name


def test_plugin_manager_dialog_dispatches_plugin_action_and_reloads_plugins(qtbot, monkeypatch) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(0)
    qtbot.wait(100)

    reload_calls: list[str] = []
    original_reload = dialog.reload_plugins

    def tracked_reload() -> None:
        reload_calls.append("reload")
        original_reload()

    monkeypatch.setattr(dialog, "reload_plugins", tracked_reload)

    qtbot.mouseClick(dialog.plugin_action_buttons[0], Qt.MouseButton.LeftButton)

    assert manager.action_calls == [(1, "qr_login", dialog)]
    assert reload_calls == ["reload"]


def test_plugin_manager_dialog_debounces_action_loading_to_final_selected_plugin(qtbot) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.plugin_table.selectRow(0)
    dialog.plugin_table.selectRow(1)
    dialog.plugin_table.selectRow(0)
    qtbot.wait(100)

    assert manager.action_query_calls == [1]
    assert [button.text() for button in dialog.plugin_action_buttons] == ["扫码登录"]


def test_plugin_manager_dialog_caches_loaded_actions_per_plugin_during_session(qtbot) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.plugin_table.selectRow(0)
    qtbot.wait(100)
    dialog.plugin_table.selectRow(1)
    qtbot.wait(100)
    dialog.plugin_table.selectRow(0)
    qtbot.wait(100)

    assert manager.action_query_calls == [1, 2]


def test_plugin_manager_dialog_edit_config_allows_empty_string_and_keeps_raw_current_value(
    qtbot,
    monkeypatch,
) -> None:
    manager = FakePluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(1)

    captured: list[str] = []

    def fake_prompt(current: str) -> str | None:
        captured.append(current)
        return ""

    monkeypatch.setattr(dialog, "_prompt_config_text", fake_prompt)

    dialog._edit_selected_config()

    assert captured == ["token=remote\ncookie=1\n"]
    assert manager.config_calls == [(2, "")]


def test_plugin_manager_dialog_keeps_selection_on_moved_plugin(qtbot) -> None:
    class ReorderingPluginManager(FakePluginManager):
        def move_plugin(self, plugin_id: int, direction: int) -> None:
            super().move_plugin(plugin_id, direction)
            index = next(i for i, plugin in enumerate(self.plugins) if plugin.id == plugin_id)
            target = index + direction
            if not (0 <= target < len(self.plugins)):
                return
            self.plugins[index], self.plugins[target] = self.plugins[target], self.plugins[index]
            for sort_order, plugin in enumerate(self.plugins):
                plugin.sort_order = sort_order

    manager = ReorderingPluginManager()
    dialog = PluginManagerDialog(manager)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.plugin_table.selectRow(0)

    dialog._move_selected(1)

    assert dialog.plugin_table.currentRow() == 1
    assert dialog._selected_plugin_id() == 1


def test_plugin_manager_dialog_focuses_search_input_when_shown(qtbot) -> None:
    dialog = PluginManagerDialog(FakePluginManager())
    qtbot.addWidget(dialog)

    dialog.show()
    qtbot.waitUntil(dialog.isVisible)
    qtbot.waitUntil(dialog.search_input.hasFocus)

    assert dialog.search_input.hasFocus() is True
