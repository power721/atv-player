from atv_player.controllers.browse_controller import BrowseController, filter_search_results
from atv_player.models import VodItem


class FakeApiClient:
    def __init__(self) -> None:
        self.resolved_links: list[str] = []
        self.detail_payload = {
            "list": [
                {
                    "vod_id": "detail-1",
                    "vod_name": "Movie",
                    "vod_pic": "pic",
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
        return self.detail_payload


def test_filter_search_results_by_drive_type() -> None:
    items = [
        VodItem(vod_id="1", vod_name="One", type_name="阿里云盘"),
        VodItem(vod_id="2", vod_name="Two", type_name="夸克网盘"),
    ]

    filtered = filter_search_results(items, "阿里")

    assert [item.vod_id for item in filtered] == ["1"]


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
