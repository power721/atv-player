from atv_player.models import DoubanCategory, PlayItem


class FakeApiClient:
    def __init__(self) -> None:
        self.category_payload = {"class": []}
        self.items_payload = {"list": [], "total": 0}
        self.search_payload = {"list": [], "total": 0}
        self.detail_payload = {"list": []}
        self.playback_payload = {"url": ["Episode 1", "http://j/1.mp4"], "header": {"User-Agent": "Jellyfin"}}
        self.item_calls: list[tuple[str, int]] = []
        self.search_calls: list[tuple[str, int]] = []
        self.detail_calls: list[str] = []
        self.playback_source_calls: list[str] = []
        self.playback_progress_calls: list[tuple[str, int]] = []
        self.playback_stop_calls: list[str] = []

    def list_jellyfin_categories(self) -> dict:
        return self.category_payload

    def list_jellyfin_items(self, category_id: str, page: int) -> dict:
        self.item_calls.append((category_id, page))
        return self.items_payload

    def search_jellyfin_items(self, keyword: str, page: int) -> dict:
        self.search_calls.append((keyword, page))
        return self.search_payload

    def get_jellyfin_detail(self, vod_id: str) -> dict:
        self.detail_calls.append(vod_id)
        return self.detail_payload

    def get_jellyfin_playback_source(self, vod_id: str) -> dict:
        self.playback_source_calls.append(vod_id)
        return self.playback_payload

    def report_jellyfin_playback_progress(self, vod_id: str, position_ms: int) -> None:
        self.playback_progress_calls.append((vod_id, position_ms))

    def stop_jellyfin_playback(self, vod_id: str) -> None:
        self.playback_stop_calls.append(vod_id)


def test_load_categories_inserts_recommendation_first() -> None:
    from atv_player.controllers.jellyfin_controller import JellyfinController

    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "Series", "type_name": "剧集"},
            {"type_id": "Movie", "type_name": "电影"},
        ]
    }
    controller = JellyfinController(api)

    categories = controller.load_categories()

    assert categories == [
        DoubanCategory(type_id="0", type_name="推荐"),
        DoubanCategory(type_id="Series", type_name="剧集"),
        DoubanCategory(type_id="Movie", type_name="电影"),
    ]


def test_search_items_formats_year_and_rating_for_cards() -> None:
    from atv_player.controllers.jellyfin_controller import JellyfinController

    api = FakeApiClient()
    api.search_payload = {
        "list": [
            {
                "vod_id": "1-3281",
                "vod_name": "人生切割术",
                "vod_pic": "poster.jpg",
                "vod_year": "2022",
                "vod_remarks": "9.1",
            }
        ],
        "total": 31,
    }
    controller = JellyfinController(api)

    items, total = controller.search_items("人生切割术", page=1)

    assert api.search_calls == [("人生切割术", 1)]
    assert total == 31
    assert items[0].vod_remarks == "2022 - 9.1"


def test_load_folder_items_uses_t_query_and_first_page() -> None:
    from atv_player.controllers.jellyfin_controller import JellyfinController

    api = FakeApiClient()
    api.items_payload = {
        "list": [
            {
                "vod_id": "folder-1",
                "vod_name": "Season 1",
                "vod_pic": "folder.jpg",
                "vod_tag": "folder",
                "vod_year": "2022",
            },
            {
                "vod_id": "file-1",
                "vod_name": "Episode 1",
                "vod_pic": "episode.jpg",
                "vod_tag": "file",
                "vod_year": "2022",
                "vod_remarks": "8.8",
            },
        ]
    }
    controller = JellyfinController(api)

    items, total = controller.load_folder_items("folder-1")

    assert api.item_calls == [("folder-1", 1)]
    assert api.detail_calls == []
    assert total == 2
    assert [item.vod_remarks for item in items] == ["2022", "2022 - 8.8"]


def test_build_request_disables_remote_history_and_exposes_local_jellyfin_history_hooks() -> None:
    from atv_player.controllers.jellyfin_controller import JellyfinController

    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1-3281",
                "vod_name": "Season 1",
                "vod_pic": "poster.jpg",
                "vod_play_url": "Episode 1$1-3458#Episode 2$1-3459",
            }
        ]
    }
    load_calls: list[str] = []
    save_calls: list[tuple[str, dict[str, object]]] = []
    controller = JellyfinController(
        api,
        playback_history_loader=lambda vod_id: load_calls.append(vod_id) or None,
        playback_history_saver=lambda vod_id, payload: save_calls.append((vod_id, payload)),
    )

    request = controller.build_request("1-3281")
    first_item = request.playlist[0]

    assert request.use_local_history is False
    assert request.restore_history is False
    assert request.playback_loader is not None
    assert request.playback_progress_reporter is not None
    assert request.playback_stopper is not None
    assert request.playback_history_loader is not None
    assert request.playback_history_saver is not None

    request.playback_history_loader()
    request.playback_history_saver({"position": 45000})
    request.playback_loader(first_item)
    request.playback_progress_reporter(first_item, 2000, False)
    request.playback_stopper(first_item)

    assert load_calls == ["1-3281"]
    assert save_calls == [("1-3281", {"position": 45000})]
    assert first_item.url == "http://j/1.mp4"
    assert first_item.headers == {"User-Agent": "Jellyfin"}
    assert api.playback_source_calls == ["1-3458"]
    assert api.playback_progress_calls == [("1-3458", 2000)]
    assert api.playback_stop_calls == ["1-3458"]


def test_jellyfin_progress_reporter_reports_when_called_for_paused_final_update() -> None:
    from atv_player.controllers.jellyfin_controller import JellyfinController

    api = FakeApiClient()
    controller = JellyfinController(api)
    item = PlayItem(title="Episode 1", url="", vod_id="1-3458")

    controller.report_playback_progress(item, 2000, False)
    controller.report_playback_progress(item, 2000, True)

    assert api.playback_progress_calls == [("1-3458", 2000), ("1-3458", 2000)]


def test_build_request_single_video_uses_detail_vod_id_as_playlist_item_id() -> None:
    from atv_player.controllers.jellyfin_controller import JellyfinController

    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "1-290df557c04ec4544342450d74f07416",
                "vod_name": "[我的机器人女友(国日)].2008.BluRay.720p.x264.2Audio.AC3-CnSCG[中文字幕3.9G]",
                "vod_pic": "poster.jpg",
                "vod_play_url": "1-290df557c04ec4544342450d74f07416",
                "vod_year": "2008",
            }
        ]
    }
    controller = JellyfinController(api)

    request = controller.build_request("1-290df557c04ec4544342450d74f07416")

    assert len(request.playlist) == 1
    assert request.playlist[0].title == "[我的机器人女友(国日)].2008.BluRay.720p.x264.2Audio.AC3-CnSCG[中文字幕3.9G]"
    assert request.playlist[0].vod_id == "1-290df557c04ec4544342450d74f07416"


def test_playback_loader_uses_first_stream_url_and_parses_stringified_header_json() -> None:
    from atv_player.controllers.jellyfin_controller import JellyfinController

    api = FakeApiClient()
    api.playback_payload = {
        "url": [
            "源 1",
            "http://j/first.mp4",
            "源 2",
            "http://j/second.mp4",
        ],
        "header": '{"User-Agent": "Jellyfin/10"}',
    }
    controller = JellyfinController(api)
    item = PlayItem(title="Episode 1", url="", vod_id="1-3458")

    controller.load_playback_item(item)

    assert item.url == "http://j/first.mp4"
    assert item.headers == {"User-Agent": "Jellyfin/10"}
