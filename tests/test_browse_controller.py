from datetime import datetime

from atv_player.controllers.browse_controller import (
    BrowseController,
    build_vod_list_path,
    filter_search_results,
    map_drive_video_to_play_item,
)
from atv_player.models import VodItem
from atv_player.share_types import infer_share_type


class FakeApiClient:
    def __init__(self) -> None:
        self.resolved_links: list[str] = []
        self.list_vod_calls: list[tuple[str, int, int]] = []
        self.search_keywords: list[str] = []
        self.search_payload: list[dict] = []
        self.alist_search_calls: list[tuple[str, int]] = []
        self.alist_search_payload: dict = {"list": []}
        self.detail_calls: list[str] = []
        self.rename_video_calls: list[tuple[str, str]] = []
        self.delete_video_calls: list[str] = []
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

    def search_alist_items(self, keyword: str, page: int = 1) -> dict:
        self.alist_search_calls.append((keyword, page))
        return self.alist_search_payload

    def rename_video(self, video_id: str, name: str) -> None:
        self.rename_video_calls.append((video_id, name))

    def delete_video(self, video_id: str) -> None:
        self.delete_video_calls.append(video_id)


def test_filter_search_results_by_drive_type() -> None:
    items = [
        VodItem(vod_id="1", vod_name="One", type_name="阿里", share_type="0"),
        VodItem(vod_id="2", vod_name="Two", type_name="夸克", share_type="5"),
    ]

    filtered = filter_search_results(items, "0")

    assert [item.vod_id for item in filtered] == ["1"]


def test_map_drive_video_preserves_backend_play_url_short_id() -> None:
    item = map_drive_video_to_play_item(
        {
            "name": "S01E126.mp4",
            "url": "http://atb/p/token/1@185535",
            "path": "/凡人修仙传/S01E126.mp4",
            "playId": "1@185535",
        },
        index=1,
    )

    assert item.play_id == "1@185535"
    assert item.vod_id == "/凡人修仙传/S01E126.mp4"


def test_infer_share_type_uses_share_link_hostname() -> None:
    assert infer_share_type("https://pan.quark.cn/s/demo") == "5"
    assert infer_share_type("https://pan.baidu.com/s/demo") == "10"
    assert infer_share_type("https://example.test/share") == ""


def test_filter_search_results_matches_canonical_name_without_share_type() -> None:
    items = [VodItem(vod_id="1", vod_name="夸克资源", type_name="夸克")]

    assert filter_search_results(items, "5") == items


def test_filter_search_results_matches_displayed_drive_remarks() -> None:
    items = [VodItem(vod_id="1", vod_name="夸克资源", vod_remarks="夸克")]

    assert filter_search_results(items, "5") == items


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


def test_search_alist_maps_cms_vod_items() -> None:
    api = FakeApiClient()
    api.alist_search_payload = {
        "list": [
            {
                "vod_id": "40$abc$1",
                "vod_name": "疑犯追踪（1.2.4）[440.16GB]",
                "vod_pic": "http://192.168.50.60:4567/115.jpg",
                "vod_tag": "file",
            }
        ]
    }
    controller = BrowseController(api)

    results = controller.search_alist("疑犯追踪")

    assert api.alist_search_calls == [("疑犯追踪", 1)]
    assert results[0].vod_id == "40$abc$1"
    assert results[0].vod_name == "疑犯追踪（1.2.4）[440.16GB]"
    assert results[0].vod_tag == "file"


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


def test_browse_request_uses_alist_sync_history_callbacks() -> None:
    loaded: list[str] = []
    saved: list[tuple[str, dict]] = []
    controller = BrowseController(
        FakeApiClient(),
        playback_history_loader=lambda _source_key, vod_id: loaded.append(vod_id),
        playback_history_saver=lambda _source_key, vod_id, payload: saved.append((vod_id, payload)),
    )

    request = controller.build_request_from_detail("detail-1")
    request.playback_history_loader()
    request.playback_history_saver({"position": 1000})

    assert request.use_local_history is False
    assert loaded == ["detail-1"]
    assert saved == [("detail-1", {"position": 1000})]


def test_browse_history_keeps_concrete_tvbox_source_key() -> None:
    loaded: list[tuple[str, str]] = []
    saved: list[tuple[str, str, dict]] = []
    controller = BrowseController(
        FakeApiClient(),
        playback_history_loader=lambda source_key, vod_id: loaded.append((source_key, vod_id)),
        playback_history_saver=lambda source_key, vod_id, payload: saved.append(
            (source_key, vod_id, payload)
        ),
    )

    request = controller.build_request_from_detail("detail-1", source_key="csp_TgWeb")
    request.playback_history_loader()
    request.playback_history_saver({"position": 1000})

    assert request.source_key == "csp_TgWeb"
    assert loaded == [("csp_TgWeb", "detail-1")]
    assert saved == [("csp_TgWeb", "detail-1", {"position": 1000})]


def test_build_request_from_detail_preserves_original_filename_separately_from_rewritten_title() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "detail-1",
                "vod_name": "极地恶灵 第三季",
                "items": [
                    {
                        "name": "S02E03.mp4",
                        "title": "03(688.11 MB)",
                        "url": "http://m/3.m3u8",
                        "path": "/show/s01-s02/极地恶灵.第二季.2019.英语中字.1080/S02E03.mp4",
                    }
                ],
            }
        ]
    }
    controller = BrowseController(api)

    request = controller.build_request_from_detail("detail-1")

    assert request.playlist[0].title == "03(688.11 MB)"
    assert request.playlist[0].original_title == "S02E03.mp4"


def test_build_request_from_detail_maps_playlist_sort_metadata() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "detail-1",
                "vod_name": "Series",
                "items": [
                    {
                        "name": "Episode 2.mkv",
                        "title": "第2集",
                        "url": "http://m/2.m3u8",
                        "size": 2048,
                        "rating": "8.5",
                        "time": "2026-07-23T10:00:00+08:00",
                    }
                ],
            }
        ]
    }

    item = BrowseController(api).build_request_from_detail("detail-1").playlist[0]

    assert item.original_title == "Episode 2.mkv"
    assert item.size == 2048
    assert item.rating == 8.5
    assert item.time == "2026-07-23T10:00:00+08:00"


def test_build_request_from_detail_tolerates_invalid_sort_metadata() -> None:
    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "detail-1",
                "vod_name": "Series",
                "items": [
                    {
                        "title": "Episode",
                        "url": "1",
                        "size": "bad",
                        "rating": "bad",
                        "time": None,
                    }
                ],
            }
        ]
    }

    item = BrowseController(api).build_request_from_detail("detail-1").playlist[0]

    assert item.size == 0
    assert item.rating == 0.0
    assert item.time == ""


def test_build_playlist_from_folder_maps_reliable_sort_metadata() -> None:
    controller = BrowseController(FakeApiClient())
    folder_items = [
        VodItem(
            vod_id="v1",
            vod_name="Episode 1.mkv",
            path="/TV/Episode 1.mkv",
            type=2,
            vod_tag="file",
            vod_remarks="1.5 GB",
            vod_time="2026-07-23 10:00:00",
        )
    ]

    playlist, _ = controller.build_playlist_from_folder(folder_items, "v1")

    assert playlist[0].original_title == "Episode 1.mkv"
    assert playlist[0].size == int(1.5 * 1024**3)
    assert playlist[0].rating == 0.0
    assert playlist[0].time == "2026-07-23 10:00:00"


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


def test_rename_file_uses_backend_video_id_from_vod_id() -> None:
    api = FakeApiClient()
    controller = BrowseController(api)

    controller.rename_file(VodItem(vod_id="1$91483$1", vod_name="旧名.mkv"), "新名.mkv")

    assert api.rename_video_calls == [("91483", "新名.mkv")]


def test_delete_file_uses_backend_video_id_from_vod_id() -> None:
    api = FakeApiClient()
    controller = BrowseController(api)

    controller.delete_file(VodItem(vod_id="1$91483$1", vod_name="旧名.mkv"))

    assert api.delete_video_calls == ["91483"]


def _drive_file_detail_payload() -> dict:
    return {
        "list": [
            {
                "vod_id": "1$210816$1",
                "vod_name": "叶卡捷琳娜大帝.2014.S01E01.1080p.WEB-DL.H.264.mkv",
                "vod_tag": "file",
                "vod_play_url": "http://192.168.50.60:4567/p/1/abc",
                "items": [
                    {
                        "title": "叶卡捷琳娜大帝.2014.S01E01.1080p.WEB-DL.H.264.mkv",
                        "url": "http://192.168.50.60:4567/p/1/abc",
                        "path": "/我的123分享/叶卡捷琳娜大帝/S01E01.mkv",
                        "subs": [
                            {
                                "name": "简体中文",
                                "lang": "chs",
                                "format": "text/x-ssa",
                                "ext": "ass",
                                "url": "https://cdn.123295.com/c-m8002?filename=sub.chs.ass",
                            }
                        ],
                    }
                ],
            }
        ]
    }


def test_build_request_from_detail_carries_drive_external_subtitles() -> None:
    api = FakeApiClient()
    api.detail_payload = _drive_file_detail_payload()
    controller = BrowseController(api)

    request = controller.build_request_from_detail("1$210816$1")

    subtitle = request.playlist[0].external_subtitles[0]
    assert subtitle.name == "简体中文 [网盘]"
    assert subtitle.lang == "chs"
    assert subtitle.url == "https://cdn.123295.com/c-m8002?filename=sub.chs.ass"
    assert subtitle.format == "ass"
    assert subtitle.source == "spider"


def test_build_request_from_folder_item_propagates_external_subtitles() -> None:
    api = FakeApiClient()
    api.detail_payload = _drive_file_detail_payload()
    controller = BrowseController(api)
    clicked = VodItem(
        vod_id="1$210816$1",
        vod_name="叶卡捷琳娜大帝.2014.S01E01.1080p.WEB-DL.H.264.mkv",
        type=2,
        vod_tag="file",
        path="/我的123分享/叶卡捷琳娜大帝/叶卡捷琳娜大帝.2014.S01E01.1080p.WEB-DL.H.264.mkv",
    )

    request = controller.build_request_from_folder_item(clicked, [clicked])

    assert request.playlist[0].url == "http://192.168.50.60:4567/p/1/abc"
    assert [subtitle.url for subtitle in request.playlist[0].external_subtitles] == [
        "https://cdn.123295.com/c-m8002?filename=sub.chs.ass"
    ]


def test_drive_external_subtitle_format_prefers_url_suffix() -> None:
    api = FakeApiClient()
    api.detail_payload = _drive_file_detail_payload()
    api.detail_payload["list"][0]["items"][0]["subs"] = [
        {"name": "English", "lang": "eng", "url": "https://cdn.example.com/sub.en.srt"}
    ]
    controller = BrowseController(api)

    request = controller.build_request_from_detail("1$210816$1")

    subtitle = request.playlist[0].external_subtitles[0]
    assert subtitle.format == "srt"
    assert subtitle.name == "English [网盘]"


def test_drive_external_subtitles_missing_or_invalid_entries_are_ignored() -> None:
    api = FakeApiClient()
    api.detail_payload = _drive_file_detail_payload()
    api.detail_payload["list"][0]["items"][0]["subs"] = ["bad-entry", {"name": "无链接"}, None]
    controller = BrowseController(api)

    request = controller.build_request_from_detail("1$210816$1")

    assert request.playlist[0].external_subtitles == []
