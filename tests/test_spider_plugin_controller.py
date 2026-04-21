import logging

import pytest

from atv_player.api import ApiError
from atv_player.plugins.controller import SpiderPluginController


class FakeSpider:
    def homeContent(self, filter):
        return {
            "class": [
                {"type_id": "hot", "type_name": "热门"},
                {"type_id": "tv", "type_name": "剧场"},
            ],
            "list": [
                {"vod_id": "/detail/home-1", "vod_name": "首页推荐", "vod_pic": "poster-home"},
            ],
        }

    def categoryContent(self, tid, pg, filter, extend):
        return {
            "list": [
                {"vod_id": f"/detail/{tid}-{pg}", "vod_name": f"{tid}-{pg}", "vod_pic": "poster-cat", "vod_remarks": "更新中"},
            ],
            "page": pg,
            "pagecount": 3,
            "total": 90,
        }

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "红果短剧",
                    "vod_pic": "poster-detail",
                    "vod_play_from": "备用线$$$极速线",
                    "vod_play_url": "第1集$/play/1#第2集$https://media.example/2.m3u8$$$第3集$/play/3",
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "url": f"https://stream.example{id}.m3u8", "header": {"Referer": "https://site.example"}}

    def searchContent(self, key, quick, pg="1"):
        return {
            "list": [{"vod_id": f"/detail/{key}", "vod_name": key, "vod_pic": "poster-search"}],
            "total": 1,
        }


class JsonHeaderSpider(FakeSpider):
    def playerContent(self, flag, id, vipFlags):
        return {
            "parse": 0,
            "url": f"https://stream.example{id}.m3u8",
            "header": '{"User-Agent":"PluginUA","Referer":"https://site.example"}',
        }


class DriveLinkSpider(FakeSpider):
    def __init__(self) -> None:
        self.player_calls: list[tuple[str, str]] = []

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "网盘剧集",
                    "vod_pic": "poster-detail",
                    "vod_play_from": "网盘线$$$直链线",
                    "vod_play_url": (
                        "第1集$https://pan.quark.cn/s/f518510ef92a$$$"
                        "第2集$https://media.example/2.m3u8"
                    ),
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        self.player_calls.append((flag, id))
        return super().playerContent(flag, id, vipFlags)


class FailingSearchSpider(FakeSpider):
    def searchContent(self, key, quick, pg="1"):
        raise RuntimeError("search boom")


def test_controller_load_categories_prepends_home_when_home_list_exists() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    categories = controller.load_categories()
    items, total = controller.load_items("home", 1)

    assert [item.type_name for item in categories] == ["推荐", "热门", "剧场"]
    assert [item.vod_name for item in items] == ["首页推荐"]
    assert total == 1


def test_controller_search_and_category_mapping() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    items, total = controller.search_items("庆余年", 1)
    category_items, category_total = controller.load_items("tv", 2)

    assert total == 1
    assert items[0].vod_name == "庆余年"
    assert category_total == 90
    assert category_items[0].vod_name == "tv-2"


def test_controller_build_request_exposes_grouped_route_playlists() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    request = controller.build_request("/detail/1")

    assert request.use_local_history is False
    assert request.playlist_index == 0
    assert len(request.playlists) == 2
    assert [item.title for item in request.playlists[0]] == ["第1集", "第2集"]
    assert [item.title for item in request.playlists[1]] == ["第3集"]
    assert request.playlist is request.playlists[0]

    first = request.playlists[0][0]
    second = request.playlists[0][1]
    third = request.playlists[1][0]

    assert first.url == ""
    assert first.play_source == "备用线"
    assert first.index == 0
    assert first.vod_id == "/play/1"
    assert second.url == "https://media.example/2.m3u8"
    assert third.play_source == "极速线"
    assert third.index == 0


def test_controller_build_request_defers_player_content_until_episode_load() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.headers == {"Referer": "https://site.example"}


def test_controller_parses_json_string_headers_from_player_content() -> None:
    controller = SpiderPluginController(JsonHeaderSpider(), plugin_name="红果短剧", search_enabled=True)

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.headers == {
        "User-Agent": "PluginUA",
        "Referer": "https://site.example",
    }


def test_controller_resolves_supported_drive_links_via_backend_detail_loader() -> None:
    spider = DriveLinkSpider()
    drive_calls: list[str] = []

    def load_drive_detail(link: str) -> dict:
        drive_calls.append(link)
        return {
            "list": [
                {
                    "vod_id": link,
                    "vod_name": "夸克资源",
                    "vod_play_url": "正片$https://media.example/quark-1.m3u8",
                }
            ]
        }

    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
    )

    request = controller.build_request("/detail/drive")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    result = request.playback_loader(first)

    assert drive_calls == ["https://pan.quark.cn/s/f518510ef92a"]
    assert spider.player_calls == []
    assert result is not None
    assert [item.title for item in result.replacement_playlist] == ["正片"]
    assert [item.url for item in result.replacement_playlist] == ["https://media.example/quark-1.m3u8"]


def test_controller_keeps_player_content_for_non_drive_plugin_ids() -> None:
    spider = FakeSpider()
    drive_calls: list[str] = []
    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=lambda link: drive_calls.append(link) or {"list": []},
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert drive_calls == []
    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.headers == {"Referer": "https://site.example"}


def test_controller_returns_replacement_playlist_for_quark_drive_route() -> None:
    spider = DriveLinkSpider()

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.quark.cn/s/f518510ef92a"
        return {
            "list": [
                {
                    "vod_id": "1$94954$1",
                    "vod_name": "夸克资源",
                    "items": [
                        {"title": "S1 - 1", "url": "http://m/1.mp4", "path": "/S1/1.mp4", "size": 11},
                        {"title": "S1 - 2", "url": "http://m/2.mp4", "path": "/S1/2.mp4", "size": 12},
                    ],
                }
            ]
        }

    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
    )

    request = controller.build_request("/detail/drive")
    assert request.playback_loader is not None
    result = request.playback_loader(request.playlists[0][0])

    assert result is not None
    assert [item.title for item in result.replacement_playlist] == ["S1 - 1", "S1 - 2"]
    assert [item.url for item in result.replacement_playlist] == ["http://m/1.mp4", "http://m/2.mp4"]
    assert [item.play_source for item in result.replacement_playlist] == ["网盘线(夸克)", "网盘线(夸克)"]
    assert result.replacement_start_index == 0


def test_controller_formats_generic_drive_route_with_detected_provider() -> None:
    spider = DriveLinkSpider()
    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=lambda link: {"list": []},
    )

    request = controller.build_request("/detail/drive")

    assert [item.play_source for item in request.playlists[0]] == ["网盘线(夸克)"]
    assert [item.play_source for item in request.playlists[1]] == ["直链线"]


def test_controller_does_not_duplicate_provider_suffix_when_route_already_names_provider() -> None:
    class BaiduDriveSpider(FakeSpider):
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "百度网盘剧集",
                        "vod_play_from": "百度线",
                        "vod_play_url": "查看$https://pan.baidu.com/s/1demo?pwd=test",
                    }
                ]
            }

    controller = SpiderPluginController(
        BaiduDriveSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=lambda link: {"list": []},
    )

    request = controller.build_request("/detail/baidu")

    assert [item.play_source for item in request.playlist] == ["百度线"]


def test_controller_preserves_formatted_drive_route_label_in_replacement_playlist() -> None:
    spider = DriveLinkSpider()

    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=lambda link: {
            "list": [
                {
                    "vod_id": "1$94954$1",
                    "vod_name": "夸克资源",
                    "items": [
                        {"title": "S1 - 1", "url": "http://m/1.mp4"},
                        {"title": "S1 - 2", "url": "http://m/2.mp4"},
                    ],
                }
            ]
        },
    )

    request = controller.build_request("/detail/drive")
    assert request.playback_loader is not None
    result = request.playback_loader(request.playlists[0][0])

    assert result is not None
    assert [item.play_source for item in result.replacement_playlist] == ["网盘线(夸克)", "网盘线(夸克)"]


def test_controller_returns_replacement_playlist_for_baidu_drive_route() -> None:
    class BaiduDriveSpider(FakeSpider):
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "百度网盘剧集",
                        "vod_play_from": "百度线",
                        "vod_play_url": "查看$https://pan.baidu.com/s/1demo?pwd=test",
                    }
                ]
            }

    controller = SpiderPluginController(
        BaiduDriveSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=lambda link: {
            "list": [
                {
                    "vod_id": "detail-1",
                    "vod_name": "百度资源",
                    "items": [
                        {"title": "第1集", "url": "http://b/1.mp4"},
                        {"title": "第2集", "url": "http://b/2.mp4"},
                    ],
                }
            ]
        },
    )

    request = controller.build_request("/detail/baidu")
    assert request.playback_loader is not None
    result = request.playback_loader(request.playlist[0])

    assert result is not None
    assert [item.title for item in result.replacement_playlist] == ["第1集", "第2集"]
    assert [item.url for item in result.replacement_playlist] == ["http://b/1.mp4", "http://b/2.mp4"]


def test_controller_build_request_attaches_local_playback_history_callbacks() -> None:
    load_calls: list[str] = []
    save_calls: list[tuple[str, dict[str, object]]] = []
    controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        playback_history_loader=lambda vod_id: load_calls.append(vod_id) or None,
        playback_history_saver=lambda vod_id, payload: save_calls.append((vod_id, payload)),
    )

    request = controller.build_request("/detail/1")

    assert request.use_local_history is False
    assert request.playback_history_loader is not None
    assert request.playback_history_saver is not None

    request.playback_history_loader()
    request.playback_history_saver({"position": 45000})

    assert load_calls == ["/detail/1"]
    assert save_calls == [("/detail/1", {"position": 45000})]


def test_controller_logs_search_failure(caplog) -> None:
    controller = SpiderPluginController(
        FailingSearchSpider(),
        plugin_name="失败插件",
        search_enabled=True,
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ApiError, match="search boom"):
            controller.search_items("庆余年", 1)

    assert "Spider plugin search failed" in caplog.text
    assert "失败插件" in caplog.text
