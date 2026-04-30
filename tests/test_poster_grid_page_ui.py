import threading

import pytest
from PySide6.QtCore import Qt

from atv_player.api import ApiError, UnauthorizedError
from atv_player.models import CategoryFilter, CategoryFilterOption, DoubanCategory, VodItem
import atv_player.ui.poster_grid_page as poster_grid_page_module
from atv_player.ui.poster_grid_page import PosterGridPage


class FakeDoubanController:
    def __init__(self) -> None:
        self.category_calls = 0
        self.item_calls: list[tuple[str, int]] = []
        self.categories = [
            DoubanCategory(type_id="suggestion", type_name="推荐"),
            DoubanCategory(type_id="movie", type_name="电影"),
        ]
        self.items_by_category = {
            "suggestion": (
                [VodItem(vod_id="m1", vod_name="霸王别姬", vod_pic="poster-1", vod_remarks="9.6")],
                60,
            ),
            "movie": (
                [VodItem(vod_id="m2", vod_name="活着", vod_pic="poster-2", vod_remarks="9.3")],
                35,
            ),
        }

    def load_categories(self):
        self.category_calls += 1
        return self.categories

    def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
        self.item_calls.append((category_id, page))
        return self.items_by_category[category_id]


class AsyncDoubanController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self._events = {
            ("suggestion", 1): threading.Event(),
            ("movie", 1): threading.Event(),
        }

    def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
        self.item_calls.append((category_id, page))
        self._events[(category_id, page)].wait(timeout=5)
        return self.items_by_category[category_id]

    def release(self, category_id: str, page: int) -> None:
        self._events[(category_id, page)].set()


class FailingDoubanController(FakeDoubanController):
    def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
        if category_id == "movie":
            raise ApiError("获取列表失败")
        return super().load_items(category_id, page, filters)


class AsyncFailingDoubanController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self._events = {
            ("suggestion", 1): threading.Event(),
            ("movie", 1): threading.Event(),
        }

    def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
        self.item_calls.append((category_id, page))
        self._events[(category_id, page)].wait(timeout=5)
        if category_id == "suggestion":
            raise ApiError("旧请求失败")
        return self.items_by_category[category_id]

    def release(self, category_id: str, page: int) -> None:
        self._events[(category_id, page)].set()


class AsyncUnauthorizedDoubanController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self._events = {
            ("suggestion", 1): threading.Event(),
            ("movie", 1): threading.Event(),
        }

    def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
        self.item_calls.append((category_id, page))
        self._events[(category_id, page)].wait(timeout=5)
        if category_id == "suggestion":
            raise UnauthorizedError("Unauthorized")
        return self.items_by_category[category_id]

    def release(self, category_id: str, page: int) -> None:
        self._events[(category_id, page)].set()


class SearchableDoubanController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self.search_calls: list[tuple[str, int]] = []
        self.search_results = (
            [VodItem(vod_id="s1", vod_name="黑袍纠察队", vod_pic="poster-search", vod_remarks="搜索结果")],
            30,
        )

    def search_items(self, keyword: str, page: int):
        self.search_calls.append((keyword, page))
        return self.search_results


class FilterablePosterController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self.categories = [
            DoubanCategory(
                type_id="movie",
                type_name="电影",
                filters=[
                    CategoryFilter(
                        key="sc",
                        name="影视类型",
                        options=[
                            CategoryFilterOption(name="不限", value="0"),
                            CategoryFilterOption(name="动作", value="6"),
                        ],
                    )
                ],
            ),
            DoubanCategory(type_id="tv", type_name="剧集"),
        ]
        self.items_by_category = {
            "movie": (
                [VodItem(vod_id="m2", vod_name="活着", vod_pic="poster-2", vod_remarks="9.3")],
                35,
            ),
            "tv": (
                [VodItem(vod_id="t1", vod_name="漫长的季节", vod_pic="poster-3", vod_remarks="连载中")],
                12,
            ),
        }
        self.filtered_item_calls: list[tuple[str, int, dict[str, str] | None]] = []

    def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
        self.filtered_item_calls.append((category_id, page, None if filters is None else dict(filters)))
        return super().load_items(category_id, page, filters)


class EmptyValueFilterPosterController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self.categories = [
            DoubanCategory(
                type_id="movie",
                type_name="电影",
                filters=[
                    CategoryFilter(
                        key="class",
                        name="类型",
                        options=[
                            CategoryFilterOption(name="全部", value=""),
                            CategoryFilterOption(name="爱情", value="爱情"),
                        ],
                    )
                ],
            )
        ]

    def load_items(self, category_id: str, page: int, filters: dict[str, str] | None = None):
        return super().load_items(category_id, page, filters)


def show_loaded_page(qtbot, page: PosterGridPage) -> PosterGridPage:
    qtbot.addWidget(page)
    page.show()
    page.ensure_loaded()
    return page


def _checked_filter_value(page: PosterGridPage, key: str) -> str:
    for button in page.filter_buttons[key]:
        if button.isChecked():
            return str(button.property("filterValue") or "")
    return ""


def _filter_button(page: PosterGridPage, key: str, value: str):
    return next(button for button in page.filter_buttons[key] if button.property("filterValue") == value)


def test_poster_grid_page_loads_categories_and_first_page(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController()))

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    assert page.category_list.currentItem().text() == "推荐"
    assert page.page_label.text() == "第 1 / 2 页"
    assert page.card_buttons[0].text() == "霸王别姬\n9.6"


def test_poster_grid_page_clicking_card_emits_search_requested(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController()))

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    with qtbot.waitSignal(page.search_requested, timeout=1000) as signal:
        page.card_buttons[0].click()

    assert signal.args == ["霸王别姬"]


def test_poster_grid_page_clicking_card_can_emit_open_requested(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController(), click_action="open"))

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    with qtbot.waitSignal(page.open_requested, timeout=1000) as signal:
        page.card_buttons[0].click()

    assert signal.args == ["m1"]


def test_poster_grid_page_clicking_card_can_emit_item_open_requested(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController(), click_action="open"))

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    with qtbot.waitSignal(page.item_open_requested, timeout=1000) as signal:
        page.card_buttons[0].click()

    assert signal.args[0].vod_id == "m1"
    assert signal.args[0].vod_name == "霸王别姬"


def test_poster_grid_page_can_show_search_controls_when_enabled(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(SearchableDoubanController(), click_action="open", search_enabled=True))
    qtbot.waitUntil(lambda: page.category_list.count() == 2)

    assert page.keyword_edit.isHidden() is False
    assert page.search_button.isHidden() is False
    assert page.clear_button.isHidden() is False
    assert page.search_button.isEnabled() is False
    assert page.clear_button.isEnabled() is False


def test_poster_grid_page_enables_search_and_clear_only_with_keyword(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(SearchableDoubanController(), click_action="open", search_enabled=True))
    qtbot.waitUntil(lambda: page.category_list.count() == 2)

    assert page.search_button.isEnabled() is False
    assert page.clear_button.isEnabled() is False

    page.keyword_edit.setText("黑袍纠察队")
    qtbot.waitUntil(lambda: page.search_button.isEnabled() is True)
    assert page.clear_button.isEnabled() is True

    page.keyword_edit.clear()
    qtbot.waitUntil(lambda: page.search_button.isEnabled() is False)
    assert page.clear_button.isEnabled() is False


def test_poster_grid_page_places_filter_button_after_clear_button(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FilterablePosterController(), click_action="open", search_enabled=True))
    qtbot.waitUntil(lambda: page.selected_category_id == "movie")

    search_row = page._search_row
    assert search_row is not None
    indexes = {
        page.search_button: search_row.indexOf(page.search_button),
        page.clear_button: search_row.indexOf(page.clear_button),
        page.filter_toggle_button: search_row.indexOf(page.filter_toggle_button),
    }

    assert indexes[page.search_button] < indexes[page.clear_button] < indexes[page.filter_toggle_button]


def test_poster_grid_page_hides_filter_button_by_default(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController(), click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.category_list.count() == 2)

    assert page.filter_toggle_button.isHidden() is True
    assert page.filter_panel.isHidden() is True


def test_poster_grid_page_shows_filter_button_for_filtered_category_and_stays_collapsed(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FilterablePosterController(), click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    qtbot.waitUntil(lambda: page.selected_category_id == "movie")

    assert page.filter_toggle_button.isHidden() is False
    assert page.filter_panel.isHidden() is True

    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)
    buttons = page.filter_buttons["sc"]

    assert [button.text() for button in buttons] == ["默认", "不限", "动作"]
    assert buttons[0].isCheckable() is True
    assert _checked_filter_value(page, "sc") == ""


def test_poster_grid_page_renders_filter_options_as_checkable_buttons(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FilterablePosterController(), click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    buttons = page.filter_buttons["sc"]

    assert [button.text() for button in buttons] == ["默认", "不限", "动作"]
    assert buttons[0].isCheckable() is True
    assert buttons[0].isChecked() is True


def test_poster_grid_page_filter_buttons_use_light_theme_stylesheet(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FilterablePosterController(), click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    button = page.filter_buttons["sc"][0]
    stylesheet = button.styleSheet()

    assert "background-color: #ffffff;" in stylesheet
    assert "border: 1px solid #d0d0d0;" in stylesheet
    assert "color: #1a1a1a;" in stylesheet
    assert "QPushButton:hover" in stylesheet
    assert "#e8e8e8" in stylesheet
    assert "QPushButton:checked" in stylesheet
    assert "#0066cc" in stylesheet
    assert "#0080ff" in stylesheet


def test_poster_grid_page_filter_group_labels_use_bold_blue_text(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FilterablePosterController(), click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    label_item = page.filter_panel_layout.itemAt(0, page.filter_panel_layout.ItemRole.LabelRole)
    assert label_item is not None
    label = label_item.widget()

    assert label is not None
    assert label.text() == "影视类型"
    assert "color: #0066cc;" in label.styleSheet()
    assert label.font().bold() is True


def test_poster_grid_page_uses_plugin_empty_filter_button_without_extra_default(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(EmptyValueFilterPosterController(), click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    buttons = page.filter_buttons["class"]

    assert [button.text() for button in buttons] == ["全部", "爱情"]
    assert buttons[0].isChecked() is True


def test_poster_grid_page_clicking_filter_button_selects_it_and_reloads(qtbot) -> None:
    controller = FilterablePosterController()
    page = show_loaded_page(qtbot, PosterGridPage(controller, click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    qtbot.waitUntil(lambda: controller.filtered_item_calls[0] == ("movie", 1, {}))
    page.current_page = 3
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    default_button = _filter_button(page, "sc", "")
    action_button = _filter_button(page, "sc", "6")
    action_button.click()

    qtbot.waitUntil(lambda: controller.filtered_item_calls[-1] == ("movie", 1, {"sc": "6"}))
    assert default_button.isChecked() is False
    assert action_button.isChecked() is True
    assert page.current_page == 1


def test_poster_grid_page_sets_pointing_hand_cursor_for_all_clickable_buttons(qtbot) -> None:
    page = show_loaded_page(
        qtbot,
        PosterGridPage(FilterablePosterController(), click_action="open", search_enabled=True, folder_navigation_enabled=True),
    )

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    clickable_buttons = [
        page.search_button,
        page.clear_button,
        page.filter_toggle_button,
        page.prev_page_button,
        page.next_page_button,
        page.card_buttons[0],
        page.filter_buttons["sc"][0],
    ]

    assert all(button.cursor().shape() == Qt.CursorShape.PointingHandCursor for button in clickable_buttons)


def test_poster_grid_page_expands_filters_and_reloads_page_one_on_change(qtbot) -> None:
    controller = FilterablePosterController()
    page = show_loaded_page(qtbot, PosterGridPage(controller, click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    qtbot.waitUntil(lambda: controller.filtered_item_calls[0] == ("movie", 1, {}))
    page.current_page = 3
    page.filter_toggle_button.click()
    qtbot.waitUntil(lambda: page.filter_panel.isHidden() is False)

    _filter_button(page, "sc", "6").click()

    qtbot.waitUntil(lambda: controller.filtered_item_calls[-1] == ("movie", 1, {"sc": "6"}))
    assert page.current_page == 1


def test_poster_grid_page_remembers_filter_state_per_category(qtbot) -> None:
    controller = FilterablePosterController()
    controller.categories = [
        DoubanCategory(
            type_id="movie",
            type_name="电影",
            filters=[
                CategoryFilter(
                    key="sc",
                    name="影视类型",
                    options=[
                        CategoryFilterOption(name="不限", value="0"),
                        CategoryFilterOption(name="动作", value="6"),
                    ],
                )
            ],
        ),
        DoubanCategory(
            type_id="tv",
            type_name="剧集",
            filters=[
                CategoryFilter(
                    key="status",
                    name="剧集状态",
                    options=[
                        CategoryFilterOption(name="不限", value="0"),
                        CategoryFilterOption(name="连载中", value="1"),
                    ],
                )
            ],
        ),
    ]
    page = show_loaded_page(qtbot, PosterGridPage(controller, click_action="open", search_enabled=False))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()
    _filter_button(page, "sc", "6").click()
    qtbot.waitUntil(lambda: controller.filtered_item_calls[-1] == ("movie", 1, {"sc": "6"}))

    page.category_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: controller.filtered_item_calls[-1] == ("tv", 1, {}))
    page.filter_toggle_button.click()
    _filter_button(page, "status", "1").click()
    qtbot.waitUntil(lambda: controller.filtered_item_calls[-1] == ("tv", 1, {"status": "1"}))

    page.category_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.filter_toggle_button.click()

    assert _checked_filter_value(page, "sc") == "6"


def test_poster_grid_page_hides_category_filters_during_search_and_restores_them_after_clear(qtbot) -> None:
    class SearchableFilterController(FilterablePosterController):
        def __init__(self) -> None:
            super().__init__()
            self.search_calls: list[tuple[str, int]] = []

        def search_items(self, keyword: str, page: int):
            self.search_calls.append((keyword, page))
            return ([VodItem(vod_id="search-1", vod_name="搜索结果")], 1)

    controller = SearchableFilterController()
    page = show_loaded_page(qtbot, PosterGridPage(controller, click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: page.selected_category_id == "movie")
    page.keyword_edit.setText("黑袍纠察队")
    page.search()

    qtbot.waitUntil(lambda: controller.search_calls == [("黑袍纠察队", 1)])
    assert page.filter_toggle_button.isHidden() is True
    assert page.filter_panel.isHidden() is True

    page.clear_search()

    qtbot.waitUntil(lambda: controller.filtered_item_calls[-1] == ("movie", 1, {}))
    assert page.filter_toggle_button.isHidden() is False


def test_poster_grid_page_navigation_enabled_shows_root_breadcrumbs(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController(), folder_navigation_enabled=True))

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    qtbot.waitUntil(lambda: len(page.breadcrumb_buttons) == 2)

    assert page.breadcrumb_bar.isHidden() is False
    assert [button.text() for button in page.breadcrumb_buttons] == ["首页", "推荐"]


def test_poster_grid_page_breadcrumb_buttons_use_pointing_hand_cursor(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController(), folder_navigation_enabled=True))

    qtbot.waitUntil(lambda: len(page.breadcrumb_buttons) == 2)

    assert all(button.cursor().shape() == Qt.CursorShape.PointingHandCursor for button in page.breadcrumb_buttons)


def test_poster_grid_page_clicking_breadcrumb_emits_folder_navigation_request(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController(), folder_navigation_enabled=True))

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    page.push_folder_breadcrumb("folder-1", "分区")
    qtbot.waitUntil(lambda: len(page.breadcrumb_buttons) == 3)

    with qtbot.waitSignal(page.folder_breadcrumb_requested, timeout=1000) as signal:
        page.breadcrumb_buttons[1].click()

    assert signal.args == ["suggestion", "category", 1]


def test_poster_grid_page_category_change_resets_folder_breadcrumbs(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController(), folder_navigation_enabled=True))

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    page.push_folder_breadcrumb("folder-1", "分区")
    qtbot.waitUntil(lambda: len(page.breadcrumb_buttons) == 3)

    page.category_list.setCurrentRow(1)

    qtbot.waitUntil(lambda: [button.text() for button in page.breadcrumb_buttons] == ["首页", "电影"])


def test_poster_grid_page_search_replaces_category_cards_and_clear_restores_category(qtbot) -> None:
    controller = SearchableDoubanController()
    page = show_loaded_page(qtbot, PosterGridPage(controller, click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    assert page.card_buttons[0].text() == "霸王别姬\n9.6"

    page.keyword_edit.setText("黑袍纠察队")
    page.search()

    qtbot.waitUntil(lambda: controller.search_calls == [("黑袍纠察队", 1)])
    qtbot.waitUntil(lambda: page.card_buttons[0].text() == "黑袍纠察队\n搜索结果")
    assert page.current_page == 1

    page.clear_search()

    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("suggestion", 1))
    qtbot.waitUntil(lambda: page.card_buttons[0].text() == "霸王别姬\n9.6")


def test_poster_grid_page_clicking_search_result_can_emit_open_requested(qtbot) -> None:
    controller = SearchableDoubanController()
    page = show_loaded_page(qtbot, PosterGridPage(controller, click_action="open", search_enabled=True))

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    page.keyword_edit.setText("黑袍纠察队")
    page.search()
    qtbot.waitUntil(lambda: page.card_buttons[0].text() == "黑袍纠察队\n搜索结果")

    with qtbot.waitSignal(page.open_requested, timeout=1000) as signal:
        page.card_buttons[0].click()

    assert signal.args == ["s1"]


def test_poster_grid_page_category_change_resets_to_first_page(qtbot) -> None:
    controller = FakeDoubanController()
    page = show_loaded_page(qtbot, PosterGridPage(controller))

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    page.current_page = 3
    page.category_list.setCurrentRow(1)

    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("movie", 1))
    assert page.current_page == 1


def test_poster_grid_page_ignores_stale_item_response(qtbot) -> None:
    controller = AsyncDoubanController()
    page = show_loaded_page(qtbot, PosterGridPage(controller))

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    controller.release("movie", 1)
    page.category_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("movie", 1))
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    assert page.card_buttons[0].text() == "活着\n9.3"

    controller.release("suggestion", 1)
    qtbot.wait(50)
    assert page.card_buttons[0].text() == "活着\n9.3"


def test_poster_grid_page_ignores_stale_failed_item_response(qtbot) -> None:
    controller = AsyncFailingDoubanController()
    page = show_loaded_page(qtbot, PosterGridPage(controller))

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    controller.release("movie", 1)
    page.category_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("movie", 1))
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    assert page.card_buttons[0].text() == "活着\n9.3"

    controller.release("suggestion", 1)
    qtbot.wait(50)

    assert page.card_buttons[0].text() == "活着\n9.3"
    assert page.status_label.text() == ""


def test_poster_grid_page_ignores_stale_unauthorized_response(qtbot) -> None:
    controller = AsyncUnauthorizedDoubanController()
    page = show_loaded_page(qtbot, PosterGridPage(controller))
    unauthorized = {"count": 0}
    page.unauthorized.connect(lambda: unauthorized.__setitem__("count", unauthorized["count"] + 1))

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    controller.release("movie", 1)
    page.category_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("movie", 1))
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    assert page.card_buttons[0].text() == "活着\n9.3"

    controller.release("suggestion", 1)
    qtbot.wait(50)

    assert unauthorized["count"] == 0
    assert page.card_buttons[0].text() == "活着\n9.3"


def test_poster_grid_page_keeps_previous_cards_when_new_load_fails(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FailingDoubanController()))

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    assert page.card_buttons[0].text() == "霸王别姬\n9.6"

    page.category_list.setCurrentRow(1)

    qtbot.waitUntil(lambda: page.status_label.text() == "获取列表失败")
    assert page.card_buttons[0].text() == "霸王别姬\n9.6"


def test_poster_grid_page_renders_loaded_poster_icon_on_card(qtbot, monkeypatch) -> None:
    class ImmediateThread:
        def __init__(self, target, daemon=None) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    from PySide6.QtGui import QImage

    image = QImage(20, 40, QImage.Format.Format_RGB32)
    image.fill(0x00FF00)

    monkeypatch.setattr(poster_grid_page_module, "load_remote_poster_image", lambda *args, **kwargs: image)
    monkeypatch.setattr(poster_grid_page_module.threading, "Thread", ImmediateThread)

    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController()))
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    assert page.card_buttons[0].icon().isNull() is False


def test_poster_grid_page_renders_local_poster_file_on_card(qtbot, monkeypatch, tmp_path) -> None:
    class ImmediateThread:
        def __init__(self, target, daemon=None) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    from PySide6.QtGui import QImage

    poster_path = tmp_path / "live.png"
    image = QImage(20, 40, QImage.Format.Format_RGB32)
    image.fill(0x00FF00)
    assert image.save(str(poster_path))

    controller = FakeDoubanController()
    controller.items_by_category["suggestion"] = (
        [VodItem(vod_id="m1", vod_name="霸王别姬", vod_pic=str(poster_path), vod_remarks="9.6")],
        60,
    )

    monkeypatch.setattr(poster_grid_page_module.threading, "Thread", ImmediateThread)

    page = show_loaded_page(qtbot, PosterGridPage(controller))
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    assert page.card_buttons[0].icon().isNull() is False


def test_poster_grid_page_cards_use_wider_size_and_pointing_cursor(qtbot) -> None:
    page = show_loaded_page(qtbot, PosterGridPage(FakeDoubanController()))

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    button = page.card_buttons[0]

    assert button.width() == PosterGridPage._CARD_WIDTH
    assert button.height() == PosterGridPage._CARD_HEIGHT
    assert button.iconSize() == PosterGridPage._CARD_POSTER_SIZE
    assert button.cursor().shape() == Qt.CursorShape.PointingHandCursor


def test_poster_grid_page_reduces_columns_when_width_is_tighter(qtbot) -> None:
    controller = FakeDoubanController()
    controller.items_by_category["suggestion"] = (
        [
            VodItem(vod_id=str(index), vod_name=f"Movie {index}", vod_pic="", vod_remarks="9.0")
            for index in range(6)
        ],
        30,
    )
    page = PosterGridPage(controller)
    qtbot.addWidget(page)
    page.resize(1300, 900)
    page.show()
    page.ensure_loaded()

    qtbot.waitUntil(lambda: len(page.card_buttons) == 6)
    narrow_columns = page._current_card_columns

    assert narrow_columns < 6
    assert page.cards_layout.getItemPosition(5)[:2] == (1, 1)

    page.resize(2200, 900)
    qtbot.waitUntil(lambda: page._current_card_columns > narrow_columns)

    assert page._current_card_columns == 6
    assert page.cards_layout.getItemPosition(5)[:2] == (0, 5)


def test_poster_grid_page_centers_content_container(qtbot) -> None:
    page = PosterGridPage(FakeDoubanController())
    qtbot.addWidget(page)
    page.resize(2200, 1000)
    page.show()
    page.ensure_loaded()

    qtbot.waitUntil(lambda: page.category_list.count() == 2)

    container_center = page.content_container.geometry().center().x()
    page_center = page.rect().center().x()

    assert abs(container_center - page_center) <= 5


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_poster_grid_page_ignores_async_item_result_after_widget_deletion(qtbot) -> None:
    controller = AsyncDoubanController()
    page = PosterGridPage(controller)
    destroyed = {"count": 0}
    page.destroyed.connect(lambda *_args: destroyed.__setitem__("count", destroyed["count"] + 1))

    page.ensure_loaded()
    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    qtbot.waitUntil(lambda: controller.item_calls == [("suggestion", 1)], timeout=1000)

    page.deleteLater()
    qtbot.waitUntil(lambda: destroyed["count"] == 1, timeout=1000)

    controller.release("suggestion", 1)
    qtbot.wait(100)

    assert destroyed["count"] == 1


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_poster_grid_page_ignores_async_poster_result_after_widget_deletion(qtbot, monkeypatch) -> None:
    release_poster = threading.Event()
    destroyed = {"count": 0}

    from PySide6.QtGui import QImage

    image = QImage(20, 40, QImage.Format.Format_RGB32)
    image.fill(0x00FF00)

    def fake_load_remote_poster_image(*args, **kwargs):
        assert release_poster.wait(timeout=5), "poster load was never released"
        return image

    monkeypatch.setattr(poster_grid_page_module, "load_remote_poster_image", fake_load_remote_poster_image)

    page = PosterGridPage(FakeDoubanController())
    page.destroyed.connect(lambda *_args: destroyed.__setitem__("count", destroyed["count"] + 1))
    page.ensure_loaded()
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    page.deleteLater()
    qtbot.waitUntil(lambda: destroyed["count"] == 1, timeout=1000)

    release_poster.set()
    qtbot.wait(100)

    assert destroyed["count"] == 1
