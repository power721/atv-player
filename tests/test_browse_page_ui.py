from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter

from atv_player.ui.browse_page import BrowsePage


class FakeBrowseController:
    pass


def test_browse_page_uses_split_view_for_search_and_file_list(qtbot) -> None:
    page = BrowsePage(FakeBrowseController())
    qtbot.addWidget(page)

    assert isinstance(page.content_splitter, QSplitter)
    assert page.content_splitter.orientation() == Qt.Orientation.Horizontal
    assert page.content_splitter.indexOf(page.search_panel) == 0
    assert page.content_splitter.indexOf(page.file_panel) == 1
    assert page.results_table.columnCount() == 2
    assert page.table.columnCount() == 3


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
