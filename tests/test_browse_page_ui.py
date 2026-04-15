import threading

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QSplitter

from atv_player.api import ApiError
from atv_player.models import AppConfig, HistoryRecord, VodItem
from atv_player.ui.browse_page import BrowsePage
from atv_player.ui.history_page import HistoryPage
from atv_player.ui.search_page import SearchPage


class FakeBrowseController:
    def __init__(self, total: int = 0) -> None:
        self.loaded_paths: list[str] = []
        self.load_calls: list[tuple[str, int, int]] = []
        self.total = total

    def load_folder(self, path: str, page: int = 1, size: int = 50):
        self.loaded_paths.append(path)
        self.load_calls.append((path, page, size))
        return [], self.total


class ErroringBrowseController(FakeBrowseController):
    def load_folder(self, path: str, page: int = 1, size: int = 50):
        raise ApiError("加载文件列表超时")


class FakeHistoryController:
    def load_page(self, page: int, size: int):
        return [], 0


class FakeSearchController:
    pass


class AsyncSearchController:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._main_thread_id = threading.get_ident()
        self._events_by_keyword: dict[str, list[threading.Event]] = {}
        self._results_by_keyword: dict[str, list[VodItem]] = {}

    def search(self, keyword: str) -> list[VodItem]:
        self.calls.append(keyword)
        assert threading.get_ident() != self._main_thread_id
        event = threading.Event()
        self._events_by_keyword.setdefault(keyword, []).append(event)
        assert event.wait(timeout=5), f"search for {keyword!r} was never released"
        return self._results_by_keyword.get(keyword, [])

    def finish(self, keyword: str, results: list[VodItem]) -> None:
        self._results_by_keyword[keyword] = results
        self._events_by_keyword[keyword].pop(0).set()


class RecordingSearchController(FakeBrowseController):
    def __init__(self) -> None:
        super().__init__()
        self.search_calls: list[str] = []

    def search(self, keyword: str):
        self.search_calls.append(keyword)
        return []


def test_browse_page_uses_split_view_for_search_and_file_list(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)

    assert isinstance(page.content_splitter, QSplitter)
    assert page.content_splitter.orientation() == Qt.Orientation.Horizontal
    assert page.content_splitter.indexOf(page.search_panel) == 0
    assert page.content_splitter.indexOf(page.file_panel) == 1
    assert page.results_table.columnCount() == 2
    assert page.table.columnCount() == 6


def test_browse_page_hides_search_results_panel_when_empty(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)
    page.show()

    assert page.search_panel.isHidden() is True
    assert page.filter_combo.isHidden() is True
    assert page.clear_button.isHidden() is True


def test_browse_page_shows_search_results_panel_at_one_quarter_width(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)
    page.resize(1200, 800)
    page.show()

    page._show_search_results_panel()

    left, right = page.content_splitter.sizes()
    assert 200 <= left <= 400
    assert right > left


def test_search_page_centers_content_container(qtbot) -> None:
    page = SearchPage(FakeSearchController())
    qtbot.addWidget(page)
    page.resize(2000, 900)
    page.show()
    qtbot.wait(50)

    container_center = page.content_container.geometry().center().x()
    page_center = page.rect().center().x()

    assert abs(container_center - page_center) <= 5


def test_browse_page_centers_content_container(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)
    page.resize(2200, 1000)
    page.show()
    qtbot.wait(50)

    container_center = page.content_container.geometry().center().x()
    page_center = page.rect().center().x()

    assert abs(container_center - page_center) <= 5


def test_history_page_centers_content_container(qtbot) -> None:
    page = HistoryPage(FakeHistoryController())
    qtbot.addWidget(page)
    page.resize(2200, 1000)
    page.show()
    qtbot.wait(50)

    container_center = page.content_container.geometry().center().x()
    page_center = page.rect().center().x()

    assert abs(container_center - page_center) <= 5


def test_history_page_exposes_refresh_button(qtbot) -> None:
    page = HistoryPage(FakeHistoryController())
    qtbot.addWidget(page)

    assert page.refresh_button.text() == "刷新"


def test_browse_page_persists_and_restores_content_splitter_state(qtbot) -> None:
    saved = {"count": 0}
    config = AppConfig()
    page = BrowsePage(FakeBrowseController(), config=config, save_config=lambda: saved.__setitem__("count", saved["count"] + 1))
    qtbot.addWidget(page)
    page.resize(1400, 900)
    page.show()

    page._show_search_results_panel()
    page.content_splitter.setSizes([320, 880])
    page._persist_content_splitter_state()

    assert config.browse_content_splitter_state is not None
    assert saved["count"] >= 1

    restored = BrowsePage(FakeBrowseController(), config=config, save_config=lambda: None)
    qtbot.addWidget(restored)
    restored.resize(1400, 900)
    restored.show()
    restored._show_search_results_panel()

    assert restored.content_splitter.saveState() == QByteArray(config.browse_content_splitter_state)


def test_browse_page_search_keyword_sets_input_and_starts_search(qtbot) -> None:
    controller = RecordingSearchController()
    page = BrowsePage(controller)
    qtbot.addWidget(page)
    page.show()

    page.search_keyword("霸王别姬")

    assert controller.search_calls == ["霸王别姬"]
    assert page.keyword_edit.text() == "霸王别姬"


def test_tables_are_read_only(qtbot) -> None:
    browse_page = BrowsePage(FakeBrowseController())
    history_page = HistoryPage(FakeHistoryController())
    qtbot.addWidget(browse_page)
    qtbot.addWidget(history_page)

    assert browse_page.results_table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert browse_page.table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert history_page.table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers


def test_browse_page_breadcrumb_click_loads_target_folder(qtbot) -> None:
    controller = FakeBrowseController()
    page = BrowsePage(controller)
    qtbot.addWidget(page)
    page.show()

    page.load_path("/电影/国产/动作")

    assert [button.text() for button in page.breadcrumb_buttons] == ["🏠首页", "电影", "国产", "动作"]

    controller.loaded_paths.clear()
    controller.load_calls.clear()
    page.breadcrumb_buttons[2].click()

    assert controller.loaded_paths == ["/电影/国产"]
    assert controller.load_calls == [("/电影/国产", 1, 50)]


def test_browse_page_shows_folder_timeout_in_breadcrumb_status(qtbot) -> None:
    page = BrowsePage(ErroringBrowseController())
    qtbot.addWidget(page)

    page.load_path("/电影")

    status_widget = page.breadcrumb_layout.itemAt(0).widget()
    assert status_widget.text() == "/电影 | 加载文件列表超时"
    assert page.table.rowCount() == 0


def test_browse_page_shows_size_dbid_and_rating_columns(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)

    page._populate_table(
        [
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Episode 1",
                "vod_time": "2026-04-14",
                "vod_remarks": "1.4 GB",
                "dbid": 123456,
            })(),
            type("Item", (), {
                "type": 1,
                "vod_tag": "folder",
                "vod_name": "Movie Folder",
                "vod_time": "2026-04-14",
                "vod_remarks": "8.6",
                "dbid": 654321,
            })(),
        ]
    )

    assert page.table.horizontalHeaderItem(2).text() == "大小"
    assert page.table.horizontalHeaderItem(3).text() == "豆瓣ID"
    assert page.table.horizontalHeaderItem(4).text() == "评分"
    assert page.table.item(0, 2).text() == "1.4 GB"
    assert page.table.item(0, 3).text() == "123456"
    assert page.table.item(0, 4).text() == ""
    assert page.table.item(1, 2).text() == "-"
    assert page.table.item(1, 3).text() == "654321"
    assert page.table.item(1, 4).text() == "8.6"
    assert page.table.item(0, 1).toolTip() == "Episode 1"
    assert page.table.item(1, 1).toolTip() == "Movie Folder"


def test_main_text_columns_stretch_and_other_columns_fit_content(qtbot) -> None:
    browse_page = BrowsePage(FakeBrowseController())
    history_page = HistoryPage(FakeHistoryController())
    search_page = SearchPage(FakeSearchController())
    qtbot.addWidget(browse_page)
    qtbot.addWidget(history_page)
    qtbot.addWidget(search_page)

    browse_results_header = browse_page.results_table.horizontalHeader()
    assert browse_results_header.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert browse_results_header.sectionResizeMode(1) == QHeaderView.ResizeMode.Stretch

    browse_files_header = browse_page.table.horizontalHeader()
    assert browse_files_header.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert browse_files_header.sectionResizeMode(1) == QHeaderView.ResizeMode.Stretch
    assert browse_files_header.sectionResizeMode(2) == QHeaderView.ResizeMode.ResizeToContents
    assert browse_files_header.sectionResizeMode(3) == QHeaderView.ResizeMode.ResizeToContents
    assert browse_files_header.sectionResizeMode(4) == QHeaderView.ResizeMode.ResizeToContents
    assert browse_files_header.sectionResizeMode(5) == QHeaderView.ResizeMode.ResizeToContents

    history_header = history_page.table.horizontalHeader()
    assert history_header.sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch
    assert history_header.sectionResizeMode(1) == QHeaderView.ResizeMode.ResizeToContents
    assert history_header.sectionResizeMode(2) == QHeaderView.ResizeMode.ResizeToContents
    assert history_header.sectionResizeMode(3) == QHeaderView.ResizeMode.ResizeToContents
    assert history_header.sectionResizeMode(4) == QHeaderView.ResizeMode.ResizeToContents

    search_header = search_page.results_table.horizontalHeader()
    assert search_header.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert search_header.sectionResizeMode(1) == QHeaderView.ResizeMode.Stretch


def test_search_results_tables_show_source_channel_time_and_name_columns(qtbot) -> None:
    browse_page = BrowsePage(FakeBrowseController())
    search_page = SearchPage(FakeSearchController())
    qtbot.addWidget(browse_page)
    qtbot.addWidget(search_page)

    expected_headers = ["来源", "名称"]

    assert [browse_page.results_table.horizontalHeaderItem(index).text() for index in range(2)] == expected_headers
    assert [search_page.results_table.horizontalHeaderItem(index).text() for index in range(2)] == expected_headers


def test_history_page_formats_episode_progress_and_time(qtbot) -> None:
    class Controller:
        def load_page(self, page: int, size: int):
            return [
                HistoryRecord(
                    id=1,
                    key="movie-1",
                    vod_name="Movie",
                    vod_pic="pic",
                    vod_remarks="Episode 2",
                    episode=1,
                    episode_url="2.m3u8",
                    position=90000,
                    opening=0,
                    ending=0,
                    speed=1.0,
                    create_time=1713168000000,
                )
            ], 1

    page = HistoryPage(Controller())
    qtbot.addWidget(page)

    page.load_history()

    assert page.table.columnCount() == 5
    assert page.table.horizontalHeaderItem(1).text() == "集数"
    assert page.table.item(0, 1).text() == "2"
    assert page.table.item(0, 3).text() == "01:30"
    assert page.table.item(0, 4).text() != "1713168000000"
    assert ":" in page.table.item(0, 4).text()


def test_browse_page_handles_open_errors_without_missing_widget_crash(qtbot) -> None:
    class Controller(FakeBrowseController):
        def build_request_from_detail(self, vod_id: str):
            raise ApiError("detail failed")

    page = BrowsePage(Controller())
    qtbot.addWidget(page)
    page.current_items = [type("Item", (), {"type": 9, "vod_id": "movie-1"})()]
    page.current_path = "/Movies"

    page._handle_open(0, 0)

    breadcrumb_label = page.breadcrumb_layout.itemAt(0).widget()
    assert breadcrumb_label.text() == "/Movies | detail failed"


def test_search_filter_options_cover_web_drive_types(qtbot) -> None:
    browse_page = BrowsePage(FakeBrowseController())
    search_page = SearchPage(FakeSearchController())
    qtbot.addWidget(browse_page)
    qtbot.addWidget(search_page)

    expected_values = {"", "0", "1", "2", "3", "5", "6", "7", "8", "9", "10"}

    browse_values = {browse_page.filter_combo.itemData(index) for index in range(browse_page.filter_combo.count())}
    search_values = {search_page.filter_combo.itemData(index) for index in range(search_page.filter_combo.count())}

    assert browse_values == expected_values
    assert search_values == expected_values


def test_search_filter_options_show_pure_drive_names(qtbot) -> None:
    browse_page = BrowsePage(FakeBrowseController())
    search_page = SearchPage(FakeSearchController())
    qtbot.addWidget(browse_page)
    qtbot.addWidget(search_page)

    expected_labels = ["全部", "百度", "天翼", "夸克", "UC", "阿里", "115", "123", "迅雷", "移动", "PikPak"]

    browse_labels = [browse_page.filter_combo.itemText(index) for index in range(browse_page.filter_combo.count())]
    search_labels = [search_page.filter_combo.itemText(index) for index in range(search_page.filter_combo.count())]

    assert browse_labels == expected_labels
    assert search_labels == expected_labels


def test_search_page_allows_empty_keyword_and_shows_loading_until_worker_finishes(qtbot) -> None:
    controller = AsyncSearchController()
    page = SearchPage(controller)
    qtbot.addWidget(page)

    page.keyword_edit.setText("")
    page.search()

    qtbot.waitUntil(lambda: controller.calls == [""], timeout=1000)

    assert page.status_label.text() == "搜索中..."
    assert page.keyword_edit.isEnabled() is False
    assert page.search_button.isEnabled() is False
    assert page.filter_combo.isEnabled() is False
    assert page.clear_button.isEnabled() is False

    controller.finish(
        "",
        [VodItem(vod_id="1", vod_name="全集", type_name="阿里", vod_play_from="频道A", vod_time="2026-04-15")],
    )

    qtbot.waitUntil(lambda: page.status_label.text() == "1 条结果", timeout=1000)

    assert page.results_table.rowCount() == 1
    assert page.results_table.item(0, 0).text() == "阿里"
    assert page.results_table.item(0, 1).text() == "全集"
    assert page.results_table.item(0, 1).toolTip() == "全集"
    assert page.keyword_edit.isEnabled() is True
    assert page.search_button.isEnabled() is True
    assert page.filter_combo.isEnabled() is True
    assert page.clear_button.isEnabled() is True


def test_search_page_clear_results_clears_keyword(qtbot) -> None:
    page = SearchPage(FakeSearchController())
    qtbot.addWidget(page)

    page.keyword_edit.setText("霸王别姬")
    page._results = [VodItem(vod_id="1", vod_name="全集", type_name="阿里")]
    page._filtered_results = list(page._results)
    page.results_table.setRowCount(1)
    page.status_label.setText("1 条结果")

    page.clear_results()

    assert page.keyword_edit.text() == ""
    assert page.results_table.rowCount() == 0
    assert page.status_label.text() == ""


def test_browse_page_allows_empty_keyword_and_displays_loading_during_async_search(qtbot) -> None:
    controller = AsyncSearchController()
    page = BrowsePage(controller)
    qtbot.addWidget(page)
    page.show()

    page.keyword_edit.setText("")
    page.search()

    qtbot.waitUntil(lambda: controller.calls == [""], timeout=1000)

    assert page.search_panel.isHidden() is False
    assert page.status_label.isHidden() is False
    assert page.status_label.text() == "搜索中..."
    assert page.keyword_edit.isEnabled() is False
    assert page.search_button.isEnabled() is False
    assert page.filter_combo.isEnabled() is False
    assert page.clear_button.isEnabled() is False

    controller.finish(
        "",
        [VodItem(vod_id="1", vod_name="全集", type_name="阿里", vod_play_from="频道A", vod_time="2026-04-15")],
    )

    qtbot.waitUntil(lambda: page.status_label.text() == "1 条结果", timeout=1000)

    assert page.results_table.rowCount() == 1
    assert page.results_table.item(0, 0).text() == "阿里"
    assert page.results_table.item(0, 1).text() == "全集"
    assert page.results_table.item(0, 1).toolTip() == "全集"
    assert page.keyword_edit.isEnabled() is True
    assert page.search_button.isEnabled() is True
    assert page.filter_combo.isEnabled() is True
    assert page.clear_button.isEnabled() is True


def test_browse_page_clear_results_clears_keyword(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)
    page.show()

    page.keyword_edit.setText("霸王别姬")
    page._search_request_id = 1
    page._handle_search_succeeded(1, [VodItem(vod_id="1", vod_name="全集", type_name="阿里")])

    page.clear_results()

    assert page.keyword_edit.text() == ""
    assert page.results_table.rowCount() == 0
    assert page.status_label.text() == ""
    assert page.search_panel.isHidden() is True


def test_browse_page_keeps_search_panel_visible_when_filter_has_no_matches(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)
    page.show()
    page._search_request_id = 1

    page._handle_search_succeeded(
        1,
        [VodItem(vod_id="1", vod_name="全集", share_type="0", type_name="阿里", vod_play_from="频道A", vod_time="2026-04-15")],
    )

    assert page.results_table.rowCount() == 1
    assert page.search_panel.isHidden() is False
    assert page.filter_combo.isHidden() is False

    page.filter_combo.setCurrentIndex(page.filter_combo.findData("10"))

    assert page.results_table.rowCount() == 0
    assert page.search_panel.isHidden() is False
    assert page.filter_combo.isHidden() is False
    assert page.status_label.text() == "0 条结果"

    page.filter_combo.setCurrentIndex(page.filter_combo.findData(""))

    assert page.results_table.rowCount() == 1
    assert page.search_panel.isHidden() is False
    assert page.filter_combo.isHidden() is False
    assert page.status_label.text() == "1 条结果"


def test_browse_page_uses_latest_async_search_result(qtbot) -> None:
    controller = AsyncSearchController()
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page.keyword_edit.setText("旧结果")
    page.search()
    qtbot.waitUntil(lambda: controller.calls == ["旧结果"], timeout=1000)

    page.keyword_edit.setText("新结果")
    page.search()
    qtbot.waitUntil(lambda: controller.calls == ["旧结果", "新结果"], timeout=1000)

    controller.finish(
        "新结果",
        [VodItem(vod_id="2", vod_name="新的", type_name="阿里", vod_play_from="频道B", vod_time="2026-04-16")],
    )
    qtbot.waitUntil(lambda: page.status_label.text() == "1 条结果", timeout=1000)
    assert page.results_table.item(0, 1).text() == "新的"
    assert page.results_table.item(0, 1).toolTip() == "新的"

    controller.finish(
        "旧结果",
        [VodItem(vod_id="1", vod_name="旧的", type_name="阿里", vod_play_from="频道A", vod_time="2026-04-15")],
    )
    qtbot.wait(100)

    assert page.results_table.item(0, 1).text() == "新的"
    assert page.results_table.item(0, 1).toolTip() == "新的"


def test_browse_page_loads_selected_page_and_page_size(qtbot) -> None:
    controller = FakeBrowseController(total=120)
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page.page_size_combo.setCurrentText("30")
    page.load_path("/电影")
    controller.load_calls.clear()

    page.next_page()

    assert controller.load_calls[-1] == ("/电影", 2, 30)
    assert page.page_label.text() == "第 2 / 4 页"


def test_browse_page_resets_to_first_page_for_new_path(qtbot) -> None:
    controller = FakeBrowseController(total=120)
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page.load_path("/电影")
    page.next_page()
    page.load_path("/剧集")

    assert controller.load_calls[-1] == ("/剧集", 1, 50)
    assert page.current_page == 1


def test_browse_page_remembers_page_state_per_path(qtbot) -> None:
    controller = FakeBrowseController(total=120)
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page.page_size_combo.setCurrentText("30")
    page.load_path("/电影")
    page.next_page()
    page.load_path("/剧集")
    page.load_path("/电影")

    assert controller.load_calls[-1] == ("/电影", 2, 30)
    assert page.current_page == 2
    assert page.page_size == 30


def test_browse_page_disables_prev_and_next_when_unavailable(qtbot) -> None:
    controller = FakeBrowseController(total=30)
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page.page_size_combo.setCurrentText("30")
    page.load_path("/电影")

    assert page.prev_page_button.isEnabled() is False
    assert page.next_page_button.isEnabled() is False


def test_history_page_loads_selected_page_and_page_size(qtbot) -> None:
    class Controller:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def load_page(self, page: int, size: int):
            self.calls.append((page, size))
            return [], 120

    controller = Controller()
    page = HistoryPage(controller)
    qtbot.addWidget(page)

    page.page_size_combo.setCurrentText("30")
    page.load_history()
    controller.calls.clear()

    page.next_page()

    assert controller.calls[-1] == (2, 30)
    assert page.page_label.text() == "第 2 / 4 页"


def test_history_page_disables_prev_and_next_when_unavailable(qtbot) -> None:
    class Controller:
        def load_page(self, page: int, size: int):
            return [], 20

    page = HistoryPage(Controller())
    qtbot.addWidget(page)

    page.page_size_combo.setCurrentText("20")
    page.load_history()

    assert page.prev_page_button.isEnabled() is False
    assert page.next_page_button.isEnabled() is False


def test_history_page_delete_reloads_previous_page_when_last_page_becomes_empty(qtbot) -> None:
    class Controller:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []
            self.records = {
                2: [
                    HistoryRecord(
                        id=9,
                        key="movie-1",
                        vod_name="Movie",
                        vod_pic="",
                        vod_remarks="Ep",
                        episode=0,
                        episode_url="",
                        position=0,
                        opening=0,
                        ending=0,
                        speed=1.0,
                        create_time=1,
                    )
                ],
                1: [],
            }

        def load_page(self, page: int, size: int):
            self.calls.append((page, size))
            total = 51 if page == 2 else 50
            return self.records.get(page, []), total

        def delete_one(self, history_id: int) -> None:
            self.records[2] = []

    controller = Controller()
    page = HistoryPage(controller)
    qtbot.addWidget(page)
    page.current_page = 2
    page.page_size = 50

    page.load_history()
    page.table.selectRow(0)
    page.delete_selected()

    assert controller.calls[-1] == (1, 50)
    assert page.current_page == 1


def test_history_page_refresh_reuses_current_page_state(qtbot) -> None:
    class Controller:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def load_page(self, page: int, size: int):
            self.calls.append((page, size))
            return [], 120

    controller = Controller()
    page = HistoryPage(controller)
    qtbot.addWidget(page)

    page.page_size_combo.setCurrentText("30")
    page.load_history()
    page.next_page()
    controller.calls.clear()

    page.refresh_button.click()

    assert controller.calls == [(2, 30)]


def test_browse_page_refresh_reuses_current_page_state(qtbot) -> None:
    controller = FakeBrowseController(total=120)
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page.page_size_combo.setCurrentText("30")
    page.load_path("/电影")
    page.next_page()
    controller.load_calls.clear()

    page.reload()

    assert controller.load_calls == [("/电影", 2, 30)]


def test_browse_page_sorts_current_rows_by_name_from_header_click(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)

    page._populate_table(
        [
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Zulu",
                "vod_time": "2026-04-14 12:00",
                "vod_remarks": "2 GB",
                "dbid": 2,
            })(),
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Alpha",
                "vod_time": "2026-04-13 12:00",
                "vod_remarks": "10 GB",
                "dbid": 1,
            })(),
        ]
    )

    header = page.table.horizontalHeader()
    header.sectionClicked.emit(1)

    assert [page.table.item(row, 1).text() for row in range(page.table.rowCount())] == ["Alpha", "Zulu"]

    header.sectionClicked.emit(1)

    assert [page.table.item(row, 1).text() for row in range(page.table.rowCount())] == ["Zulu", "Alpha"]


def test_browse_page_sorts_size_by_numeric_value_not_text(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)

    page._populate_table(
        [
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Ten",
                "vod_time": "2026-04-14",
                "vod_remarks": "10 GB",
                "dbid": 10,
            })(),
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Two",
                "vod_time": "2026-04-14",
                "vod_remarks": "2 GB",
                "dbid": 20,
            })(),
        ]
    )

    page.table.sortItems(2)

    assert [page.table.item(row, 1).text() for row in range(page.table.rowCount())] == ["Two", "Ten"]


def test_browse_page_sorting_does_not_reload_folder_data(qtbot) -> None:
    controller = FakeBrowseController()
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    page._populate_table(
        [
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Beta",
                "vod_time": "2026-04-14",
                "vod_remarks": "1 GB",
                "dbid": 0,
            })(),
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Alpha",
                "vod_time": "2026-04-14",
                "vod_remarks": "2 GB",
                "dbid": 0,
            })(),
        ]
    )

    controller.load_calls.clear()
    page.table.horizontalHeader().sectionClicked.emit(1)

    assert controller.load_calls == []


def test_browse_page_sorts_rows_with_empty_sortable_values_without_crashing(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)

    page._populate_table(
        [
            type("Item", (), {
                "type": 1,
                "vod_tag": "folder",
                "vod_name": "Folder",
                "vod_time": "",
                "vod_remarks": "8.6",
                "dbid": 0,
            })(),
            type("Item", (), {
                "type": 2,
                "vod_tag": "file",
                "vod_name": "Movie",
                "vod_time": "2026-04-14 08:00",
                "vod_remarks": "1.4 GB",
                "dbid": 123456,
            })(),
        ]
    )

    page.table.sortItems(2)
    page.table.sortItems(4)
    page.table.sortItems(5)

    assert page.table.rowCount() == 2
    assert {page.table.item(row, 1).text() for row in range(page.table.rowCount())} == {"Folder", "Movie"}


def test_browse_page_opens_item_from_sorted_row_order(qtbot) -> None:
    class Controller(FakeBrowseController):
        def __init__(self) -> None:
            super().__init__()
            self.requests: list[str] = []

        def build_request_from_folder_item(self, item, folder_items):
            self.requests.append(item.vod_id)
            return {"vod_id": item.vod_id, "folder_items": folder_items}

    controller = Controller()
    page = BrowsePage(controller)
    qtbot.addWidget(page)

    first_item = type("Item", (), {
        "type": 2,
        "vod_id": "zulu-id",
        "vod_tag": "file",
        "vod_name": "Zulu",
        "vod_time": "2026-04-14",
        "vod_remarks": "1 GB",
        "dbid": 0,
        "path": "/Movies/Zulu.mkv",
        "vod_pic": "",
        "type_name": "",
        "vod_content": "",
        "vod_year": "",
        "vod_area": "",
        "vod_lang": "",
        "vod_director": "",
        "vod_actor": "",
        "vod_play_url": "http://example.com/zulu.m3u8",
    })()
    second_item = type("Item", (), {
        "type": 2,
        "vod_id": "alpha-id",
        "vod_tag": "file",
        "vod_name": "Alpha",
        "vod_time": "2026-04-14",
        "vod_remarks": "2 GB",
        "dbid": 0,
        "path": "/Movies/Alpha.mkv",
        "vod_pic": "",
        "type_name": "",
        "vod_content": "",
        "vod_year": "",
        "vod_area": "",
        "vod_lang": "",
        "vod_director": "",
        "vod_actor": "",
        "vod_play_url": "http://example.com/alpha.m3u8",
    })()
    page.current_items = [first_item, second_item]
    page._populate_table(page.current_items)

    page.table.horizontalHeader().sectionClicked.emit(1)
    page._handle_open(0, 0)

    assert controller.requests == ["alpha-id"]
