from atv_player.controllers.bilibili_controller import BilibiliController
from atv_player.models import (
    CategoryFilter,
    CategoryFilterOption,
    DoubanCategory,
    PlayChapter,
    PlayItem,
    PlaybackDetailAction,
    PlaybackDetailField,
    PlaybackDetailFieldAction,
    PlaybackDetailValuePart,
)


class TextResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeApiClient:
    def __init__(self) -> None:
        self.category_payload = {"class": []}
        self.items_payload = {"list": [], "total": 0}
        self.search_payload = {"list": [], "total": 0}
        self.detail_payload = {"list": []}
        self.playback_payload = {"url": ["Episode 1", "http://b/1.mp4"], "header": {"Referer": "https://www.bilibili.com/"}}
        self.detail_action_payload = {"actions": []}
        self.item_calls: list[tuple[str, int, dict[str, str] | None]] = []
        self.search_calls: list[tuple[str, int]] = []
        self.detail_calls: list[str] = []
        self.playback_source_calls: list[str] = []
        self.detail_action_calls: list[tuple[str, str]] = []

    def list_bilibili_categories(self) -> dict:
        return self.category_payload

    def list_bilibili_items(self, category_id: str, page: int, filters: dict[str, str] | None = None) -> dict:
        self.item_calls.append((category_id, page, None if filters is None else dict(filters)))
        return self.items_payload

    def search_bilibili_items(self, keyword: str, page: int) -> dict:
        self.search_calls.append((keyword, page))
        return self.search_payload

    def get_bilibili_detail(self, vod_id: str) -> dict:
        self.detail_calls.append(vod_id)
        return self.detail_payload

    def get_bilibili_playback_source(self, vod_id: str) -> dict:
        self.playback_source_calls.append(vod_id)
        return self.playback_payload

    def run_bilibili_detail_action(self, vod_id: str, action_id: str) -> dict:
        self.detail_action_calls.append((vod_id, action_id))
        return self.detail_action_payload


def test_load_categories_preserves_backend_provided_recommendation() -> None:
    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "0", "type_name": "后端推荐"},
            {"type_id": "bangumi", "type_name": "番剧"},
            {"type_id": "movie", "type_name": "电影"},
        ]
    }
    controller = BilibiliController(api)

    categories = controller.load_categories()

    assert categories == [
        DoubanCategory(type_id="0", type_name="后端推荐"),
        DoubanCategory(type_id="bangumi", type_name="番剧"),
        DoubanCategory(type_id="movie", type_name="电影"),
    ]


def test_load_categories_preserves_numeric_zero_category_id() -> None:
    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": 0, "type_name": "后端推荐"},
            {"type_id": 2, "type_name": "番剧"},
        ]
    }
    controller = BilibiliController(api)

    categories = controller.load_categories()

    assert categories == [
        DoubanCategory(type_id="0", type_name="后端推荐"),
        DoubanCategory(type_id="2", type_name="番剧"),
    ]


def test_load_categories_maps_filter_groups() -> None:
    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "0", "type_name": "推荐"},
            {"type_id": "bangumi", "type_name": "番剧"},
        ],
        "filters": {
            "bangumi": [
                {
                    "key": "season_status",
                    "name": "状态",
                    "value": [
                        {"n": "全部", "v": "0"},
                        {"n": "完结", "v": "1"},
                    ],
                }
            ]
        },
    }
    controller = BilibiliController(api)

    categories = controller.load_categories()

    assert categories[1].filters == [
        CategoryFilter(
            key="season_status",
            name="状态",
            options=[
                CategoryFilterOption(name="全部", value="0"),
                CategoryFilterOption(name="完结", value="1"),
            ],
        )
    ]


def test_search_items_maps_bilibili_search_payload() -> None:
    api = FakeApiClient()
    api.search_payload = {
        "list": [
            {
                "vod_id": "BV1xx411c7mD",
                "vod_name": "孤独摇滚",
                "vod_pic": "poster.jpg",
                "vod_year": "2022",
                "vod_remarks": "9.2",
            }
        ],
        "total": 8,
    }
    controller = BilibiliController(api)

    items, total = controller.search_items("孤独摇滚", page=1)

    assert api.search_calls == [("孤独摇滚", 1)]
    assert total == 1
    assert items[0].vod_id == "BV1xx411c7mD"
    assert items[0].vod_remarks == "2022 - 9.2"


def test_bilibili_controller_passes_optional_filters_argument() -> None:
    api = FakeApiClient()
    controller = BilibiliController(api)

    controller.load_items("bangumi", 1, filters={"season_status": "1"})

    assert api.item_calls[-1] == ("bangumi", 1, {"season_status": "1"})


def test_load_folder_items_uses_t_query_and_first_page() -> None:
    api = FakeApiClient()
    api.items_payload = {
        "list": [
            {
                "vod_id": "folder-1",
                "vod_name": "第一季",
                "vod_pic": "folder.jpg",
                "vod_tag": "folder",
                "vod_year": "2024",
            },
            {
                "vod_id": "BVchild1",
                "vod_name": "第1话",
                "vod_pic": "episode.jpg",
                "vod_tag": "file",
                "vod_year": "2024",
                "vod_remarks": "9.9",
            },
        ]
    }
    controller = BilibiliController(api)

    items, total = controller.load_folder_items("folder-1")

    assert api.item_calls == [("folder-1", 1, None)]
    assert api.detail_calls == []
    assert total == 1
    assert [(item.vod_id, item.vod_tag) for item in items] == [("folder-1", "folder"), ("BVchild1", "file")]
    assert [item.vod_remarks for item in items] == ["2024", "2024 - 9.9"]


def test_build_request_disables_remote_history_and_exposes_local_bilibili_history_hooks() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "BV1xx411c7mD",
                "vod_name": "孤独摇滚",
                "vod_pic": "poster.jpg",
                "vod_play_url": "第1话$BV1xx411c7mD#第2话$BV1yy522d8nE",
            }
        ]
    }
    load_calls: list[str] = []
    save_calls: list[tuple[str, dict[str, object]]] = []
    controller = BilibiliController(
        api,
        playback_history_loader=lambda vod_id: load_calls.append(vod_id) or None,
        playback_history_saver=lambda vod_id, payload: save_calls.append((vod_id, payload)),
    )

    request = controller.build_request("BV1xx411c7mD")
    first_item = request.playlist[0]

    assert request.use_local_history is False
    assert request.restore_history is False
    assert request.playback_loader is not None
    assert request.async_playback_loader is True
    assert request.playback_progress_reporter is None
    assert request.playback_stopper is None
    assert request.playback_history_loader is not None
    assert request.playback_history_saver is not None

    request.playback_history_loader()
    request.playback_history_saver({"position": 45000})
    request.playback_loader(first_item)

    assert load_calls == ["BV1xx411c7mD"]
    assert save_calls == [("BV1xx411c7mD", {"position": 45000})]
    assert first_item.url == "http://b/1.mp4"
    assert first_item.headers == {"Referer": "https://www.bilibili.com/"}
    assert api.playback_source_calls == ["BV1xx411c7mD"]


def test_load_playback_item_parses_java_map_style_header_string() -> None:
    api = FakeApiClient()
    api.playback_payload = {
        "url": "data:application/dash+xml;base64,PE1QRD48L01QRD4=",
        "header": (
            "{Cookie=SESSDATA=627aa33b%2C1793512479%2Cd0aa8%2A51;"
            "bili_jct=d2487e6999b44715203ec3db8b4afd0a;sid=fq0q4ahm, "
            "Referer=https://www.bilibili.com, "
            "User-Agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36}"
        ),
    }
    controller = BilibiliController(api)
    item = PlayItem(title="视频", url="", vod_id="BV1xx411c7mD")

    controller.load_playback_item(item)

    assert item.url == "data:application/dash+xml;base64,PE1QRD48L01QRD4="
    assert item.headers == {
        "Cookie": "SESSDATA=627aa33b%2C1793512479%2Cd0aa8%2A51;bili_jct=d2487e6999b44715203ec3db8b4afd0a;sid=fq0q4ahm",
        "Referer": "https://www.bilibili.com",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    }


def test_load_playback_item_loads_direct_bilibili_danmaku_xml_from_payload() -> None:
    api = FakeApiClient()
    api.playback_payload = {
        "url": "http://127.0.0.1:2323/dash/demo.mpd",
        "header": {
            "Referer": "https://www.bilibili.com/video/BV1xx411c7mD",
            "User-Agent": "Mozilla/5.0 Test",
            "Cookie": "SESSDATA=test",
        },
        "danmaku": "https://comment.bilibili.com/38086313137.xml",
    }
    seen: list[tuple[str, dict[str, str], float, bool]] = []

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool) -> TextResponse:
        seen.append((url, headers, timeout, follow_redirects))
        return TextResponse('<?xml version="1.0" encoding="UTF-8"?><i><d p="1,1,25,16777215">测试弹幕</d></i>')

    controller = BilibiliController(api, http_get=fake_get)
    item = PlayItem(title="视频", url="", vod_id="BV1xx411c7mD", media_title="孤独摇滚")

    controller.load_playback_item(item)

    assert item.url == "http://127.0.0.1:2323/dash/demo.mpd"
    assert item.danmaku_xml == '<?xml version="1.0" encoding="UTF-8"?><i><d p="1,1,25,16777215">测试弹幕</d></i>'
    assert item.selected_danmaku_provider == "bilibili"
    assert item.selected_danmaku_url == "https://comment.bilibili.com/38086313137.xml"
    assert item.selected_danmaku_title == "孤独摇滚"
    assert seen == [
        (
            "https://comment.bilibili.com/38086313137.xml",
            {
                "Referer": "https://www.bilibili.com/video/BV1xx411c7mD",
                "User-Agent": "Mozilla/5.0 Test",
                "Cookie": "SESSDATA=test",
            },
            10.0,
            True,
        )
    ]


def test_load_playback_item_maps_bilibili_subtitles_from_playback_payload() -> None:
    api = FakeApiClient()
    api.playback_payload = {
        "url": "http://127.0.0.1:2323/dash/demo.mpd",
        "header": {"Referer": "https://www.bilibili.com/video/BV1xx411c7mD"},
        "subs": [
            {"url": "", "name": "关闭", "lang": "", "format": "application/x-subrip"},
            {"url": "http://127.0.0.1:4567/subtitles?lang=zh", "name": "中文", "lang": "ai-zh", "format": "application/x-subrip"},
            {"url": "http://127.0.0.1:4567/subtitles?lang=en", "name": "English", "lang": "ai-en", "format": "application/x-subrip"},
        ],
    }
    controller = BilibiliController(api)
    item = PlayItem(title="视频", url="", vod_id="BV1xx411c7mD")

    controller.load_playback_item(item)

    assert item.url == "http://127.0.0.1:2323/dash/demo.mpd"
    assert [(sub.name, sub.lang, sub.url, sub.format) for sub in item.external_subtitles] == [
        ("中文 [B站]", "ai-zh", "http://127.0.0.1:4567/subtitles?lang=zh", "application/x-subrip"),
        ("English [B站]", "ai-en", "http://127.0.0.1:4567/subtitles?lang=en", "application/x-subrip"),
    ]


def test_load_playback_item_maps_bilibili_detail_actions() -> None:
    api = FakeApiClient()
    api.playback_payload = {
        "url": ["Episode 1", "http://b/1.mp4"],
        "header": {"Referer": "https://www.bilibili.com/"},
        "actions": [
            {"id": "favorite_collection", "label": "收藏歌单", "active": True},
            {"id": "like_track", "label": "点赞", "enabled": False},
            {"id": "", "label": "bad"},
        ],
    }
    controller = BilibiliController(api)
    item = PlayItem(title="视频", url="", vod_id="BV1xx411c7mD")

    controller.load_playback_item(item)

    assert item.detail_actions == [
        PlaybackDetailAction(id="favorite_collection", label="收藏歌单", active=True),
        PlaybackDetailAction(id="like_track", label="点赞", enabled=False),
    ]


def test_build_request_splits_bilibili_routes_by_play_source_without_cross_group_id_corruption() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "BV1ebREBmEha",
                "vod_name": "和AI玩猜历史人物游戏，又被它给耍了",
                "vod_pic": "poster.jpg",
                "vod_play_from": "BiliBili$$$相关视频$$$UP主视频",
                "vod_play_url": (
                    "视频$BV1ebREBmEha$$$"
                    "和AI玩历史猜人物游戏，差点吵起来$116225338313894-36684630727#"
                    "当朋友恋爱脑发作，就让他看这个视频$116458038368774-37773577100$$$"
                    "和AI玩猜历史人物游戏，又被它给耍了$BV1ebREBmEha#"
                    "读“野史”笑了就吃柠檬挑战（3）$BV1YgRKBGELF"
                ),
            }
        ]
    }
    controller = BilibiliController(api)

    request = controller.build_request("BV1ebREBmEha")

    assert request.playlist_index == 0
    assert len(request.playlists) == 3
    assert request.playlist is request.playlists[0]
    assert [item.play_source for item in request.playlists[0]] == ["BiliBili"]
    assert [item.play_source for item in request.playlists[1]] == ["相关视频", "相关视频"]
    assert [item.play_source for item in request.playlists[2]] == ["UP主视频", "UP主视频"]
    assert [item.vod_id for item in request.playlists[0]] == ["BV1ebREBmEha"]
    assert [item.vod_id for item in request.playlists[1]] == [
        "116225338313894-36684630727",
        "116458038368774-37773577100",
    ]
    assert [item.vod_id for item in request.playlists[2]] == ["BV1ebREBmEha", "BV1YgRKBGELF"]
    assert request.vod.detail_style == "bilibili"


def test_build_request_exposes_bilibili_detail_action_runner() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "BV1xx411c7mD",
                "vod_name": "孤独摇滚",
                "vod_play_url": "第1话$BV1xx411c7mD",
            }
        ]
    }
    api.detail_action_payload = {
        "actions": [
            {"id": "favorite_collection", "label": "已收藏歌单", "active": True},
            {"id": "favorite_track", "label": "已收藏歌曲", "active": True},
        ]
    }
    controller = BilibiliController(api)

    request = controller.build_request("BV1xx411c7mD")

    assert request.detail_action_runner is not None
    refreshed = request.detail_action_runner(request.playlist[0], "favorite_track")

    assert api.detail_action_calls == [("BV1xx411c7mD", "favorite_track")]
    assert refreshed == [
        PlaybackDetailAction(id="favorite_collection", label="已收藏歌单", active=True),
        PlaybackDetailAction(id="favorite_track", label="已收藏歌曲", active=True),
    ]


def test_build_request_maps_bilibili_stat_ext_into_vod_detail_fields() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "BV14rd3BJEDV",
                "vod_name": "你是我的哆啦A梦-AI",
                "vod_play_url": "正片$BV14rd3BJEDV",
                "ext": {
                    "coin": 11058,
                    "danmaku": 774,
                    "favorite": 23111,
                    "like": 41129,
                    "reply": 962,
                    "share": 2047,
                    "view": 2387191,
                },
            }
        ]
    }
    controller = BilibiliController(api)

    request = controller.build_request("BV14rd3BJEDV")

    assert request.vod.detail_fields == [
        PlaybackDetailField(
            label="BVID",
            value_parts=[
                PlaybackDetailValuePart(
                    label="BV14rd3BJEDV",
                    action=PlaybackDetailFieldAction(type="link", value="BV14rd3BJEDV", target="bilibili"),
                )
            ],
        ),
        PlaybackDetailField(label="投币", value="1.1万"),
        PlaybackDetailField(label="点赞", value="4.1万"),
        PlaybackDetailField(label="收藏", value="2.3万"),
        PlaybackDetailField(
            label="回复",
            value_parts=[
                PlaybackDetailValuePart(
                    label="962",
                    action=PlaybackDetailFieldAction(type="comments", value="BV14rd3BJEDV", target="bilibili"),
                )
            ],
        ),
        PlaybackDetailField(label="弹幕", value="774"),
    ]


def test_build_request_keeps_ss_reply_stat_plain_without_comments_action() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "ss142986",
                "vod_name": "番剧详情",
                "vod_play_url": "正片$ss142986",
                "ext": {"reply": 333402},
            }
        ]
    }
    controller = BilibiliController(api)

    request = controller.build_request("ss142986")

    assert request.vod.detail_fields == [
        PlaybackDetailField(
            label="Season ID",
            value_parts=[
                PlaybackDetailValuePart(
                    label="142986",
                    action=PlaybackDetailFieldAction(type="link", value="ss142986", target="bilibili"),
                )
            ],
        ),
        PlaybackDetailField(label="回复", value="33.3万"),
    ]


def test_build_request_maps_bilibili_web_ids_into_clickable_detail_fields() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "BV14rd3BJEDV",
                "vod_name": "你是我的哆啦A梦-AI",
                "vod_play_url": "正片$BV14rd3BJEDV",
                "ext": {"ids": "season$142986"},
            }
        ]
    }
    controller = BilibiliController(api)

    request = controller.build_request("BV14rd3BJEDV")

    assert request.vod.detail_fields == [
        PlaybackDetailField(
            label="BVID",
            value_parts=[
                PlaybackDetailValuePart(
                    label="BV14rd3BJEDV",
                    action=PlaybackDetailFieldAction(type="link", value="BV14rd3BJEDV", target="bilibili"),
                )
            ],
        ),
        PlaybackDetailField(
            label="Season ID",
            value_parts=[
                PlaybackDetailValuePart(
                    label="142986",
                    action=PlaybackDetailFieldAction(type="link", value="season$142986", target="bilibili"),
                )
            ],
        ),
    ]


def test_build_request_maps_ss_vod_id_into_clickable_bilibili_season_field() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "ss142986",
                "vod_name": "番剧详情",
                "vod_play_url": "正片$ss142986",
            }
        ]
    }
    controller = BilibiliController(api)

    request = controller.build_request("ss142986")

    assert request.vod.detail_fields == [
        PlaybackDetailField(
            label="Season ID",
            value_parts=[
                PlaybackDetailValuePart(
                    label="142986",
                    action=PlaybackDetailFieldAction(type="link", value="ss142986", target="bilibili"),
                )
            ],
        )
    ]


def test_resolve_playlist_item_marks_bilibili_detail_style() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "BV1ebREBmEha",
                "vod_name": "和AI玩猜历史人物游戏，又被它给耍了",
                "vod_content": "发布于2026-05-02 17:26:35",
            }
        ]
    }
    controller = BilibiliController(api)

    resolved = controller.resolve_playlist_item(PlayItem(title="视频", url="", vod_id="BV1ebREBmEha"))

    assert resolved is not None
    assert resolved.detail_style == "bilibili"


def test_load_playback_item_raises_when_vod_id_missing() -> None:
    controller = BilibiliController(FakeApiClient())

    item = PlayItem(title="第1话", url="")

    try:
        controller.load_playback_item(item)
    except ValueError as exc:
        assert str(exc) == "缺少 B站 播放 ID"
    else:
        raise AssertionError("expected ValueError")


def test_load_playback_item_parses_chapters_from_payload() -> None:
    api = FakeApiClient()
    api.playback_payload = {
        "url": "http://127.0.0.1:2323/dash/demo.mpd",
        "header": {"Referer": "https://www.bilibili.com"},
        "chapters": [
            {"from": 522, "to": 1039, "title": "第二集"},
            {"from": 0, "to": 37, "title": " 片头 "},
            {"from": "bad"},
            {"title": "无起点"},
            "junk",
            {"from": 999, "to": 1200, "title": "  "},
        ],
    }
    controller = BilibiliController(api)
    item = PlayItem(title="视频", url="", vod_id="BV195KY6YEeY")

    controller.load_playback_item(item)

    assert item.chapters == [
        PlayChapter(title="片头", start_seconds=0.0, end_seconds=37.0),
        PlayChapter(title="第二集", start_seconds=522.0, end_seconds=1039.0),
    ]


def test_load_playback_item_without_chapters_key_keeps_empty_list() -> None:
    api = FakeApiClient()
    controller = BilibiliController(api)
    item = PlayItem(title="视频", url="", vod_id="BV1xx411c7mD")

    controller.load_playback_item(item)

    assert item.chapters == []


def test_build_request_detail_action_runner_uses_current_item_vod_id() -> None:
    """相关/UP 主播放组逐条播放时,点赞/投币/收藏必须作用于当前播放条目而非打开时的视频。"""
    api = FakeApiClient()
    api.detail_payload = {
        "list": [{"vod_id": "BV1xx411c7mD", "vod_name": "孤独摇滚", "vod_play_url": "第1话$BV1xx411c7mD"}]
    }
    controller = BilibiliController(api)
    request = controller.build_request("BV1xx411c7mD")
    related = PlayItem(title="相关视频", url="http://b/2.mp4", vod_id="111-222")

    request.detail_action_runner(related, "like")

    assert api.detail_action_calls == [("111-222", "like")]


def test_map_detail_actions_parses_icon_field() -> None:
    from atv_player.controllers.bilibili_controller import _map_detail_actions

    actions = _map_detail_actions(
        [
            {"id": "like", "label": "已点赞", "active": True, "icon": "like"},
            {"id": "coin", "label": "投币", "icon": 123},
            {"id": "favorite", "label": "收藏"},
        ]
    )

    assert actions[0].icon == "like"
    assert actions[1].icon == "123"
    assert actions[2].icon == ""
