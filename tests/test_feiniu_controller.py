from atv_player.controllers.feiniu_controller import FeiniuController
from atv_player.models import CategoryFilter, CategoryFilterOption, DoubanCategory, PlayItem


class FakeApiClient:
    def __init__(self) -> None:
        self.category_payload = {"class": []}
        self.items_payload = {"list": [], "total": 0}
        self.search_payload = {"list": [], "total": 0}
        self.detail_payload = {"list": []}
        self.playback_payload = {"url": ["Episode 1", "http://m/1.mp4"], "header": {"User-Agent": "Feiniu"}}
        self.item_calls: list[tuple[str, int, dict[str, str] | None]] = []
        self.search_calls: list[tuple[str, int]] = []
        self.detail_calls: list[str] = []
        self.playback_source_calls: list[str] = []
        self.playback_progress_calls: list[tuple[str, int]] = []
        self.playback_stop_calls: list[str] = []

    def list_feiniu_categories(self) -> dict:
        return self.category_payload

    def list_feiniu_items(self, category_id: str, page: int, filters: dict[str, str] | None = None) -> dict:
        self.item_calls.append((category_id, page, None if filters is None else dict(filters)))
        return self.items_payload

    def search_feiniu_items(self, keyword: str, page: int) -> dict:
        self.search_calls.append((keyword, page))
        return self.search_payload

    def get_feiniu_detail(self, vod_id: str) -> dict:
        self.detail_calls.append(vod_id)
        return self.detail_payload

    def get_feiniu_playback_source(self, vod_id: str) -> dict:
        self.playback_source_calls.append(vod_id)
        return self.playback_payload

    def report_feiniu_playback_progress(self, vod_id: str, position_ms: int) -> None:
        self.playback_progress_calls.append((vod_id, position_ms))

    def stop_feiniu_playback(self, vod_id: str) -> None:
        self.playback_stop_calls.append(vod_id)


def test_load_categories_inserts_recommendation_first() -> None:
    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "Series", "type_name": "剧集"},
            {"type_id": "Movie", "type_name": "电影"},
        ]
    }
    controller = FeiniuController(api)

    categories = controller.load_categories()

    assert categories == [
        DoubanCategory(type_id="0", type_name="推荐"),
        DoubanCategory(type_id="Series", type_name="剧集"),
        DoubanCategory(type_id="Movie", type_name="电影"),
    ]


def test_load_categories_maps_filter_groups() -> None:
    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "Series", "type_name": "剧集"},
        ],
        "filters": {
            "Series": [
                {
                    "key": "status",
                    "name": "剧集状态",
                    "value": [
                        {"n": "不限", "v": "0"},
                        {"n": "连载中", "v": "1"},
                    ],
                }
            ]
        },
    }
    controller = FeiniuController(api)

    categories = controller.load_categories()

    assert categories[1].filters == [
        CategoryFilter(
            key="status",
            name="剧集状态",
            options=[
                CategoryFilterOption(name="不限", value="0"),
                CategoryFilterOption(name="连载中", value="1"),
            ],
        )
    ]


def test_search_items_maps_feiniu_search_payload() -> None:
    api = FakeApiClient()
    api.search_payload = {
        "list": [
            {
                "vod_id": "1-3281",
                "vod_name": "黑袍纠察队",
                "vod_pic": "poster.jpg",
                "vod_year": "2020",
                "vod_remarks": "9.0",
            }
        ],
        "total": 31,
    }
    controller = FeiniuController(api)

    items, total = controller.search_items("黑袍纠察队", page=1)

    assert api.search_calls == [("黑袍纠察队", 1)]
    assert total == 31
    assert items[0].vod_id == "1-3281"
    assert items[0].vod_name == "黑袍纠察队"
    assert items[0].vod_remarks == "2020 - 9.0"


def test_feiniu_controller_passes_optional_filters_argument() -> None:
    api = FakeApiClient()
    controller = FeiniuController(api)

    controller.load_items("Movie", 1, filters={"status": "1"})

    assert api.item_calls[-1] == ("Movie", 1, {"status": "1"})


def test_load_folder_items_uses_t_query_and_first_page() -> None:
    api = FakeApiClient()
    api.items_payload = {
        "list": [
            {
                "vod_id": "folder-1",
                "vod_name": "Season 1",
                "vod_pic": "folder.jpg",
                "vod_tag": "folder",
                "vod_year": "2020",
            },
            {
                "vod_id": "file-1",
                "vod_name": "Episode 1",
                "vod_pic": "episode.jpg",
                "vod_tag": "file",
                "vod_year": "2021",
                "vod_remarks": "8.8",
            },
        ]
    }
    controller = FeiniuController(api)

    items, total = controller.load_folder_items("folder-1")

    assert api.item_calls == [("folder-1", 1, None)]
    assert api.detail_calls == []
    assert total == 2
    assert [(item.vod_id, item.vod_tag) for item in items] == [
        ("folder-1", "folder"),
        ("file-1", "file"),
    ]
    assert [item.vod_remarks for item in items] == ["2020", "2021 - 8.8"]


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
    controller = FeiniuController(api)

    request = controller.build_request("1-3281")

    assert api.detail_calls == ["1-3281"]
    assert request.vod.vod_id == "1-3281"
    assert [item.title for item in request.playlist] == ["Episode 1", "Episode 2"]
    assert [item.vod_id for item in request.playlist] == ["1-3282", "1-3283"]


def test_build_request_disables_remote_history_and_exposes_local_feiniu_history_hooks() -> None:
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
    controller = FeiniuController(
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

    assert request.source_kind == "feiniu"
    assert load_calls == ["1-3281"]
    assert save_calls == [("1-3281", {"position": 45000})]
    assert first_item.url == "http://m/1.mp4"
    assert first_item.headers == {"User-Agent": "Feiniu"}
    assert api.playback_source_calls == ["1-3458"]
    assert api.playback_progress_calls == [("1-3458", 2000)]
    assert api.playback_stop_calls == ["1-3458"]


def test_feiniu_progress_reporter_reports_while_paused() -> None:
    api = FakeApiClient()
    controller = FeiniuController(api)
    item = PlayItem(title="Episode 1", url="", vod_id="1-3458")

    controller.report_playback_progress(item, 2000, True)

    assert api.playback_progress_calls == [("1-3458", 2000)]


def test_build_request_single_video_uses_detail_vod_id_as_playlist_item_id() -> None:
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
    controller = FeiniuController(api)

    request = controller.build_request("1-290df557c04ec4544342450d74f07416")

    assert len(request.playlist) == 1
    assert request.playlist[0].title == "[我的机器人女友(国日)].2008.BluRay.720p.x264.2Audio.AC3-CnSCG[中文字幕3.9G]"
    assert request.playlist[0].vod_id == "1-290df557c04ec4544342450d74f07416"


def test_playback_loader_uses_first_stream_url_and_parses_stringified_header_json() -> None:
    api = FakeApiClient()
    api.playback_payload = {
        "url": [
            "源 1",
            "http://m/first.mp4",
            "源 2",
            "http://m/second.mp4",
        ],
        "header": '{"User-Agent": "Feiniu/1.0"}',
    }
    controller = FeiniuController(api)
    item = PlayItem(title="Episode 1", url="", vod_id="1-3458")

    controller.load_playback_item(item)

    assert item.url == "http://m/first.mp4"
    assert item.headers == {"User-Agent": "Feiniu/1.0"}
