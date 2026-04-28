import logging
import time

import pytest

import atv_player.plugins.controller as controller_module
from atv_player.api import ApiError
from atv_player.danmaku.models import DanmakuSearchItem
from atv_player.plugins.controller import SpiderPluginController


def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


@pytest.fixture(autouse=True)
def _disable_persistent_danmaku_cache(monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", lambda name, reg_src: "")
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", lambda name, reg_src, xml_text: None)


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


class ParseRequiredSpider(FakeSpider):
    def playerContent(self, flag, id, vipFlags):
        return {"parse": 1, "url": f"https://page.example{id}"}


class HtmlPageSpider(FakeSpider):
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "吞噬星空",
                    "vod_pic": "poster-detail",
                    "vod_play_from": "qq",
                    "vod_play_url": "吞噬星空_01$https://v.qq.com/x/cover/324olz7ilvo2j5f/i00350r6rf4.html",
                }
            ]
        }


class NumericMovieSpider(FakeSpider):
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "疯狂动物城2",
                    "vod_pic": "poster-detail",
                    "vod_play_from": "默认线",
                    "vod_play_url": "1$/play/1#2$/play/2#3$/play/3#4$/play/4",
                }
            ]
        }


class NumericSeriesSpider(FakeSpider):
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "白日提灯",
                    "vod_pic": "poster-detail",
                    "vod_play_from": "默认线",
                    "vod_play_url": "#".join(f"{index}$/play/{index}" for index in range(1, 9)),
                }
            ]
        }


class PluginLevelDanmakuSpider(FakeSpider):
    def danmaku(self):
        return True

    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "url": f"https://stream.example{id}.m3u8"}


class LegacyPayloadDanmuSpider(FakeSpider):
    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "danmu": True, "url": f"https://stream.example{id}.m3u8"}


class RemappedDetailIdSpider(FakeSpider):
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": f"resolved:{ids[0]}",
                    "vod_name": "改写详情 ID 的剧集",
                    "vod_pic": "poster-detail",
                    "vod_play_from": "备用线",
                    "vod_play_url": "第1集$/play/1#第2集$/play/2",
                }
            ]
        }


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
    assert first.media_title == "红果短剧"
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


def test_controller_uses_media_title_only_for_short_bare_numeric_playlists() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return []

    class DanmakuNumericMovieSpider(NumericMovieSpider):
        def danmaku(self):
            return True

    controller = SpiderPluginController(
        DanmakuNumericMovieSpider(),
        plugin_name="布布影视",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/movie-1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)
    _wait_until(lambda: first.danmaku_pending is False)

    assert calls == [("search", "疯狂动物城2|/play/1")]


def test_controller_keeps_implicit_numeric_suffix_for_long_bare_numeric_playlists() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return []

    class DanmakuNumericSeriesSpider(NumericSeriesSpider):
        def danmaku(self):
            return True

    controller = SpiderPluginController(
        DanmakuNumericSeriesSpider(),
        plugin_name="布布影视",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/series-1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)
    _wait_until(lambda: first.danmaku_pending is False)

    assert calls == [("search", "白日提灯 1|/play/1")]


def test_controller_does_not_print_payloads_during_build_and_playback_resolution(capsys) -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    request = controller.build_request("/detail/1")
    first = request.playlist[0]
    assert request.playback_loader is not None

    request.playback_loader(first)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_controller_build_request_keeps_html_page_urls_for_later_resolution() -> None:
    controller = SpiderPluginController(HtmlPageSpider(), plugin_name="吞噬星空", search_enabled=True)

    request = controller.build_request("/detail/qq-1")
    first = request.playlist[0]

    assert first.url == ""
    assert first.vod_id == "https://v.qq.com/x/cover/324olz7ilvo2j5f/i00350r6rf4.html"
    assert first.play_source == "qq"


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


def test_controller_resolves_parse_required_player_content_via_parser_service() -> None:
    parser_calls: list[tuple[str, str, str]] = []

    class FakeParserService:
        def resolve(self, flag: str, url: str, preferred_key: str = ""):
            parser_calls.append((flag, url, preferred_key))
            return type(
                "Result",
                (),
                {
                    "parser_key": "jx2",
                    "parser_label": "jx2",
                    "url": "https://media.example/resolved.m3u8",
                    "headers": {"Referer": "https://page.example"},
                },
            )()

    controller = SpiderPluginController(
        ParseRequiredSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        playback_parser_service=FakeParserService(),
        preferred_parse_key_loader=lambda: "jx1",
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert parser_calls == [("备用线", "https://page.example/play/1", "jx1")]
    assert first.url == "https://media.example/resolved.m3u8"
    assert first.headers == {"Referer": "https://page.example"}


def test_controller_raises_when_parse_required_without_parser_service() -> None:
    controller = SpiderPluginController(ParseRequiredSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("/detail/1")

    with pytest.raises(ValueError, match="当前插件未配置内置解析"):
        assert request.playback_loader is not None
        request.playback_loader(request.playlist[0])


def test_controller_resolves_danmaku_when_spider_enables_plugin_level_danmaku() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return [DanmakuSearchItem(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/x/cover/demo.html")]

        def resolve_danmu(self, page_url: str) -> str:
            calls.append(("resolve", page_url))
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">hi</d></i>'

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    _wait_until(lambda: first.danmaku_xml != "")

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.danmaku_xml == '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">hi</d></i>'
    assert calls == [
        ("search", "红果短剧 1集|/play/1"),
        ("resolve", "https://v.qq.com/x/cover/demo.html"),
    ]


def test_controller_tries_next_danmaku_candidate_when_first_candidate_has_no_records() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return [
                DanmakuSearchItem(provider="tencent", name="10", url="https://v.qq.com/x/cover/mzc00200xxpsogl/t4101te90vx.html"),
                DanmakuSearchItem(provider="tencent", name="10", url="https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html"),
            ]

        def resolve_danmu(self, page_url: str) -> str:
            calls.append(("resolve", page_url))
            if page_url.endswith("t4101te90vx.html"):
                raise RuntimeError("empty danmaku")
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>'

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    _wait_until(lambda: first.danmaku_xml != "")

    assert first.danmaku_xml == '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>'
    assert calls == [
        ("search", "红果短剧 1集|/play/1"),
        ("resolve", "https://v.qq.com/x/cover/mzc00200xxpsogl/t4101te90vx.html"),
        ("resolve", "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html"),
    ]


def test_controller_ignores_legacy_player_content_danmu_flag_when_plugin_level_danmaku_is_disabled() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return [DanmakuSearchItem(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/x/cover/demo.html")]

        def resolve_danmu(self, page_url: str) -> str:
            calls.append(("resolve", page_url))
            return "unexpected"

    controller = SpiderPluginController(
        LegacyPayloadDanmuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.danmaku_xml == ""
    assert calls == []


def test_controller_ignores_danmaku_resolution_failures_without_breaking_playback(caplog) -> None:
    class FailingDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            raise RuntimeError("danmu boom")

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FailingDanmakuService(),
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    with caplog.at_level(logging.WARNING):
        request.playback_loader(first)

    _wait_until(lambda: first.danmaku_pending is False)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.danmaku_xml == ""
    assert "danmaku" in caplog.text.lower()


def test_controller_uses_cached_danmaku_xml_without_network_lookup(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return []

        def resolve_danmu(self, page_url: str) -> str:
            calls.append(("resolve", page_url))
            return ""

    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">cached</d></i>'
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", lambda name, reg_src: xml_text)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", lambda name, reg_src, xml: None)

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    _wait_until(lambda: first.danmaku_xml == xml_text)

    assert first.danmaku_xml == xml_text
    assert calls == []


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
                    "vod_name": "百度资源",
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


def test_controller_resolves_danmaku_for_drive_replacement_playlist_items() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    spider = DanmakuDriveLinkSpider()
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return [DanmakuSearchItem(provider="tencent", name=name, url="https://v.qq.com/x/cover/demo.html")]

        def resolve_danmu(self, page_url: str) -> str:
            calls.append(("resolve", page_url))
            return f'<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">{len(calls)}</d></i>'

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.quark.cn/s/f518510ef92a"
        return {
            "list": [
                {
                    "vod_id": "1$94954$1",
                    "vod_name": "夸克资源",
                    "items": [
                        {"title": "25集", "url": "http://m/1.mp4", "path": "/S1/1.mp4", "size": 11},
                        {"title": "26集", "url": "http://m/2.mp4", "path": "/S1/2.mp4", "size": 12},
                    ],
                }
            ]
        }

    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/drive")
    assert request.playback_loader is not None
    result = request.playback_loader(request.playlists[0][0])

    assert result is not None
    first, second = result.replacement_playlist
    _wait_until(lambda: first.danmaku_xml != "")
    assert first.danmaku_xml != ""
    assert second.danmaku_xml == ""

    request.playback_loader(second)
    _wait_until(lambda: second.danmaku_xml != "")

    assert second.danmaku_xml != ""
    assert calls == [
        ("search", "网盘剧集 25集|https://pan.quark.cn/s/f518510ef92a"),
        ("resolve", "https://v.qq.com/x/cover/demo.html"),
        ("search", "网盘剧集 26集|http://m/2.mp4"),
        ("resolve", "https://v.qq.com/x/cover/demo.html"),
    ]


def test_controller_falls_back_to_first_episode_when_single_drive_item_has_no_episode_number() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return []

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.quark.cn/s/f518510ef92a"
        return {
            "list": [
                {
                    "vod_id": "1$94954$1",
                    "vod_name": "百度资源",
                    "items": [
                        {"title": "全集", "url": "http://m/1.mp4", "path": "/S1/1.mp4", "size": 11},
                    ],
                }
            ]
        }

    controller = SpiderPluginController(
        DanmakuDriveLinkSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/drive")
    assert request.playback_loader is not None
    result = request.playback_loader(request.playlists[0][0])

    assert result is not None
    assert calls == [("search", "网盘剧集 1集|https://pan.quark.cn/s/f518510ef92a")]


def test_controller_extracts_episode_number_from_sxxexx_style_titles() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return []

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.quark.cn/s/f518510ef92a"
        return {
            "list": [
                {
                    "vod_id": "1$94954$1",
                    "vod_name": "百度资源",
                    "items": [
                        {
                            "title": "S02E25.2025.2160P",
                            "url": "http://m/25.mp4",
                            "path": "/S2/25.mp4",
                            "size": 25,
                        },
                    ],
                }
            ]
        }

    controller = SpiderPluginController(
        DanmakuDriveLinkSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/drive")
    assert request.playback_loader is not None
    result = request.playback_loader(request.playlists[0][0])

    assert result is not None
    assert calls == [("search", "网盘剧集 25集|https://pan.quark.cn/s/f518510ef92a")]


def test_controller_extracts_episode_number_from_numeric_title_with_size_suffix() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return []

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.quark.cn/s/f518510ef92a"
        return {
            "list": [
                {
                    "vod_id": "1$94954$1",
                    "vod_name": "百度资源",
                    "items": [
                        {
                            "title": "12(1.26 GB)",
                            "url": "http://m/12.mp4",
                            "path": "/S1/12.mp4",
                            "size": 12,
                        },
                    ],
                }
            ]
        }

    controller = SpiderPluginController(
        DanmakuDriveLinkSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/drive")
    assert request.playback_loader is not None
    result = request.playback_loader(request.playlists[0][0])

    assert result is not None
    assert calls == [("search", "网盘剧集 12集|https://pan.quark.cn/s/f518510ef92a")]


def test_controller_uses_media_title_only_for_year_prefixed_movie_filename() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return []

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.quark.cn/s/f518510ef92a"
        return {
            "list": [
                {
                    "vod_id": "1$94954$1",
                    "vod_name": "百度资源",
                    "items": [
                        {
                            "title": "2025.2160p.iTunes.WEB-DL.H265.DV.HDR.DDP5.1.Atmos.mkv(18.87 GB)",
                            "url": "http://m/1.mp4",
                            "path": "/Zootopia 2/2025.2160p.iTunes.WEB-DL.H265.DV.HDR.DDP5.1.Atmos.mkv",
                            "size": 20266318222,
                        },
                        {
                            "title": "Zootopia.2.2025.1080p.AMZN.WEB-DL.English.DDP5.1.H.264.mkv(5.51 GB)",
                            "url": "http://m/2.mp4",
                            "path": "/Zootopia 2/Zootopia.2.2025.1080p.AMZN.WEB-DL.English.DDP5.1.H.264.mkv",
                            "size": 5916310000,
                        },
                    ],
                }
            ]
        }

    controller = SpiderPluginController(
        DanmakuDriveLinkSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/drive")
    assert request.playback_loader is not None
    result = request.playback_loader(request.playlists[0][0])

    assert result is not None
    request.playback_loader(result.replacement_playlist[0])
    _wait_until(lambda: result.replacement_playlist[0].danmaku_pending is False)

    assert calls == [("search", "网盘剧集|http://m/1.mp4")]


def test_controller_uses_replacement_playlist_index_when_drive_titles_have_no_episode_number() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return []

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.quark.cn/s/f518510ef92a"
        return {
            "list": [
                {
                    "vod_id": "1$94954$1",
                    "vod_name": "夸克资源",
                    "items": [
                        {"title": "正片.mp4", "url": "http://m/1.mp4", "path": "/S1/1.mp4", "size": 11},
                        {"title": "国语.mp4", "url": "http://m/2.mp4", "path": "/S1/2.mp4", "size": 12},
                        {"title": "超清.mp4", "url": "http://m/3.mp4", "path": "/S1/3.mp4", "size": 13},
                    ],
                }
            ]
        }

    controller = SpiderPluginController(
        DanmakuDriveLinkSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/drive")
    assert request.playback_loader is not None
    result = request.playback_loader(request.playlists[0][0])

    assert result is not None
    request.playback_loader(result.replacement_playlist[1])

    assert calls == [
        ("search", "网盘剧集 1集|https://pan.quark.cn/s/f518510ef92a"),
        ("search", "网盘剧集 2集|http://m/2.mp4"),
    ]


def test_controller_uses_local_history_episode_for_quark_drive_replacement_start_index() -> None:
    spider = DriveLinkSpider()
    load_calls: list[str] = []

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
        playback_history_loader=lambda vod_id: load_calls.append(vod_id) or type(
            "History",
            (),
            {
                "episode": 1,
                "episode_url": "http://m/2.mp4",
                "playlist_index": 0,
            },
        )(),
    )

    request = controller.build_request("/detail/drive")
    assert request.playback_loader is not None
    result = request.playback_loader(request.playlists[0][0])

    assert result is not None
    assert load_calls == ["/detail/drive"]
    assert result.replacement_start_index == 1


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


def test_controller_build_request_uses_requested_vod_id_for_local_history_callbacks() -> None:
    load_calls: list[str] = []
    save_calls: list[tuple[str, dict[str, object]]] = []
    controller = SpiderPluginController(
        RemappedDetailIdSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        playback_history_loader=lambda vod_id: load_calls.append(vod_id) or None,
        playback_history_saver=lambda vod_id, payload: save_calls.append((vod_id, payload)),
    )

    request = controller.build_request("/detail/original")

    assert request.source_vod_id == "/detail/original"
    assert request.playback_history_loader is not None
    assert request.playback_history_saver is not None

    request.playback_history_loader()
    request.playback_history_saver({"position": 45000})

    assert load_calls == ["/detail/original"]
    assert save_calls == [("/detail/original", {"position": 45000})]


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
