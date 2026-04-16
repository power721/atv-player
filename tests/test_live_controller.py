from atv_player.models import DoubanCategory


class FakeApiClient:
    def __init__(self) -> None:
        self.category_payload = {"class": []}
        self.items_payload = {"list": [], "total": 0}
        self.detail_payload = {"list": []}
        self.item_calls: list[tuple[str, int]] = []
        self.detail_calls: list[str] = []

    def list_live_categories(self) -> dict:
        return self.category_payload

    def list_live_items(self, category_id: str, page: int) -> dict:
        self.item_calls.append((category_id, page))
        return self.items_payload

    def get_live_detail(self, vod_id: str) -> dict:
        self.detail_calls.append(vod_id)
        return self.detail_payload


def test_load_categories_inserts_recommendation_first() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "bili", "type_name": "哔哩哔哩"},
            {"type_id": "douyu", "type_name": "斗鱼"},
        ]
    }
    controller = LiveController(api)

    categories = controller.load_categories()

    assert categories == [
        DoubanCategory(type_id="0", type_name="推荐"),
        DoubanCategory(type_id="bili", type_name="哔哩哔哩"),
        DoubanCategory(type_id="douyu", type_name="斗鱼"),
    ]


def test_load_folder_items_reuses_live_listing_api() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()
    api.items_payload = {
        "list": [
            {"vod_id": "bili-9-744", "vod_name": "分区", "vod_tag": "folder"},
            {"vod_id": "bili$1785607569", "vod_name": "直播间", "vod_tag": "file"},
        ]
    }
    controller = LiveController(api)

    items, total = controller.load_folder_items("bili-9")

    assert api.item_calls == [("bili-9", 1)]
    assert total == 2
    assert [(item.vod_id, item.vod_tag) for item in items] == [
        ("bili-9-744", "folder"),
        ("bili$1785607569", "file"),
    ]


def test_build_request_parses_title_url_playlist_from_detail_payload() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "bili$1785607569",
                "vod_name": "主播直播间",
                "vod_play_url": "线路 1$https://stream.example/live.m3u8#线路 2$https://backup.example/live.m3u8",
            }
        ]
    }
    controller = LiveController(api)

    request = controller.build_request("bili$1785607569")

    assert api.detail_calls == ["bili$1785607569"]
    assert request.vod.vod_id == "bili$1785607569"
    assert [item.title for item in request.playlist] == ["线路 1", "线路 2"]
    assert [item.url for item in request.playlist] == [
        "https://stream.example/live.m3u8",
        "https://backup.example/live.m3u8",
    ]


def test_build_request_prefers_detail_items_when_item_urls_exist() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "bili$1785607569",
                "vod_name": "主播直播间",
                "items": [
                    {"title": "高清", "url": "https://stream.example/hd.m3u8", "vod_id": "line-hd"},
                    {"title": "超清", "url": "https://stream.example/uhd.m3u8", "vod_id": "line-uhd"},
                ],
            }
        ]
    }
    controller = LiveController(api)

    request = controller.build_request("bili$1785607569")

    assert [item.title for item in request.playlist] == ["高清", "超清"]
    assert [item.url for item in request.playlist] == [
        "https://stream.example/hd.m3u8",
        "https://stream.example/uhd.m3u8",
    ]


def test_build_request_prefixes_titles_with_route_name_from_vod_play_from() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "bili$1785607569",
                "vod_name": "主播直播间",
                "vod_play_from": "线路1$$$线路2",
                "vod_play_url": (
                    "原画-flv-avc-1$https://stream.example/main.m3u8"
                    "$$$"
                    "蓝光-fmp4-hevc-1$https://backup.example/live.m3u8"
                ),
            }
        ]
    }
    controller = LiveController(api)

    request = controller.build_request("bili$1785607569")

    assert [item.title for item in request.playlist] == [
        "线路1 | 原画-flv-avc-1",
        "线路2 | 蓝光-fmp4-hevc-1",
    ]
    assert [item.url for item in request.playlist] == [
        "https://stream.example/main.m3u8",
        "https://backup.example/live.m3u8",
    ]
