from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QSplitter

from atv_player.ui.browse_page import BrowsePage
from atv_player.ui.history_page import HistoryPage
from atv_player.ui.search_page import SearchPage


class FakeBrowseController:
    def __init__(self) -> None:
        self.loaded_paths: list[str] = []

    def load_folder(self, path: str, page: int = 1, size: int = 50):
        self.loaded_paths.append(path)
        return [], 0


class FakeHistoryController:
    pass


class FakeSearchController:
    pass


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
    page.breadcrumb_buttons[2].click()

    assert controller.loaded_paths == ["/电影/国产"]


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

    search_header = search_page.results_table.horizontalHeader()
    assert search_header.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert search_header.sectionResizeMode(1) == QHeaderView.ResizeMode.Stretch
