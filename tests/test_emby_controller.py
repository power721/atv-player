from atv_player.controllers.emby_controller import EmbyController
from atv_player.models import DoubanCategory


class FakeApiClient:
    def __init__(self) -> None:
        self.category_payload = {"class": []}
        self.items_payload = {"list": [], "total": 0}
        self.search_payload = {"list": [], "total": 0}
        self.detail_payload = {"list": []}
        self.item_calls: list[tuple[str, int]] = []
        self.search_calls: list[tuple[str, int]] = []
        self.detail_calls: list[str] = []

    def list_emby_categories(self) -> dict:
        return self.category_payload

    def list_emby_items(self, category_id: str, page: int) -> dict:
        self.item_calls.append((category_id, page))
        return self.items_payload

    def search_emby_items(self, keyword: str, page: int) -> dict:
        self.search_calls.append((keyword, page))
        return self.search_payload

    def get_emby_detail(self, vod_id: str) -> dict:
        self.detail_calls.append(vod_id)
        return self.detail_payload


def test_load_categories_maps_emby_class_payload() -> None:
    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "Series", "type_name": "剧集"},
            {"type_id": "Movie", "type_name": "电影"},
        ]
    }
    controller = EmbyController(api)

    categories = controller.load_categories()

    assert categories == [
        DoubanCategory(type_id="Series", type_name="剧集"),
        DoubanCategory(type_id="Movie", type_name="电影"),
    ]


def test_search_items_maps_emby_search_payload() -> None:
    api = FakeApiClient()
    api.search_payload = {
        "list": [
            {
                "vod_id": "1-3281",
                "vod_name": "黑袍纠察队",
                "vod_pic": "poster.jpg",
                "vod_remarks": "4K",
            }
        ],
        "total": 31,
    }
    controller = EmbyController(api)

    items, total = controller.search_items("黑袍纠察队", page=1)

    assert api.search_calls == [("黑袍纠察队", 1)]
    assert total == 31
    assert items[0].vod_id == "1-3281"
    assert items[0].vod_name == "黑袍纠察队"


def test_build_request_from_detail_uses_ids_endpoint_and_playlist_parsing() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1-3281",
                "vod_name": "Season 1",
                "vod_pic": "poster.jpg",
                "vod_play_url": "Episode 1$1-3282#Episode 2$1-3283",
            }
        ]
    }
    controller = EmbyController(api)

    request = controller.build_request("1-3281")

    assert api.detail_calls == ["1-3281"]
    assert request.vod.vod_id == "1-3281"
    assert [item.title for item in request.playlist] == ["Episode 1", "Episode 2"]
    assert [item.vod_id for item in request.playlist] == ["1-3282", "1-3283"]
