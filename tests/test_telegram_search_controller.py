from atv_player.controllers.telegram_search_controller import TelegramSearchController
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
        self.resolve_calls: list[str] = []

    def list_telegram_search_categories(self) -> dict:
        return self.category_payload

    def list_telegram_search_items(self, category_id: str, page: int) -> dict:
        self.item_calls.append((category_id, page))
        return self.items_payload

    def search_telegram_items(self, keyword: str, page: int) -> dict:
        self.search_calls.append((keyword, page))
        return self.search_payload

    def get_telegram_search_detail(self, vod_id: str) -> dict:
        self.detail_calls.append(vod_id)
        return self.detail_payload

    def get_detail(self, vod_id: str) -> dict:
        self.resolve_calls.append(vod_id)
        return {
            "list": [
                {
                    "vod_id": vod_id,
                    "vod_name": f"Resolved {vod_id}",
                    "vod_play_url": f"http://m/{vod_id}.m3u8",
                    "items": [
                        {"title": f"Resolved {vod_id}", "url": f"http://m/{vod_id}.m3u8", "vod_id": vod_id},
                    ],
                }
            ]
        }


def test_load_categories_inserts_recommendation_first() -> None:
    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "XiangxiuNBB", "type_name": "香秀"},
            {"type_id": "Movie", "type_name": "电影"},
        ]
    }
    controller = TelegramSearchController(api)

    categories = controller.load_categories()

    assert categories == [
        DoubanCategory(type_id="0", type_name="推荐"),
        DoubanCategory(type_id="XiangxiuNBB", type_name="香秀"),
        DoubanCategory(type_id="Movie", type_name="电影"),
    ]


def test_load_items_uses_recommendation_endpoint_without_page_param() -> None:
    api = FakeApiClient()
    controller = TelegramSearchController(api)

    controller.load_items("0", page=1)
    controller.load_items("XiangxiuNBB", page=3)

    assert api.item_calls == [("0", 1), ("XiangxiuNBB", 3)]


def test_search_items_maps_search_payload() -> None:
    api = FakeApiClient()
    api.search_payload = {
        "list": [
            {
                "vod_id": "https://pan.quark.cn/s/demo",
                "vod_name": "黑袍纠察队",
                "vod_pic": "poster.jpg",
                "vod_remarks": "4K",
            }
        ],
        "total": 31,
    }
    controller = TelegramSearchController(api)

    items, total = controller.search_items("黑袍纠察队", page=1)

    assert api.search_calls == [("黑袍纠察队", 1)]
    assert total == 31
    assert items[0].vod_id == "https://pan.quark.cn/s/demo"
    assert items[0].vod_name == "黑袍纠察队"
    assert items[0].vod_pic == "poster.jpg"
    assert items[0].vod_remarks == "4K"


def test_search_items_uses_pagecount_when_total_is_missing() -> None:
    api = FakeApiClient()
    api.search_payload = {"list": [], "pagecount": 3}
    controller = TelegramSearchController(api)

    _items, total = controller.search_items("黑袍纠察队", page=2)

    assert total == 90


def test_build_request_from_detail_uses_folder_playback_resolution_pattern() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1$91792$1",
                "vod_name": "第 5 季 - 2160p WEB-DL HDR10+ H265 DDP 5.1",
                "vod_pic": "http://192.168.50.60:4567/list.png",
                "vod_play_url": (
                    "S05E01 - 第 1 集 - 2160p WEB-DL HDR10+ H265 DDP 5.1.mkv(8.43 GB)$1@91793@0@0#"
                    "S05E02 - 第 2 集 - 2160p WEB-DL HDR10+ H265 DDP 5.1.mkv(8.27 GB)$1@91794@0@1#"
                    "S05E03 - 第 3 集 - 2160p WEB HDR H265.mkv(8.69 GB)$1@91795@0@2"
                ),
                "vod_play_from": "丫仙女",
                "vod_content": "playlist folder",
                "path": "/我的夸克分享/temp/5@f518510ef92a@/Season 5/~playlist",
            }
        ]
    }
    controller = TelegramSearchController(api)

    request = controller.build_request("https://pan.quark.cn/s/f518510ef92a")

    assert api.detail_calls == ["https://pan.quark.cn/s/f518510ef92a"]
    assert request.vod.vod_id == "1$91792$1"
    assert [item.title for item in request.playlist] == [
        "S05E01 - 第 1 集 - 2160p WEB-DL HDR10+ H265 DDP 5.1.mkv(8.43 GB)",
        "S05E02 - 第 2 集 - 2160p WEB-DL HDR10+ H265 DDP 5.1.mkv(8.27 GB)",
        "S05E03 - 第 3 集 - 2160p WEB HDR H265.mkv(8.69 GB)",
    ]
    assert [item.vod_id for item in request.playlist] == ["1@91793@0@0", "1@91794@0@1", "1@91795@0@2"]
    assert [item.url for item in request.playlist] == ["", "", ""]
    assert request.clicked_index == 0
    assert request.source_kind == "browse"
    assert request.source_mode == "detail"
    assert request.source_vod_id == "1$91792$1"

    resolved = request.detail_resolver(request.playlist[1])

    assert api.resolve_calls == ["1@91794@0@1"]
    assert resolved is not None
    assert resolved.vod_name == "Resolved 1@91794@0@1"
