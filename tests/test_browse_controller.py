from datetime import datetime

from atv_player.controllers.browse_controller import BrowseController, build_vod_list_path, filter_search_results
from atv_player.models import VodItem


class FakeApiClient:
    def __init__(self) -> None:
        self.resolved_links: list[str] = []
        self.list_vod_calls: list[tuple[str, int, int]] = []
        self.search_keywords: list[str] = []
        self.search_payload: list[dict] = []
        self.detail_calls: list[str] = []
        self.detail_payload = {
            "list": [
                {
                    "vod_id": "detail-1",
                    "vod_name": "Movie",
                    "vod_pic": "pic",
                    "vod_play_url": "http://m/1.m3u8",
                    "items": [
                        {"title": "Episode 1", "url": "1.m3u8"},
                        {"title": "Episode 2", "url": "2.m3u8"},
                    ],
                }
            ]
        }

    def resolve_share_link(self, link: str) -> str:
        self.resolved_links.append(link)
        return "/Movies/Resolved"

    def get_detail(self, vod_id: str) -> dict:
        self.detail_calls.append(vod_id)
        return self.detail_payload

    def list_vod(self, path_id: str, page: int, size: int) -> dict:
        self.list_vod_calls.append((path_id, page, size))
        return {"list": [], "total": 0}

    def telegram_search(self, keyword: str) -> list[dict]:
        self.search_keywords.append(keyword)
        return self.search_payload


def test_filter_search_results_by_drive_type() -> None:
    items = [
        VodItem(vod_id="1", vod_name="One", type_name="阿里", share_type="0"),
        VodItem(vod_id="2", vod_name="Two", type_name="夸克", share_type="5"),
    ]

    filtered = filter_search_results(items, "0")

    assert [item.vod_id for item in filtered] == ["1"]


def test_search_maps_share_type_id_to_pure_name() -> None:
    api = FakeApiClient()
    api.search_payload = [
        {
            "id": "s1",
            "name": "Movie",
            "time": "2026-04-15",
            "type": "0",
            "channel": "TG",
            "link": "https://t.me/share",
        }
    ]
    controller = BrowseController(api)

    results = controller.search("")

    assert api.search_keywords == [""]
    assert results[0].type_name == "阿里"
    assert results[0].share_type == "0"


def test_search_formats_timestamp_to_local_time() -> None:
    api = FakeApiClient()
    api.search_payload = [
        {
            "id": "s1",
            "name": "Movie",
            "time": "1713168000000",
            "type": "0",
            "channel": "TG",
            "link": "https://t.me/share",
        }
    ]
    controller = BrowseController(api)

    results = controller.search("")

    assert results[0].vod_time == datetime.fromtimestamp(1713168000000 / 1000).strftime("%Y-%m-%d %H:%M:%S")


def test_build_playlist_from_folder_starts_at_clicked_video() -> None:
    controller = BrowseController(FakeApiClient())
    folder_items = [
        VodItem(vod_id="f1", vod_name="folder", type=1, path="/TV/folder"),
        VodItem(vod_id="v1", vod_name="Ep1", type=2, vod_play_url="http://m/1.m3u8", path="/TV/Ep1.mkv"),
        VodItem(vod_id="v2", vod_name="Ep2", type=2, vod_play_url="http://m/2.m3u8", path="/TV/Ep2.mkv"),
    ]

    playlist, start_index = controller.build_playlist_from_folder(folder_items, clicked_vod_id="v2")

    assert [item.title for item in playlist] == ["Ep1", "Ep2"]
    assert start_index == 1


def test_build_playlist_from_folder_preserves_vod_ids_for_playable_files() -> None:
    controller = BrowseController(FakeApiClient())
    folder_items = [
        VodItem(vod_id="f1", vod_name="folder", type=1, path="/TV/folder"),
        VodItem(vod_id="v1", vod_name="Ep1", type=2, vod_play_url="", path="/TV/Ep1.mkv"),
        VodItem(vod_id="v2", vod_name="Ep2", type=2, vod_play_url="", path="/TV/Ep2.mkv"),
    ]

    playlist, start_index = controller.build_playlist_from_folder(folder_items, clicked_vod_id="v2")

    assert [(item.title, item.vod_id) for item in playlist] == [("Ep1", "v1"), ("Ep2", "v2")]
    assert start_index == 1


def test_resolve_search_result_returns_backend_folder_path() -> None:
    api = FakeApiClient()
    controller = BrowseController(api)
    item = VodItem(vod_id="s1", vod_name="Movie", vod_play_url="https://t.me/share")

    resolved_path = controller.resolve_search_result(item)

    assert resolved_path == "/Movies/Resolved"
    assert api.resolved_links == ["https://t.me/share"]


def test_build_request_from_detail_maps_playlist_items() -> None:
    controller = BrowseController(FakeApiClient())

    request = controller.build_request_from_detail("detail-1")

    assert request.vod.vod_id == "detail-1"
    assert [item.title for item in request.playlist] == ["Episode 1", "Episode 2"]
    assert request.clicked_index == 0


def test_build_request_from_detail_maps_title_metadata_fields() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "detail-1",
                "vod_name": "九寨沟",
                "type_name": "纪录片",
                "vod_year": "2006",
                "vod_area": "中国大陆",
                "vod_lang": "无对白",
                "vod_remarks": "6.2",
                "vod_director": "Masa Nishimura",
                "vod_actor": "未知",
                "vod_content": "九寨沟风景名胜区位于四川省阿坝藏族羌族自治州南坪县境内。",
                "dbid": 19971621,
                "items": [
                    {"title": "正片", "url": "http://m/1.m3u8"},
                ],
            }
        ]
    }
    controller = BrowseController(api)

    request = controller.build_request_from_detail("detail-1")

    assert request.vod.vod_name == "九寨沟"
    assert request.vod.type_name == "纪录片"
    assert request.vod.vod_year == "2006"
    assert request.vod.vod_area == "中国大陆"
    assert request.vod.vod_lang == "无对白"
    assert request.vod.vod_remarks == "6.2"
    assert request.vod.vod_director == "Masa Nishimura"
    assert request.vod.vod_actor == "未知"
    assert request.vod.vod_content == "九寨沟风景名胜区位于四川省阿坝藏族羌族自治州南坪县境内。"
    assert request.vod.dbid == 19971621


def test_build_request_from_folder_item_preserves_available_metadata() -> None:
    controller = BrowseController(FakeApiClient())
    clicked_item = VodItem(
        vod_id="v1",
        vod_name="九寨沟",
        vod_pic="poster.jpg",
        path="/纪录片/九寨沟.mp4",
        type=2,
        type_name="纪录片",
        vod_year="2006",
        vod_area="中国大陆",
        vod_lang="无对白",
        vod_remarks="6.2",
        vod_director="Masa Nishimura",
        vod_actor="未知",
        vod_content="九寨沟风景名胜区位于四川省阿坝藏族羌族自治州南坪县境内。",
        dbid=19971621,
        vod_play_url="http://m/1.m3u8",
    )

    request = controller.build_request_from_folder_item(clicked_item, [clicked_item])

    assert request.vod.type_name == "纪录片"
    assert request.vod.vod_year == "2006"
    assert request.vod.vod_area == "中国大陆"
    assert request.vod.vod_lang == "无对白"
    assert request.vod.vod_remarks == "6.2"
    assert request.vod.vod_director == "Masa Nishimura"
    assert request.vod.vod_actor == "未知"
    assert request.vod.vod_content.startswith("九寨沟风景名胜区位于")
    assert request.vod.dbid == 19971621


def test_build_request_from_folder_item_resolves_clicked_item_detail_before_playback() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1$91483$1",
                "vod_name": "Resolved Episode",
                "vod_pic": "resolved-poster.jpg",
                "vod_play_url": "http://192.168.50.60:4567/p/web/1@91483?ac=web&ids=1$91483$1",
                "type_name": "剧情",
                "vod_content": "resolved content",
                "items": [
                    {
                        "id": 91483,
                        "title": "Resolved Episode",
                        "url": "http://192.168.50.60:4567/p/web/1@91483?ac=web&ids=1$91483$1",
                        "path": "/TV/Ep1.mkv",
                        "size": 123,
                    }
                ],
            }
        ]
    }
    controller = BrowseController(api)
    clicked_item = VodItem(
        vod_id="1$91483$1",
        vod_name="Folder Episode",
        path="/TV/Ep1.mkv",
        type=2,
        vod_play_url="",
        vod_content="folder content",
    )

    request = controller.build_request_from_folder_item(clicked_item, [clicked_item])

    assert api.detail_calls == ["1$91483$1"]
    assert request.vod.vod_name == "Resolved Episode"
    assert request.vod.vod_content == "resolved content"
    assert request.playlist[0].url == "http://192.168.50.60:4567/p/web/1@91483?ac=web&ids=1$91483$1"
    assert request.playlist[0].vod_id == "1$91483$1"
    assert request.resolved_vod_by_id["1$91483$1"].vod_name == "Resolved Episode"


def test_build_request_from_folder_item_falls_back_to_clicked_item_when_detail_is_missing() -> None:
    api = FakeApiClient()
    api.detail_payload = {"list": []}
    controller = BrowseController(api)
    clicked_item = VodItem(
        vod_id="v1",
        vod_name="Folder Episode",
        path="/TV/Ep1.mkv",
        type=2,
        vod_play_url="http://m/fallback.m3u8",
        vod_content="folder content",
    )

    request = controller.build_request_from_folder_item(clicked_item, [clicked_item])

    assert request.vod.vod_id == "v1"
    assert request.vod.vod_name == "Folder Episode"
    assert request.vod.vod_content == "folder content"
    assert request.playlist[0].url == "http://m/fallback.m3u8"
    assert request.resolved_vod_by_id["v1"].vod_name == "Folder Episode"


def test_build_vod_list_path_wraps_root_without_encoding() -> None:
    assert build_vod_list_path("/") == "1$/$1"


def test_load_folder_wraps_path_without_encoding_for_file_list_api() -> None:
    api = FakeApiClient()
    controller = BrowseController(api)

    controller.load_folder("/电影/国产", page=2, size=30)

    assert api.list_vod_calls == [("1$/电影/国产$1", 2, 30)]
