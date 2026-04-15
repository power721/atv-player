import threading

from PySide6.QtCore import Qt

from atv_player.api import ApiError, UnauthorizedError
from atv_player.models import DoubanCategory, VodItem
import atv_player.ui.douban_page as douban_page_module
from atv_player.ui.douban_page import DoubanPage


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

    def load_items(self, category_id: str, page: int):
        self.item_calls.append((category_id, page))
        return self.items_by_category[category_id]


class AsyncDoubanController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self._events = {
            ("suggestion", 1): threading.Event(),
            ("movie", 1): threading.Event(),
        }

    def load_items(self, category_id: str, page: int):
        self.item_calls.append((category_id, page))
        self._events[(category_id, page)].wait(timeout=5)
        return self.items_by_category[category_id]

    def release(self, category_id: str, page: int) -> None:
        self._events[(category_id, page)].set()


class FailingDoubanController(FakeDoubanController):
    def load_items(self, category_id: str, page: int):
        if category_id == "movie":
            raise ApiError("获取列表失败")
        return super().load_items(category_id, page)


class AsyncFailingDoubanController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self._events = {
            ("suggestion", 1): threading.Event(),
            ("movie", 1): threading.Event(),
        }

    def load_items(self, category_id: str, page: int):
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

    def load_items(self, category_id: str, page: int):
        self.item_calls.append((category_id, page))
        self._events[(category_id, page)].wait(timeout=5)
        if category_id == "suggestion":
            raise UnauthorizedError("Unauthorized")
        return self.items_by_category[category_id]

    def release(self, category_id: str, page: int) -> None:
        self._events[(category_id, page)].set()


def test_douban_page_loads_categories_and_first_page(qtbot) -> None:
    page = DoubanPage(FakeDoubanController())
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    assert page.category_list.currentItem().text() == "推荐"
    assert page.page_label.text() == "第 1 / 2 页"
    assert page.card_buttons[0].text() == "霸王别姬\n9.6"


def test_douban_page_clicking_card_emits_search_requested(qtbot) -> None:
    page = DoubanPage(FakeDoubanController())
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    with qtbot.waitSignal(page.search_requested, timeout=1000) as signal:
        page.card_buttons[0].click()

    assert signal.args == ["霸王别姬"]


def test_douban_page_category_change_resets_to_first_page(qtbot) -> None:
    controller = FakeDoubanController()
    page = DoubanPage(controller)
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    page.current_page = 3
    page.category_list.setCurrentRow(1)

    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("movie", 1))
    assert page.current_page == 1


def test_douban_page_ignores_stale_item_response(qtbot) -> None:
    controller = AsyncDoubanController()
    page = DoubanPage(controller)
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    controller.release("movie", 1)
    page.category_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("movie", 1))
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    assert page.card_buttons[0].text() == "活着\n9.3"

    controller.release("suggestion", 1)
    qtbot.wait(50)
    assert page.card_buttons[0].text() == "活着\n9.3"


def test_douban_page_ignores_stale_failed_item_response(qtbot) -> None:
    controller = AsyncFailingDoubanController()
    page = DoubanPage(controller)
    qtbot.addWidget(page)
    page.show()

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


def test_douban_page_ignores_stale_unauthorized_response(qtbot) -> None:
    controller = AsyncUnauthorizedDoubanController()
    page = DoubanPage(controller)
    qtbot.addWidget(page)
    page.show()
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


def test_douban_page_keeps_previous_cards_when_new_load_fails(qtbot) -> None:
    page = DoubanPage(FailingDoubanController())
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    assert page.card_buttons[0].text() == "霸王别姬\n9.6"

    page.category_list.setCurrentRow(1)

    qtbot.waitUntil(lambda: page.status_label.text() == "获取列表失败")
    assert page.card_buttons[0].text() == "霸王别姬\n9.6"


def test_douban_page_renders_loaded_poster_icon_on_card(qtbot, monkeypatch) -> None:
    class ImmediateThread:
        def __init__(self, target, daemon=None) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    from PySide6.QtGui import QImage

    image = QImage(20, 40, QImage.Format.Format_RGB32)
    image.fill(0x00FF00)

    monkeypatch.setattr(douban_page_module, "load_remote_poster_image", lambda *args, **kwargs: image)
    monkeypatch.setattr(douban_page_module.threading, "Thread", ImmediateThread)

    page = DoubanPage(FakeDoubanController())
    qtbot.addWidget(page)
    page.show()
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    assert page.card_buttons[0].icon().isNull() is False


def test_douban_page_cards_use_wider_size_and_pointing_cursor(qtbot) -> None:
    page = DoubanPage(FakeDoubanController())
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    button = page.card_buttons[0]

    assert button.width() == DoubanPage._CARD_WIDTH
    assert button.height() == DoubanPage._CARD_HEIGHT
    assert button.iconSize() == DoubanPage._CARD_POSTER_SIZE
    assert button.cursor().shape() == Qt.CursorShape.PointingHandCursor


def test_douban_page_reduces_columns_when_width_is_tighter(qtbot) -> None:
    controller = FakeDoubanController()
    controller.items_by_category["suggestion"] = (
        [
            VodItem(vod_id=str(index), vod_name=f"Movie {index}", vod_pic="", vod_remarks="9.0")
            for index in range(6)
        ],
        30,
    )
    page = DoubanPage(controller)
    qtbot.addWidget(page)
    page.resize(1300, 900)
    page.show()

    qtbot.waitUntil(lambda: len(page.card_buttons) == 6)
    narrow_columns = page._current_card_columns

    assert narrow_columns < 6
    assert page.cards_layout.getItemPosition(5)[:2] == (1, 1)

    page.resize(2200, 900)
    qtbot.waitUntil(lambda: page._current_card_columns > narrow_columns)

    assert page._current_card_columns == 6
    assert page.cards_layout.getItemPosition(5)[:2] == (0, 5)


def test_douban_page_centers_content_container(qtbot) -> None:
    page = DoubanPage(FakeDoubanController())
    qtbot.addWidget(page)
    page.resize(2200, 1000)
    page.show()

    qtbot.waitUntil(lambda: page.category_list.count() == 2)

    container_center = page.content_container.geometry().center().x()
    page_center = page.rect().center().x()

    assert abs(container_center - page_center) <= 5
