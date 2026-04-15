from atv_player.controllers.douban_controller import DoubanController
from atv_player.models import DoubanCategory


class FakeApiClient:
    def __init__(self) -> None:
        self.category_payload = {"class": []}
        self.items_payload = {"list": [], "total": 0}
        self.item_calls: list[tuple[str, int, int]] = []

    def list_douban_categories(self) -> dict:
        return self.category_payload

    def list_douban_items(self, category_id: str, page: int, size: int = 35) -> dict:
        self.item_calls.append((category_id, page, size))
        return self.items_payload


def test_load_categories_maps_backend_class_payload() -> None:
    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "suggestion", "type_name": "推荐"},
            {"type_id": "movie", "type_name": "电影"},
        ]
    }
    controller = DoubanController(api)

    categories = controller.load_categories()

    assert categories == [
        DoubanCategory(type_id="suggestion", type_name="推荐"),
        DoubanCategory(type_id="movie", type_name="电影"),
    ]


def test_load_items_maps_vod_fields_and_total() -> None:
    api = FakeApiClient()
    api.items_payload = {
        "list": [
            {
                "vod_id": "d1",
                "vod_name": "霸王别姬",
                "vod_pic": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p1.jpg",
                "vod_remarks": "9.6",
                "dbid": 1291546,
            }
        ],
        "total": 70,
    }
    controller = DoubanController(api)

    items, total = controller.load_items("movie", page=2)

    assert total == 70
    assert items[0].vod_id == "d1"
    assert items[0].vod_name == "霸王别姬"
    assert items[0].vod_pic.endswith("p1.jpg")
    assert items[0].vod_remarks == "9.6"
    assert items[0].dbid == 1291546


def test_load_items_uses_fixed_desktop_page_size() -> None:
    api = FakeApiClient()
    controller = DoubanController(api)

    controller.load_items("movie", page=3)

    assert api.item_calls == [("movie", 3, 35)]
