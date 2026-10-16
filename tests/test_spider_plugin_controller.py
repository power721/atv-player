import logging
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import zlib

import pytest

import atv_player.danmaku.cache as danmaku_cache_module
import atv_player.plugins.controller as controller_module
from atv_player.api import ApiError
from atv_player.controllers.player_controller import PlayerController
from atv_player.danmaku.errors import DanmakuEmptyResultError
from atv_player.danmaku.providers.iqiyi import IqiyiDanmakuProvider
from atv_player.danmaku.models import (
    DanmakuSearchItem,
    DanmakuSeriesPreference,
    DanmakuSourceGroup,
    DanmakuSourceOption,
    DanmakuSourceSearchResult,
)
from atv_player.danmaku.preferences import DanmakuSeriesPreferenceStore
from atv_player.danmaku.service import DanmakuService, build_danmaku_series_key
from atv_player.metadata.providers.plugin import CustomPluginProvider
from atv_player.models import (
    CategoryFilter,
    CategoryFilterOption,
    HistoryRecord,
    PlayItem,
    PlaybackDetailAction,
    PlaybackDetailField,
)
from atv_player.plugins.controller import SpiderPluginController, _count_danmaku_entries


def test_spider_plugin_controller_delegates_episode_offset(tmp_path: Path) -> None:
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    controller = SpiderPluginController(
        object(),
        plugin_name="demo",
        search_enabled=False,
        danmaku_preference_store=store,
    )
    item = PlayItem(
        title="第12集",
        url="",
        media_title="剑来",
        selected_danmaku_provider="tencent",
    )

    controller.save_danmaku_offset(item, -2.5, playlist=[item])

    assert controller.load_danmaku_offset(item, playlist=[item]) == -2.5
    assert item.danmaku_offset_seconds == -2.5


class ManagerActionSpider:
    def getManagerActions(self):
        return [
            {"id": "qr_login", "label": "扫码登录"},
            {
                "id": "refresh_cookie",
                "label": "刷新 Cookie",
                "enabled": False,
                "tooltip": "需要先扫码登录",
            },
            {"id": "hidden", "label": "隐藏动作", "visible": False},
            "bad-payload",
            {"id": "", "label": "缺少 id"},
        ]


def test_spider_plugin_controller_manager_actions_coerces_and_filters() -> None:
    logs: list[str] = []
    controller = SpiderPluginController(
        ManagerActionSpider(),
        plugin_name="demo",
        search_enabled=False,
        plugin_log_writer=logs.append,
    )

    actions = controller.manager_actions()

    assert [(action.id, action.label, action.enabled) for action in actions] == [
        ("qr_login", "扫码登录", True),
        ("refresh_cookie", "刷新 Cookie", False),
    ]
    assert actions[1].tooltip == "需要先扫码登录"
    assert len(logs) == 2


def test_spider_plugin_controller_manager_actions_without_spider_support() -> None:
    controller = SpiderPluginController(
        object(),
        plugin_name="demo",
        search_enabled=False,
    )

    assert controller.manager_actions() == []


class JsonResponse:
    def __init__(self, payload=None, text: str = "", status_code: int = 200, content: bytes = b"") -> None:
        self._payload = payload
        self.text = text
        self.status_code = status_code
        self.content = content

    def json(self):
        return self._payload


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
    monkeypatch.setattr(controller_module, "load_cached_danmaku_source_search_result", lambda name, reg_src: None)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_source_search_result", lambda name, reg_src, result: None)


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

    def searchContent(self, key, quick, pg=1, category=""):
        return {
            "list": [{"vod_id": f"/detail/{key}", "vod_name": key, "vod_pic": "poster-search"}],
            "total": 1,
        }


class DeferredInitSpider(FakeSpider):
    def __init__(self) -> None:
        self.init_calls = 0
        self.initialized = False

    def init(self, extend=""):
        self.init_calls += 1
        self.initialized = True
        self.extend = extend

    def homeContent(self, filter):
        if not self.initialized:
            raise AssertionError("spider init should run before homeContent")
        return super().homeContent(filter)


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
    def searchContent(self, key, quick, pg=1, category=""):
        raise RuntimeError("search boom")


class SearchCategorySpider(FakeSpider):
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, bool, int, str]] = []

    def searchContent(self, key, quick, pg=1, category=""):
        self.search_calls.append((key, quick, pg, category))
        return super().searchContent(key, quick, pg, category)


class LegacySearchSpider(FakeSpider):
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, bool, int]] = []

    def searchContent(self, key, quick, pg=1):
        self.search_calls.append((key, quick, pg))
        return super().searchContent(key, quick, pg)


class NoSearchSpider:
    def homeContent(self, filter):
        return {
            "class": [{"type_id": "tv", "type_name": "剧场"}],
            "list": [],
        }

    def categoryContent(self, tid, pg, filter, extend):
        return {
            "list": [{"vod_id": f"/detail/{tid}-{pg}", "vod_name": f"{tid}-{pg}"}],
            "total": 1,
        }

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "本地播放",
                    "vod_play_from": "默认线",
                    "vod_play_url": "第1集$https://media.example/1.m3u8",
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "url": "https://media.example/1.m3u8"}


class ParseRequiredSpider(FakeSpider):
    def playerContent(self, flag, id, vipFlags):
        return {"parse": 1, "url": f"https://page.example{id}"}


class SubtitlePayloadSpider(FakeSpider):
    def __init__(self, subt: str) -> None:
        self._subt = subt

    def playerContent(self, flag, id, vipFlags):
        return {
            "parse": 0,
            "url": f"https://stream.example{id}.m3u8",
            "header": {"Referer": "https://site.example"},
            "subt": self._subt,
        }


class LyricPayloadSpider(FakeSpider):
    def __init__(self, lyric: object, subt: str = "") -> None:
        self._lyric = lyric
        self._subt = subt

    def playerContent(self, flag, id, vipFlags):
        payload = {
            "parse": 0,
            "url": f"https://stream.example{id}.m3u8",
            "header": {"Referer": "https://site.example"},
        }
        if self._lyric is not None:
            payload["lyric"] = self._lyric
        if self._subt:
            payload["subt"] = self._subt
        return payload


class QualityPayloadSpider(FakeSpider):
    def __init__(self, url: str, qualities: object) -> None:
        self._url = url
        self._qualities = qualities

    def playerContent(self, flag, id, vipFlags):
        return {
            "parse": 0,
            "url": self._url,
            "header": {"Referer": "https://site.example"},
            "qualities": self._qualities,
        }


class YoutubeDetailSpider(FakeSpider):
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "YouTube 剧集",
                    "vod_play_from": "YouTube线",
                    "vod_play_url": "第1集$https://www.youtube.com/watch?v=test123",
                }
            ]
        }


class YoutubePlayerSpider(FakeSpider):
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "YouTube 剧集",
                    "vod_play_from": "YouTube线",
                    "vod_play_url": "第1集$/play/yt",
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        return {
            "parse": 0,
            "url": "https://www.youtube.com/watch?v=test123",
            "header": {"Referer": "https://site.example"},
        }


class CoverPayloadSpider(FakeSpider):
    def playerContent(self, flag, id, vipFlags):
        return {
            "parse": 0,
            "url": f"https://stream.example{id}.m3u8",
            "header": {"Referer": "https://site.example"},
            "cover": "https://img.example/resolved-cover.jpg",
        }


class BlankCoverPayloadSpider(FakeSpider):
    def playerContent(self, flag, id, vipFlags):
        return {
            "parse": 0,
            "url": f"https://stream.example{id}.m3u8",
            "header": {"Referer": "https://site.example"},
            "cover": "   ",
        }


class ActionPayloadSpider(FakeSpider):
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "红果短剧",
                    "vod_play_from": "默认线",
                    "vod_play_url": "第1集$/play/1",
                    "actions": [
                        {"id": "favorite_album", "label": "收藏专辑", "active": True, "tooltip": "已收藏"},
                        {"id": "hidden", "label": "隐藏", "visible": False},
                    ],
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        return {
            "parse": 0,
            "url": f"https://stream.example{id}.m3u8",
            "actions": [
                {"id": "favorite_track", "label": "收藏歌曲", "enabled": False},
                {"id": "", "label": "bad"},
            ],
        }

    def runPlayerAction(self, action_id, context):
        assert context["action_id"] == action_id
        assert context["vod"].vod_name == "红果短剧"
        assert context["play_item"].title == "第1集"
        return {
            "actions": [
                {"id": "favorite_album", "label": "已收藏专辑", "active": True},
                {"id": "favorite_track", "label": "已收藏歌曲", "active": True},
            ]
        }


class PartialActionRefreshSpider(ActionPayloadSpider):
    def runPlayerAction(self, action_id, context):
        assert context["action_id"] == action_id
        return {
            "actions": [
                {"id": "favorite_album", "label": "已收藏专辑", "active": True},
            ]
        }


class DetailFieldPayloadSpider(FakeSpider):
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "红果短剧",
                    "vod_play_from": "默认线$$$备用线",
                    "vod_play_url": "第1集$/play/1#第2集$/play/2",
                    "ext": [
                        {"label": "播放", "value": "12万"},
                        {"label": "更新", "value": "2026-05-08"},
                        {"label": "", "value": "bad"},
                        {"label": "空值", "value": ""},
                    ],
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        payload = {
            "parse": 0,
            "url": f"https://stream.example{id}.m3u8",
        }
        if id == "/play/1":
            payload["ext"] = [
                {"label": "播放", "value": "18万"},
                {"label": "热度", "value": "95"},
            ]
        elif id == "/play/2":
            payload["ext"] = [
                {"label": "播放", "value": " "},
                {"label": "", "value": "ignored"},
            ]
        return payload


class ClickableDetailFieldPayloadSpider(FakeSpider):
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "红果短剧",
                    "vod_play_from": "默认线",
                    "vod_play_url": "第1集$/play/1",
                    "ext": [
                        {
                            "label": "演员",
                            "value": [
                                {"label": "演员1", "action": {"type": "search", "value": "演员1"}},
                                {"label": "演员2", "action": {"type": "detail", "value": "actor-2"}},
                            ],
                        },
                        {"label": "标签", "value": ["动作", "冒险"]},
                    ],
                }
            ]
        }


class InvalidClickableDetailFieldPayloadSpider(FakeSpider):
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "红果短剧",
                    "vod_play_from": "默认线",
                    "vod_play_url": "第1集$/play/1",
                    "ext": [
                        {
                            "label": "导演",
                            "value": [
                                {"label": "导演1", "action": {"type": "unknown", "value": "director-1"}},
                                {"label": "", "action": {"type": "search", "value": "ignored"}},
                            ],
                        }
                    ],
                }
            ]
        }


def _detail_field_signature(fields: list[PlaybackDetailField]) -> list[tuple[str, list[tuple[str, str | None, str | None]]]]:
    signature: list[tuple[str, list[tuple[str, str | None, str | None]]]] = []
    for field in fields:
        parts = getattr(field, "value_parts", None)
        if parts is None:
            signature.append(
                (
                    field.label,
                    [(str(getattr(field, "value", "")).strip(), None, None)],
                )
            )
            continue
        signature.append(
            (
                field.label,
                [
                    (
                        getattr(part, "label", ""),
                        getattr(getattr(part, "action", None), "type", None),
                        getattr(getattr(part, "action", None), "value", None),
                    )
                    for part in parts
                ],
            )
        )
    return signature


class FlakyCoverPayloadSpider(FakeSpider):
    def __init__(self) -> None:
        self._calls: dict[str, int] = {}

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "随机封面歌单",
                    "vod_pic": "poster-detail",
                    "vod_play_from": "默认线",
                    "vod_play_url": "第1首$/play/1#第2首$/play/2",
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        self._calls[id] = self._calls.get(id, 0) + 1
        payload = {
            "parse": 0,
            "url": f"https://stream.example{id}.m3u8",
            "header": {"Referer": "https://site.example"},
        }
        if id == "/play/1" and self._calls[id] == 1:
            payload["cover"] = "https://img.example/song-1.jpg"
        elif id == "/play/2":
            payload["cover"] = "https://img.example/song-2.jpg"
        else:
            payload["cover"] = " "
        return payload


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


class VarietySeasonSpider(FakeSpider):
    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "现在就出发 第三季",
                    "vod_pic": "poster-detail",
                    "vod_play_from": "默认线",
                    "vod_play_url": "20250427$/play/1#20250504$/play/2#20250511$/play/3",
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


class FilterSpider(FakeSpider):
    def __init__(self) -> None:
        self.category_calls: list[tuple[str, int, bool, dict[str, str]]] = []

    def homeContent(self, filter):
        return {
            "class": [
                {"type_id": "movie", "type_name": "电影"},
                {"type_id": "tv", "type_name": "剧集"},
                {"type_id": "adult", "type_name": "成人视频"},
            ],
            "filters": {
                "movie": [
                    {
                        "key": "sc",
                        "name": "影视类型",
                        "value": [
                            {"n": "不限", "v": "0"},
                            {"n": "动作", "v": "6"},
                        ],
                    }
                ],
                "tv": [
                    {
                        "key": "status",
                        "name": "剧集状态",
                        "value": [
                            {"n": "不限", "v": "0"},
                            {"n": "连载中", "v": "1"},
                        ],
                    }
                ],
            },
            "list": [],
        }

    def categoryContent(self, tid, pg, filter, extend):
        self.category_calls.append((tid, pg, filter, dict(extend)))
        return {
            "list": [{"vod_id": f"/detail/{tid}-{pg}", "vod_name": f"{tid}-{pg}"}],
            "total": 1,
        }


def test_controller_load_categories_prepends_home_when_home_list_exists() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    categories = controller.load_categories()
    items, total = controller.load_items("home", 1)

    assert [item.type_name for item in categories] == ["推荐", "热门", "剧场"]
    assert [item.vod_name for item in items] == ["首页推荐"]
    assert total == 1


def test_controller_initializes_spider_lazily_before_first_plugin_call() -> None:
    spider = DeferredInitSpider()
    controller = SpiderPluginController(
        spider,
        plugin_name="懒加载插件",
        search_enabled=True,
        spider_initializer=lambda: spider.init("cfg"),
    )

    assert spider.init_calls == 0

    categories = controller.load_categories()
    items, total = controller.load_items("home", 1)

    assert spider.init_calls == 1
    assert spider.extend == "cfg"
    assert [item.type_name for item in categories] == ["推荐", "热门", "剧场"]
    assert [item.vod_name for item in items] == ["首页推荐"]
    assert total == 1


def test_controller_maps_home_filters_to_matching_categories() -> None:
    controller = SpiderPluginController(FilterSpider(), plugin_name="筛选插件", search_enabled=True)

    categories = controller.load_categories()

    movie = categories[0]
    tv = categories[1]

    assert movie.type_id == "movie"
    assert movie.filters == [
        CategoryFilter(
            key="sc",
            name="影视类型",
            options=[
                CategoryFilterOption(name="不限", value="0"),
                CategoryFilterOption(name="动作", value="6"),
            ],
        )
    ]
    assert tv.filters[0].key == "status"
    assert [option.name for option in tv.filters[0].options] == ["不限", "连载中"]


def test_controller_keeps_empty_filter_option_values() -> None:
    class EmptyValueFilterSpider(FakeSpider):
        def homeContent(self, filter):
            return {
                "class": [{"type_id": "movie", "type_name": "电影"}],
                "filters": {
                    "movie": [
                        {
                            "key": "class",
                            "name": "类型",
                            "value": [
                                {"n": "全部", "v": ""},
                                {"n": "爱情", "v": "爱情"},
                            ],
                        }
                    ]
                },
                "list": [],
            }

    controller = SpiderPluginController(EmptyValueFilterSpider(), plugin_name="筛选插件", search_enabled=True)

    categories = controller.load_categories()

    assert categories[0].filters == [
        CategoryFilter(
            key="class",
            name="类型",
            options=[
                CategoryFilterOption(name="全部", value=""),
                CategoryFilterOption(name="爱情", value="爱情"),
            ],
        )
    ]


def test_controller_passes_selected_filters_into_category_content_extend() -> None:
    spider = FilterSpider()
    controller = SpiderPluginController(spider, plugin_name="筛选插件", search_enabled=True)

    items, total = controller.load_items("movie", 2, filters={"sc": "6"})

    assert total == 1
    assert items[0].vod_name == "movie-2"
    assert spider.category_calls == [("movie", 2, False, {"sc": "6"})]


def test_controller_ignores_filters_for_home_category_items() -> None:
    spider = FilterSpider()
    controller = SpiderPluginController(spider, plugin_name="筛选插件", search_enabled=True)

    controller.load_categories()
    controller.load_items("home", 1, filters={"sc": "6"})

    assert spider.category_calls == []


def test_controller_applies_category_overrides_without_changing_type_ids() -> None:
    controller = SpiderPluginController(
        FilterSpider(),
        plugin_name="筛选插件",
        search_enabled=True,
        category_overrides_json='{"order":["tv","movie"],"hidden":["adult"],"renames":{"tv":"剧集","movie":"影片"}}',
    )

    categories = controller.load_categories()

    assert [(item.type_id, item.type_name) for item in categories] == [
        ("tv", "剧集"),
        ("movie", "影片"),
    ]


def test_controller_appends_new_categories_not_present_in_saved_order() -> None:
    class ExtraCategorySpider(FilterSpider):
        def homeContent(self, filter):
            payload = super().homeContent(filter)
            payload["class"].append({"type_id": "variety", "type_name": "综艺"})
            return payload

    controller = SpiderPluginController(
        ExtraCategorySpider(),
        plugin_name="筛选插件",
        search_enabled=True,
        category_overrides_json='{"order":["tv","movie"],"renames":{"tv":"剧集"}}',
    )

    categories = controller.load_categories()

    assert [item.type_id for item in categories] == ["tv", "movie", "adult", "variety"]
    assert categories[3].type_name == "综艺"


def test_controller_exposes_raw_categories_for_category_manager() -> None:
    controller = SpiderPluginController(
        FilterSpider(),
        plugin_name="筛选插件",
        search_enabled=True,
        category_overrides_json='{"renames":{"movie":"影片"}}',
    )

    raw_categories = controller.load_raw_categories()
    visible_categories = controller.load_categories()

    assert [(item.type_id, item.type_name) for item in raw_categories] == [
        ("movie", "电影"),
        ("tv", "剧集"),
        ("adult", "成人视频"),
    ]
    assert visible_categories[0].type_name == "影片"


def test_controller_search_and_category_mapping() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    items, total = controller.search_items("庆余年", 1)
    category_items, category_total = controller.load_items("tv", 2)

    assert total == 1
    assert items[0].vod_name == "庆余年"
    assert category_total == 3
    assert category_items[0].vod_name == "tv-2"


def test_controller_returns_pagecount_for_category_and_search_results() -> None:
    class PageCountSpider(FakeSpider):
        def categoryContent(self, tid, pg, filter, extend):
            return {
                "list": [{"vod_id": f"/detail/{tid}-{pg}", "vod_name": f"{tid}-{pg}"}],
                "total": 47,
                "pagecount": 4,
            }

        def searchContent(self, key, quick, pg=1, category=""):
            return {
                "list": [{"vod_id": f"/detail/{key}", "vod_name": key}],
                "total": 11,
                "pagecount": 2,
            }

    controller = SpiderPluginController(
        PageCountSpider(),
        plugin_name="页数插件",
        search_enabled=True,
    )

    _category_items, category_total = controller.load_items("tv", 1)
    _search_items, search_total = controller.search_items("庆余年", 1)

    assert category_total == 4
    assert search_total == 2
    assert category_total.pagecount == 4
    assert category_total.total == 47
    assert search_total.pagecount == 2
    assert search_total.total == 11


def test_controller_calculates_pagecount_from_total_when_pagecount_is_missing() -> None:
    class TotalOnlySpider(FakeSpider):
        def categoryContent(self, tid, pg, filter, extend):
            return {
                "list": [{"vod_id": f"/detail/{tid}-{pg}", "vod_name": f"{tid}-{pg}"}],
                "total": 61,
            }

        def searchContent(self, key, quick, pg=1, category=""):
            return {
                "list": [{"vod_id": f"/detail/{key}", "vod_name": key}],
                "total": 31,
            }

    controller = SpiderPluginController(
        TotalOnlySpider(),
        plugin_name="页数插件",
        search_enabled=True,
    )

    _category_items, category_total = controller.load_items("tv", 1)
    _search_items, search_total = controller.search_items("庆余年", 1)

    assert category_total == 3
    assert search_total == 2


def test_controller_load_folder_items_uses_category_content_with_folder_id() -> None:
    class FolderSpider(FakeSpider):
        def __init__(self) -> None:
            self.category_calls = []

        def categoryContent(self, tid, pg, filter, extend):
            self.category_calls.append((tid, pg, filter, extend))
            return {
                "list": [
                    {
                        "vod_id": "child-1",
                        "vod_name": "子目录",
                        "vod_tag": "folder",
                    }
                ],
                "total": 1,
            }

    spider = FolderSpider()
    controller = SpiderPluginController(spider, plugin_name="目录插件", search_enabled=True)

    items, total = controller.load_folder_items("folder-1")

    assert spider.category_calls == [("folder-1", 1, False, {})]
    assert total == 1
    assert [(item.vod_id, item.vod_name, item.vod_tag) for item in items] == [
        ("child-1", "子目录", "folder")
    ]


def test_controller_passes_selected_category_into_search_content() -> None:
    spider = SearchCategorySpider()
    controller = SpiderPluginController(spider, plugin_name="分类搜索插件", search_enabled=True)

    items, total = controller.search_items("庆余年", 1, category_id="tv")

    assert total == 1
    assert items[0].vod_name == "庆余年"
    assert spider.search_calls == [("庆余年", False, 1, "tv")]


def test_controller_search_normalizes_home_category_to_empty_string() -> None:
    spider = SearchCategorySpider()
    controller = SpiderPluginController(spider, plugin_name="分类搜索插件", search_enabled=True)

    controller.search_items("庆余年", 1, category_id="home")

    assert spider.search_calls == [("庆余年", False, 1, "")]


def test_controller_search_skips_category_for_legacy_search_signature() -> None:
    spider = LegacySearchSpider()
    controller = SpiderPluginController(spider, plugin_name="旧版搜索插件", search_enabled=True)

    items, total = controller.search_items("庆余年", 2, category_id="tv")

    assert total == 1
    assert items[0].vod_name == "庆余年"
    assert spider.search_calls == [("庆余年", False, 2)]


def test_controller_disables_search_when_spider_has_no_search_content() -> None:
    controller = SpiderPluginController(NoSearchSpider(), plugin_name="本地插件", search_enabled=True)

    request = controller.build_request("/detail/1")

    assert controller.supports_search is False
    assert request.playlist[0].title == "第1集"
    with pytest.raises(ApiError, match="当前插件不支持搜索"):
        controller.search_items("庆余年", 1)


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


class RelatedJumpSpider(FakeSpider):
    def detailContent(self, ids):
        vod_id = str(ids[0])
        return {
            "list": [
                {
                    "vod_id": vod_id,
                    "vod_name": "同系列第二季",
                    "vod_pic": "poster-detail",
                    "vod_play_from": "线路",
                    "vod_play_url": "#".join(f"第{i}集$/play/{i}" for i in range(1, 66)),
                    "vod_related": [
                        {"vod_id": "season-1", "vod_name": "同系列第一季"},
                        {"vod_id": vod_id, "vod_name": "同系列第二季"},
                    ],
                }
            ]
        }


def test_controller_related_jump_request_restores_history() -> None:
    loaded_calls: list[tuple[str, str]] = []

    def history_loader(vod_id, vod_name=""):
        loaded_calls.append((vod_id, vod_name))
        return HistoryRecord(
            id=1,
            key=vod_id,
            vod_name="同系列第二季",
            vod_pic="",
            vod_remarks="",
            episode=61,
            episode_url="",
            position=43000,
            opening=0,
            ending=0,
            speed=1.25,
            create_time=0,
            duration=58000,
        )

    controller = SpiderPluginController(
        RelatedJumpSpider(),
        plugin_name="系列插件",
        search_enabled=False,
        playback_history_loader=history_loader,
    )

    request = controller.build_request("season-2")

    related_ids = [entry.vod_id for entry in request.vod.vod_related]
    assert related_ids == ["season-1", "season-2"]

    class NoFallbackApi:
        def get_history(self, key):
            raise AssertionError("plugin session should use its own history loader")

    session = PlayerController(NoFallbackApi()).create_session(
        request.vod,
        request.playlist,
        request.clicked_index,
        playlists=request.playlists,
        use_local_history=request.use_local_history,
        restore_history=request.restore_history,
        playback_loader=request.playback_loader,
        async_playback_loader=request.async_playback_loader,
        playback_history_loader=request.playback_history_loader,
        source_kind="plugin",
        source_key="683",
    )

    assert session.start_index == 61
    assert session.start_position_seconds == 43
    assert session.speed == 1.25
    # 详情的 vod_name 会随历史查询下发,供同名兜底续播使用
    assert loaded_calls == [("season-2", "同系列第二季")]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_controller_maps_node_spider_detail_and_player_content(tmp_path: Path) -> None:
    plugin_path = tmp_path / "controller-plugin.mjs"
    plugin_path.write_text(
        """
export default {
  home() { return { class: [{ type_id: "tv", type_name: "剧集" }], list: [] } },
  category(tid, pg) { return { list: [{ vod_id: "/detail/1", vod_name: "剧集1", vod_pic: "poster" }], total: 1 } },
  detail(id) {
    return {
      list: [{
        vod_id: id,
        vod_name: "JS剧集",
        vod_pic: "poster-detail",
        vod_play_from: "备用线$$$极速线",
        vod_play_url: "第1集$/play/1#第2集$https://media.example/2.m3u8$$$第3集$/play/3"
      }]
    }
  },
  play(flag, id) { return { parse: 0, url: `https://stream.example${id}.m3u8`, header: { Referer: "https://site.example" } } }
}
""".strip(),
        encoding="utf-8",
    )
    from atv_player.plugins.node_spider import NodeSpider

    spider = NodeSpider(plugin_path=plugin_path, cache_dir=tmp_path / "cache", plugin_id=77)
    controller = SpiderPluginController(spider, plugin_name="JS剧集", search_enabled=False)

    request = controller.build_request("/detail/1")

    assert request.vod.vod_name == "JS剧集"
    assert request.playlists[0][0].title == "第1集"
    assert request.playlists[0][0].vod_id == "/play/1"
    assert request.playlists[0][1].url == "https://media.example/2.m3u8"
    assert request.playback_loader is not None
    request.playback_loader(request.playlists[0][0])
    assert request.playlists[0][0].url == "https://stream.example/play/1.m3u8"
    assert request.playlists[0][0].headers["Referer"] == "https://site.example"
    spider.destroy()


def test_controller_build_request_maps_absolute_subt_into_external_subtitles() -> None:
    controller = SpiderPluginController(
        SubtitlePayloadSpider("https://cdn.example/subtitles/episode-1.srt"),
        plugin_name="字幕插件",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert [(sub.name, sub.url, sub.format, sub.source) for sub in first.external_subtitles] == [
        ("外挂字幕 [插件]", "https://cdn.example/subtitles/episode-1.srt", "application/x-subrip", "spider"),
    ]


def test_controller_build_request_maps_spider_qualities_matching_top_level_url() -> None:
    controller = SpiderPluginController(
        QualityPayloadSpider(
            "https://stream.example/play/1-1080.m3u8",
            [
                {"id": "1080p", "label": "1080P", "url": "https://stream.example/play/1-1080.m3u8"},
                {"id": "720p", "label": "720P", "url": "https://stream.example/play/1-720.m3u8"},
            ],
        ),
        plugin_name="清晰度插件",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1-1080.m3u8"
    assert [(quality.id, quality.label, quality.url) for quality in first.playback_qualities] == [
        ("1080p", "1080P", "https://stream.example/play/1-1080.m3u8"),
        ("720p", "720P", "https://stream.example/play/1-720.m3u8"),
    ]
    assert first.selected_playback_quality_id == "1080p"


def test_controller_build_request_falls_back_to_first_valid_spider_quality() -> None:
    controller = SpiderPluginController(
        QualityPayloadSpider(
            "https://stream.example/play/1-default.m3u8",
            [
                {"id": "720p", "label": "720P", "url": "https://stream.example/play/1-720.m3u8"},
                {"id": "480p", "label": "480P", "url": "https://stream.example/play/1-480.m3u8"},
            ],
        ),
        plugin_name="清晰度插件",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1-default.m3u8"
    assert [quality.id for quality in first.playback_qualities] == ["720p", "480p"]
    assert first.selected_playback_quality_id == "720p"


def test_controller_build_request_ignores_malformed_spider_quality_entries() -> None:
    controller = SpiderPluginController(
        QualityPayloadSpider(
            "https://stream.example/play/1-1080.m3u8",
            [
                {"id": "", "label": "无效", "url": "https://stream.example/play/invalid.m3u8"},
                {"id": "bad-html", "label": "页面地址", "url": "https://example.com/watch/1.html"},
                {"id": "720p", "label": "720P", "url": "https://stream.example/play/1-720.m3u8"},
            ],
        ),
        plugin_name="清晰度插件",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1-1080.m3u8"
    assert [(quality.id, quality.label, quality.url) for quality in first.playback_qualities] == [
        ("720p", "720P", "https://stream.example/play/1-720.m3u8"),
    ]
    assert first.selected_playback_quality_id == "720p"


def test_controller_build_request_hydrates_prefilled_youtube_url_via_ytdlp() -> None:
    class FakeYtdlpService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int | None]] = []

        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

        def resolve(self, url: str, *, max_height: int | None = None, selected_audio_track_id: str = ""):
            del selected_audio_track_id
            self.calls.append((url, max_height))
            return type(
                "Result",
                (),
                {
                    "url": url,
                    "audio_url": "",
                    "headers": {"Referer": "https://www.youtube.com/"},
                    "subtitles": [],
                    "qualities": [
                        type("Quality", (), {"id": "ytdlp_2160", "label": "2160P", "url": "", "ytdl_format": "bestvideo[height<=2160]+bestaudio/best[height<=2160]/bestvideo+bestaudio/best"})(),
                        type("Quality", (), {"id": "ytdlp_1080", "label": "1080P", "url": "", "ytdl_format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best"})(),
                    ],
                    "ytdl_format": "bestvideo+bestaudio/best",
                    "selected_quality_id": "ytdlp_2160",
                    "title": "YouTube 剧集",
                    "thumbnail": "",
                    "description": "",
                    "duration_seconds": 0,
                },
            )()

        def apply_result(self, result, *, vod=None, item=None, source_url: str = "") -> None:
            resolved_title = result.title or source_url
            if vod is not None:
                vod.vod_name = resolved_title
                vod.vod_pic = result.thumbnail
                vod.vod_content = result.description
            if item is None:
                return
            item.url = result.url
            item.original_url = source_url
            item.headers = dict(result.headers)
            item.audio_url = result.audio_url
            item.ytdl_format = result.ytdl_format
            item.playback_qualities = list(result.qualities)
            item.external_subtitles = list(result.subtitles)
            item.duration_seconds = result.duration_seconds
            item.title = resolved_title
            item.media_title = resolved_title
            item.selected_playback_quality_id = result.selected_quality_id

    service = FakeYtdlpService()
    controller = SpiderPluginController(
        YoutubeDetailSpider(),
        plugin_name="YouTube插件",
        search_enabled=True,
        yt_dlp_service=service,
    )

    request = controller.build_request("/detail/youtube")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert service.calls == [("https://www.youtube.com/watch?v=test123", None)]
    assert first.url == "https://www.youtube.com/watch?v=test123"
    assert first.headers == {"Referer": "https://www.youtube.com/"}
    assert first.ytdl_format == "bestvideo+bestaudio/best"
    assert [quality.id for quality in first.playback_qualities] == ["ytdlp_2160", "ytdlp_1080"]
    assert first.selected_playback_quality_id == "ytdlp_2160"


def test_controller_build_request_resolves_playercontent_youtube_url_via_ytdlp() -> None:
    class FakeYtdlpService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int | None]] = []

        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

        def resolve(self, url: str, *, max_height: int | None = None, selected_audio_track_id: str = ""):
            del selected_audio_track_id
            self.calls.append((url, max_height))
            return type(
                "Result",
                (),
                {
                    "url": url,
                    "audio_url": "",
                    "headers": {"Referer": "https://www.youtube.com/"},
                    "subtitles": [],
                    "qualities": [
                        type("Quality", (), {"id": "ytdlp_2160", "label": "2160P", "url": "", "ytdl_format": "bestvideo[height<=2160]+bestaudio/best[height<=2160]/bestvideo+bestaudio/best"})(),
                    ],
                    "ytdl_format": "bestvideo+bestaudio/best",
                    "selected_quality_id": "ytdlp_2160",
                    "title": "YouTube 剧集",
                    "thumbnail": "",
                    "description": "",
                    "duration_seconds": 0,
                },
            )()

        def apply_result(self, result, *, vod=None, item=None, source_url: str = "") -> None:
            resolved_title = result.title or source_url
            if vod is not None:
                vod.vod_name = resolved_title
                vod.vod_pic = result.thumbnail
                vod.vod_content = result.description
            if item is None:
                return
            item.url = result.url
            item.original_url = source_url
            item.headers = dict(result.headers)
            item.audio_url = result.audio_url
            item.ytdl_format = result.ytdl_format
            item.playback_qualities = list(result.qualities)
            item.external_subtitles = list(result.subtitles)
            item.duration_seconds = result.duration_seconds
            item.title = resolved_title
            item.media_title = resolved_title
            item.selected_playback_quality_id = result.selected_quality_id

    service = FakeYtdlpService()
    controller = SpiderPluginController(
        YoutubePlayerSpider(),
        plugin_name="YouTube插件",
        search_enabled=True,
        yt_dlp_service=service,
    )

    request = controller.build_request("/detail/youtube-player")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert service.calls == [("https://www.youtube.com/watch?v=test123", None)]
    assert first.url == "https://www.youtube.com/watch?v=test123"
    assert first.headers == {"Referer": "https://www.youtube.com/"}
    assert first.ytdl_format == "bestvideo+bestaudio/best"
    assert [quality.id for quality in first.playback_qualities] == ["ytdlp_2160"]
    assert first.selected_playback_quality_id == "ytdlp_2160"


def test_controller_resolve_play_item_preserves_selected_ytdlp_quality_when_url_is_reloaded() -> None:
    class FakeYtdlpService:
        def __init__(self) -> None:
            self.resolve_calls: list[tuple[str, int | None]] = []
            self.resolve_for_quality_calls: list[tuple[str, str]] = []

        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

        def resolve(self, url: str, *, max_height: int | None = None, selected_audio_track_id: str = ""):
            del selected_audio_track_id
            self.resolve_calls.append((url, max_height))
            raise AssertionError("resolve() should not be used when a ytdlp quality is selected")

        def resolve_for_quality(self, url: str, quality_id: str, *, audio_track_id: str = ""):
            del audio_track_id
            self.resolve_for_quality_calls.append((url, quality_id))
            return type(
                "Result",
                (),
                {
                    "url": f"https://stream.example/{quality_id}.m3u8",
                    "audio_url": "",
                    "headers": {"Referer": "https://www.youtube.com/"},
                    "subtitles": [],
                    "qualities": [
                        type("Quality", (), {"id": "ytdlp_1080", "label": "1080P", "url": "", "ytdl_format": ""})(),
                        type("Quality", (), {"id": "ytdlp_720", "label": "720P", "url": "", "ytdl_format": ""})(),
                    ],
                    "ytdl_format": "",
                    "selected_quality_id": quality_id,
                    "title": "YouTube 剧集",
                    "thumbnail": "",
                    "description": "",
                    "duration_seconds": 0,
                },
            )()

        def apply_result(self, result, *, vod=None, item=None, source_url: str = "") -> None:
            del vod
            if item is None:
                return
            item.url = result.url
            item.original_url = source_url
            item.headers = dict(result.headers)
            item.audio_url = result.audio_url
            item.ytdl_format = result.ytdl_format
            item.playback_qualities = list(result.qualities)
            item.external_subtitles = list(result.subtitles)
            item.duration_seconds = result.duration_seconds
            item.title = result.title
            item.media_title = result.title
            item.selected_playback_quality_id = result.selected_quality_id

    service = FakeYtdlpService()
    controller = SpiderPluginController(
        YoutubePlayerSpider(),
        plugin_name="YouTube插件",
        search_enabled=True,
        yt_dlp_service=service,
    )

    request = controller.build_request("/detail/youtube-player")
    first = request.playlists[0][0]
    session = type(
        "Session",
        (),
        {
            "vod": request.vod,
            "playlist": request.playlist,
            "video_cover_override": "",
        },
    )()

    first.url = ""
    first.original_url = "https://www.youtube.com/watch?v=test123"
    first.selected_playback_quality_id = "ytdlp_720"

    assert request.playback_loader is not None
    request.playback_loader(session, first)

    assert service.resolve_calls == []
    assert service.resolve_for_quality_calls == [("https://www.youtube.com/watch?v=test123", "ytdlp_720")]
    assert first.url == "https://stream.example/ytdlp_720.m3u8"
    assert first.selected_playback_quality_id == "ytdlp_720"


def test_controller_resolve_play_item_passes_selected_ytdlp_audio_track() -> None:
    class FakeYtdlpService:
        def __init__(self) -> None:
            self.resolve_calls: list[tuple[str, int | None, str]] = []
            self.resolve_for_quality_calls: list[tuple[str, str, str]] = []

        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

        def resolve(self, url: str, *, max_height: int | None = None, selected_audio_track_id: str = ""):
            self.resolve_calls.append((url, max_height, selected_audio_track_id))
            raise AssertionError("resolve() should not be used when a ytdlp quality is selected")

        def resolve_for_quality(self, url: str, quality_id: str, *, audio_track_id: str = ""):
            self.resolve_for_quality_calls.append((url, quality_id, audio_track_id))
            return type(
                "Result",
                (),
                {
                    "url": f"https://stream.example/{quality_id}.m3u8",
                    "audio_url": "",
                    "headers": {"Referer": "https://www.youtube.com/"},
                    "subtitles": [],
                    "qualities": [],
                    "audio_tracks": [],
                    "selected_audio_track_id": audio_track_id,
                    "ytdl_format": "298+140-dub",
                    "selected_quality_id": quality_id,
                    "title": "YouTube 剧集",
                    "thumbnail": "",
                    "description": "",
                    "duration_seconds": 0,
                },
            )()

        def apply_result(self, result, *, vod=None, item=None, source_url: str = "") -> None:
            del vod
            if item is None:
                return
            item.url = result.url
            item.original_url = source_url
            item.headers = dict(result.headers)
            item.audio_url = result.audio_url
            item.audio_tracks = list(result.audio_tracks)
            item.selected_audio_track_id = result.selected_audio_track_id
            item.ytdl_format = result.ytdl_format
            item.playback_qualities = list(result.qualities)
            item.external_subtitles = list(result.subtitles)
            item.duration_seconds = result.duration_seconds
            item.title = result.title
            item.media_title = result.title
            item.selected_playback_quality_id = result.selected_quality_id

    service = FakeYtdlpService()
    controller = SpiderPluginController(
        YoutubePlayerSpider(),
        plugin_name="YouTube插件",
        search_enabled=True,
        yt_dlp_service=service,
    )

    request = controller.build_request("/detail/youtube-player")
    first = request.playlists[0][0]
    session = type(
        "Session",
        (),
        {
            "vod": request.vod,
            "playlist": request.playlist,
            "video_cover_override": "",
        },
    )()

    first.url = ""
    first.original_url = "https://www.youtube.com/watch?v=test123"
    first.selected_playback_quality_id = "ytdlp_720"
    first.selected_audio_track_id = "ytdlp_audio_zh_140-dub"

    assert request.playback_loader is not None
    request.playback_loader(session, first)

    assert service.resolve_calls == []
    assert service.resolve_for_quality_calls == [
        ("https://www.youtube.com/watch?v=test123", "ytdlp_720", "ytdlp_audio_zh_140-dub")
    ]
    assert first.selected_audio_track_id == "ytdlp_audio_zh_140-dub"
    assert first.ytdl_format == "298+140-dub"


def test_controller_playback_loader_overwrites_session_vod_with_ytdlp_metadata() -> None:
    class FakeYtdlpService:
        def is_available(self) -> bool:
            return True

        def can_resolve(self, url: str) -> bool:
            return "youtube.com" in url

        def resolve(self, url: str, *, max_height: int | None = None, selected_audio_track_id: str = ""):
            del max_height, selected_audio_track_id
            return type(
                "Result",
                (),
                {
                    "url": url,
                    "audio_url": "",
                    "headers": {"Referer": "https://www.youtube.com/"},
                    "subtitles": [],
                    "qualities": [],
                    "ytdl_format": "bestvideo+bestaudio/best",
                    "selected_quality_id": "ytdlp_2160",
                    "title": "Resolved YouTube Title",
                    "thumbnail": "https://img.example/youtube.jpg",
                    "description": "resolved plugin description",
                    "duration_seconds": 777,
                },
            )()

        def apply_result(self, result, *, vod=None, item=None, source_url: str = "") -> None:
            resolved_title = result.title or source_url
            if vod is not None:
                vod.vod_name = resolved_title
                vod.vod_pic = result.thumbnail
                vod.vod_content = result.description
            if item is None:
                return
            item.url = result.url
            item.original_url = source_url
            item.headers = dict(result.headers)
            item.audio_url = result.audio_url
            item.ytdl_format = result.ytdl_format
            item.playback_qualities = list(result.qualities)
            item.external_subtitles = list(result.subtitles)
            item.duration_seconds = result.duration_seconds
            item.title = resolved_title
            item.media_title = resolved_title
            item.selected_playback_quality_id = result.selected_quality_id

    controller = SpiderPluginController(
        YoutubeDetailSpider(),
        plugin_name="YouTube插件",
        search_enabled=True,
        yt_dlp_service=FakeYtdlpService(),
    )

    request = controller.build_request("/detail/youtube")
    first = request.playlists[0][0]
    session = type(
        "Session",
        (),
        {
            "vod": request.vod,
            "playlist": request.playlist,
            "video_cover_override": "",
        },
    )()

    assert request.playback_loader is not None
    request.playback_loader(session, first)

    assert session.vod.vod_name == "Resolved YouTube Title"
    assert session.vod.vod_pic == "https://img.example/youtube.jpg"
    assert session.vod.vod_content == "resolved plugin description"
    assert first.title == "Resolved YouTube Title"
    assert first.media_title == "Resolved YouTube Title"
    assert first.duration_seconds == 777


def test_controller_build_request_resolves_relative_subt_against_base_url() -> None:
    controller = SpiderPluginController(
        SubtitlePayloadSpider("/files/subtitles/episode-1.ass"),
        plugin_name="字幕插件",
        search_enabled=True,
        base_url_loader=lambda: "http://127.0.0.1:4567",
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert [(sub.url, sub.format, sub.source) for sub in first.external_subtitles] == [
        ("http://127.0.0.1:4567/files/subtitles/episode-1.ass", "text/x-ass", "spider"),
    ]


def test_controller_build_request_moves_local_absolute_subt_path_into_cache_dir(tmp_path, monkeypatch) -> None:
    subtitle_path = tmp_path / "episode-1.srt"
    subtitle_text = "1\n00:00:00,000 --> 00:00:01,000\nhello\n"
    subtitle_path.write_text(subtitle_text, encoding="utf-8")
    cache_root = tmp_path / "app-cache"
    monkeypatch.setattr(controller_module, "app_cache_dir", lambda: cache_root)
    controller = SpiderPluginController(
        SubtitlePayloadSpider(str(subtitle_path)),
        plugin_name="字幕插件",
        search_enabled=True,
        base_url_loader=lambda: "http://127.0.0.1:4567",
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    cached_subtitle_path = cache_root / "subtitles" / "episode-1.srt"
    assert [(sub.url, sub.format, sub.source) for sub in first.external_subtitles] == [
        (str(cached_subtitle_path), "application/x-subrip", "spider"),
    ]
    assert cached_subtitle_path.read_text(encoding="utf-8") == subtitle_text
    assert not subtitle_path.exists()


def test_controller_build_request_writes_inline_subt_payload_into_cache_dir(tmp_path, monkeypatch) -> None:
    subtitle_text = "1\n00:00:00,000 --> 00:00:01,000\nhello\n"
    cache_root = tmp_path / "app-cache"
    monkeypatch.setattr(controller_module, "app_cache_dir", lambda: cache_root)
    controller = SpiderPluginController(
        SubtitlePayloadSpider(f"application/x-subrip\n{subtitle_text}"),
        plugin_name="字幕插件",
        search_enabled=True,
        base_url_loader=lambda: "http://127.0.0.1:4567",
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert len(first.external_subtitles) == 1
    subtitle = first.external_subtitles[0]
    subtitle_path = Path(subtitle.url)
    assert subtitle.format == "application/x-subrip"
    assert subtitle.source == "spider"
    assert subtitle_path.parent == cache_root / "subtitles"
    assert subtitle_path.suffix == ".srt"
    assert subtitle_path.read_text(encoding="utf-8") == subtitle_text


def test_controller_build_request_prefers_generated_karaoke_subtitle_over_subt(tmp_path, monkeypatch) -> None:
    cache_root = tmp_path / "app-cache"
    monkeypatch.setattr(controller_module, "app_cache_dir", lambda: cache_root)
    controller = SpiderPluginController(
        LyricPayloadSpider(
            {
                "format": "kugou-krc",
                "text": "[0,1800]<0,450,0>轻<450,450,0>舟<900,450,0>已<1350,450,0>过",
            },
            subt="https://cdn.example/fallback.srt",
        ),
        plugin_name="歌词插件",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert len(first.external_subtitles) == 1
    subtitle = first.external_subtitles[0]
    assert subtitle.name == "逐字歌词 [插件]"
    assert subtitle.format == "text/x-ass"
    assert subtitle.source == "spider"
    assert Path(subtitle.url).suffix == ".ass"
    assert r"{\kf45}轻{\kf45}舟{\kf45}已{\kf45}过" in Path(subtitle.url).read_text(encoding="utf-8")


def test_controller_build_request_prefers_generated_netease_karaoke_subtitle_over_subt(
    tmp_path, monkeypatch
) -> None:
    cache_root = tmp_path / "app-cache"
    monkeypatch.setattr(controller_module, "app_cache_dir", lambda: cache_root)
    controller = SpiderPluginController(
        LyricPayloadSpider(
            {
                "format": "netease-yrc",
                "text": "[0,1800](0,450,0)轻(450,450,0)舟(900,450,0)已(1350,450,0)过",
            },
            subt="https://cdn.example/fallback.srt",
        ),
        plugin_name="歌词插件",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert len(first.external_subtitles) == 1
    subtitle = first.external_subtitles[0]
    assert subtitle.name == "逐字歌词 [插件]"
    assert subtitle.format == "text/x-ass"
    assert subtitle.source == "spider"
    assert Path(subtitle.url).suffix == ".ass"
    assert r"{\kf45}轻{\kf45}舟{\kf45}已{\kf45}过" in Path(subtitle.url).read_text(encoding="utf-8")


def test_controller_build_request_falls_back_to_subt_when_lyric_format_is_unknown() -> None:
    controller = SpiderPluginController(
        LyricPayloadSpider(
            {"format": "unknown-karaoke", "text": "[0,1000](0,1000,0)测试"},
            subt="https://cdn.example/fallback.srt",
        ),
        plugin_name="歌词插件",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert [(sub.name, sub.url, sub.format, sub.source) for sub in first.external_subtitles] == [
        ("外挂字幕 [插件]", "https://cdn.example/fallback.srt", "application/x-subrip", "spider"),
    ]


def test_controller_build_request_ignores_invalid_lyric_without_breaking_playback() -> None:
    controller = SpiderPluginController(
        LyricPayloadSpider({"format": "qqmusic-qrc", "text": ""}),
        plugin_name="歌词插件",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.external_subtitles == []


def test_controller_playback_loader_preserves_resolved_subtitles_on_repeat_load() -> None:
    controller = SpiderPluginController(
        SubtitlePayloadSpider("https://cdn.example/subtitles/episode-1.srt"),
        plugin_name="字幕插件",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)
    first_subtitles = [(sub.url, sub.source) for sub in first.external_subtitles]

    request.playback_loader(first)

    assert [(sub.url, sub.source) for sub in first.external_subtitles] == [
        *first_subtitles,
    ]


def test_controller_build_request_ignores_blank_or_unsupported_subt_without_breaking_playback() -> None:
    controller = SpiderPluginController(
        SubtitlePayloadSpider("subtitle.srt"),
        plugin_name="字幕插件",
        search_enabled=True,
        base_url_loader=lambda: "http://127.0.0.1:4567",
    )

    request = controller.build_request("/detail/1")
    first = request.playlists[0][0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.external_subtitles == []


def test_controller_uses_media_title_only_for_short_bare_numeric_playlists() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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


def test_controller_movie_category_from_list_prevents_episode_title_enhancer_for_plugin_detail() -> None:
    class MovieCategorySpider(FakeSpider):
        def homeContent(self, filter):
            return {
                "class": [{"type_id": "movie", "type_name": "电影"}],
                "list": [],
            }

        def categoryContent(self, tid, pg, filter, extend):
            assert tid == "movie"
            return {
                "list": [
                    {
                        "vod_id": "/detail/movie-1",
                        "vod_name": "疯狂动物城2",
                        "vod_pic": "poster-cat",
                    }
                ],
                "page": pg,
                "pagecount": 1,
                "total": 1,
            }

        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "疯狂动物城2",
                        "vod_pic": "poster-detail",
                        "vod_play_from": "默认线",
                        "vod_play_url": "1$/play/1#2$/play/2",
                    }
                ]
            }

    marker = object()

    def episode_title_enhancer_factory(**kwargs):
        vod = kwargs["vod"]
        return None if getattr(vod, "category_name", "") == "电影" else marker

    controller = SpiderPluginController(
        MovieCategorySpider(),
        plugin_name="布布影视",
        search_enabled=True,
        episode_title_enhancer_factory=episode_title_enhancer_factory,
    )

    items, total = controller.load_items("movie", 1)
    assert total == 1
    assert items[0].category_name == "电影"

    request = controller.build_request("/detail/movie-1")

    assert request.vod.category_name == "电影"
    assert request.playlist[0].category_name == "电影"
    assert request.episode_title_enhancer is None


def test_controller_keeps_implicit_numeric_suffix_for_long_bare_numeric_playlists() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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


def test_controller_uses_date_title_for_non_drive_variety_playlist_search() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return []

    class DanmakuVarietySeasonSpider(VarietySeasonSpider):
        def danmaku(self):
            return True

    controller = SpiderPluginController(
        DanmakuVarietySeasonSpider(),
        plugin_name="布布影视",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/variety-1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)
    _wait_until(lambda: first.danmaku_pending is False)

    assert calls == [("search", "现在就出发 第三季 20250427|/play/1")]


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
    assert first.parse_required is True
    assert first.url == "https://media.example/resolved.m3u8"
    assert first.headers == {"Referer": "https://page.example"}


def test_controller_keeps_direct_play_items_parse_disabled() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    assert first.parse_required is False
    assert first.url == "https://stream.example/play/1.m3u8"


class _SessionApiClient:
    def get_history(self, key: str):
        return None

    def save_history(self, payload: dict) -> None:
        return None


def test_controller_updates_session_video_cover_override_from_player_content_cover() -> None:
    controller = SpiderPluginController(CoverPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("/detail/1")
    session = PlayerController(_SessionApiClient()).create_session(
        request.vod,
        request.playlist,
        request.clicked_index,
        playlists=request.playlists,
        playlist_index=request.playlist_index,
        playback_loader=request.playback_loader,
        async_playback_loader=request.async_playback_loader,
        use_local_history=False,
    )
    first = session.playlist[0]

    assert session.video_cover_override == ""
    assert request.vod.vod_pic == "poster-detail"
    assert session.playback_loader is not None

    session.playback_loader(first)

    assert session.video_cover_override == "https://img.example/resolved-cover.jpg"
    assert request.vod.vod_pic == "poster-detail"
    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.headers == {"Referer": "https://site.example"}


def test_controller_keeps_video_cover_override_empty_when_player_content_cover_is_blank() -> None:
    controller = SpiderPluginController(BlankCoverPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("/detail/1")
    session = PlayerController(_SessionApiClient()).create_session(
        request.vod,
        request.playlist,
        request.clicked_index,
        playlists=request.playlists,
        playlist_index=request.playlist_index,
        playback_loader=request.playback_loader,
        async_playback_loader=request.async_playback_loader,
        use_local_history=False,
    )
    first = session.playlist[0]

    assert request.vod.vod_pic == "poster-detail"
    assert session.playback_loader is not None

    session.playback_loader(first)

    assert session.video_cover_override == ""
    assert request.vod.vod_pic == "poster-detail"
    assert first.url == "https://stream.example/play/1.m3u8"


def test_controller_reuses_last_known_item_cover_when_player_content_cover_is_missing_on_revisit() -> None:
    controller = SpiderPluginController(FlakyCoverPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("/detail/1")
    session = PlayerController(_SessionApiClient()).create_session(
        request.vod,
        request.playlist,
        request.clicked_index,
        playlists=request.playlists,
        playlist_index=request.playlist_index,
        playback_loader=request.playback_loader,
        async_playback_loader=request.async_playback_loader,
        use_local_history=False,
    )
    first = session.playlist[0]
    second = session.playlist[1]

    assert session.playback_loader is not None

    session.playback_loader(first)
    assert session.video_cover_override == "https://img.example/song-1.jpg"

    session.playback_loader(second)
    assert session.video_cover_override == "https://img.example/song-2.jpg"

    session.playback_loader(first)

    assert session.video_cover_override == "https://img.example/song-1.jpg"
    assert request.vod.vod_pic == "poster-detail"
    assert first.url == "https://stream.example/play/1.m3u8"


def test_controller_raises_when_parse_required_without_parser_service() -> None:
    controller = SpiderPluginController(ParseRequiredSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("/detail/1")

    with pytest.raises(ValueError, match="当前插件未配置内置解析"):
        assert request.playback_loader is not None
        request.playback_loader(request.playlist[0])


def test_controller_resolves_danmaku_when_spider_enables_plugin_level_danmaku() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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


def test_controller_populates_grouped_danmaku_candidates_on_successful_search() -> None:
    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>'

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    request = controller.build_request("/detail/1")
    item = request.playlist[0]
    request.playback_loader(item)
    _wait_until(lambda: item.danmaku_xml != "")

    assert item.selected_danmaku_provider == "tencent"
    assert item.selected_danmaku_url == "https://v.qq.com/demo"
    assert item.danmaku_search_query == "红果短剧 1集"
    assert len(item.danmaku_candidates) == 1


def test_controller_auto_resolve_danmaku_searches_and_downloads_default_source() -> None:
    """auto_resolve_danmaku is the player window's generic auto-load fallback for
    plugin sources (the path taken when opening from playback history, where the
    loader-based _maybe_resolve_danmaku does not run for the resumed episode). It
    must search sources and download the default, mirroring the generic controller."""

    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="bilibili",
                        provider_label="B站",
                        options=[
                            DanmakuSourceOption(
                                provider="bilibili",
                                name="凡人修仙传 第127话",
                                url="https://www.bilibili.com/bangumi/127",
                            )
                        ],
                    )
                ],
                default_option_url="https://www.bilibili.com/bangumi/127",
                default_provider="bilibili",
            )

        def resolve_danmu(self, page_url: str) -> str:
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>'

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="网盘资源",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(
        title="第127集 外海风云3",
        url="https://stream.example/play/127.m3u8",
        media_title="凡人修仙传",
        vod_id="/play/127",
    )

    resolved = controller.auto_resolve_danmaku(item)

    assert resolved is True
    assert item.selected_danmaku_url == "https://www.bilibili.com/bangumi/127"
    assert item.danmaku_xml != ""
    assert item.danmaku_search_query == "凡人修仙传 127集"


def test_controller_research_danmaku_uses_temporary_query_only_for_current_item() -> None:
    calls: list[str] = []

    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ):
            calls.append(name)
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="红果短剧")

    controller.refresh_danmaku_sources(item, query_override="红果短剧 腾讯版")

    assert item.danmaku_search_query == "红果短剧 腾讯版"
    assert item.danmaku_search_query_overridden is True
    assert calls[-1] == "红果短剧 腾讯版"


def test_controller_refresh_danmaku_sources_passes_temporary_provider_filter_only_to_search() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ):
            calls.append((name, provider_filter))
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="youku",
                        provider_label="优酷",
                        options=[DanmakuSourceOption(provider="youku", name="候选", url="https://v.youku.com/demo")],
                    )
                ],
                default_option_url="https://v.youku.com/demo",
                default_provider="youku",
            )

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="红果短剧")

    controller.refresh_danmaku_sources(item, force_refresh=True, provider_filter="youku")

    assert calls == [("红果短剧 1集", "youku")]
    assert item.selected_danmaku_provider == "youku"
    assert item.danmaku_search_provider == "youku"


def test_controller_refresh_danmaku_sources_retries_title_only_for_manual_episode_search_with_provider_filter() -> None:
    calls: list[tuple[str, str]] = []
    title_only_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="iqiyi",
                provider_label="爱奇艺",
                options=[
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="成何体统 第1集 认亲了",
                        url="https://www.iqiyi.com/v_ep1.html",
                    )
                ],
            )
        ],
        default_option_url="https://www.iqiyi.com/v_ep1.html",
        default_provider="iqiyi",
    )

    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ):
            calls.append((name, provider_filter))
            if name == "成何体统 1集":
                return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")
            if name == "成何体统":
                return title_only_result
            raise AssertionError(f"Unexpected query: {name}")

        def rerank_danmaku_source_search_result(self, result, **kwargs):
            assert kwargs["query_name"] == "成何体统 1集"
            assert kwargs["reg_src"] == "http://m/1.mp4"
            return result

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="成何体统",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(title="01-4K.mp4", url="http://m/1.mp4", media_title="成何体统")

    controller.refresh_danmaku_sources(
        item,
        search_title_override="成何体统",
        search_episode_override="1集",
        force_refresh=True,
        provider_filter="iqiyi",
    )

    assert calls == [("成何体统 1集", "iqiyi"), ("成何体统", "iqiyi")]
    assert item.selected_danmaku_provider == "iqiyi"
    assert item.selected_danmaku_url == "https://www.iqiyi.com/v_ep1.html"
    assert item.danmaku_search_provider == "iqiyi"


def test_controller_refresh_danmaku_sources_uses_saved_search_title_for_same_series(tmp_path) -> None:
    class FakeDanmakuService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            self.calls.append(name)
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="候选", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    series_key = build_danmaku_series_key("玄界之门")
    store.save(
        controller_module.DanmakuSeriesPreference(
            series_key=series_key,
            provider="tencent",
            page_url="https://v.qq.com/old",
            title="旧标题",
            search_title="玄界之门 特别版",
            updated_at=1,
        )
    )
    service = FakeDanmakuService()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=service,
        danmaku_preference_store=store,
    )
    item = PlayItem(title="第2集", url="https://stream.example/2.m3u8", media_title="玄界之门", vod_id="2")

    controller.refresh_danmaku_sources(item)

    assert service.calls == ["玄界之门 特别版 2集"]
    assert item.danmaku_search_title == "玄界之门 特别版"
    assert item.danmaku_search_episode == "2集"
    assert item.danmaku_search_query == "玄界之门 特别版 2集"


def test_controller_refresh_danmaku_sources_ignores_saved_search_title_when_default_title_has_season_but_saved_title_does_not(
    tmp_path,
) -> None:
    class FakeDanmakuService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            self.calls.append(name)
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="iqiyi",
                        provider_label="爱奇艺",
                        options=[DanmakuSourceOption(provider="iqiyi", name="候选", url="https://www.iqiyi.com/v_demo.html")],
                    )
                ],
                default_option_url="https://www.iqiyi.com/v_demo.html",
                default_provider="iqiyi",
            )

    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    series_key = build_danmaku_series_key("成何体统第二季")
    store.save(
        controller_module.DanmakuSeriesPreference(
            series_key=series_key,
            provider="iqiyi",
            page_url="https://www.iqiyi.com/v_old.html",
            title="旧标题",
            search_title="成何体统",
            updated_at=1,
        )
    )
    service = FakeDanmakuService()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="成何体统第二季",
        search_enabled=True,
        danmaku_service=service,
        danmaku_preference_store=store,
    )
    item = PlayItem(title="01-4K.mp4", url="https://stream.example/1.m3u8", media_title="成何体统第二季", vod_id="1")

    controller.refresh_danmaku_sources(item)

    assert service.calls == ["成何体统第二季 1集"]
    assert item.danmaku_search_title == "成何体统第二季"
    assert item.danmaku_search_query == "成何体统第二季 1集"


def test_controller_refresh_danmaku_sources_strips_trailing_year_from_default_media_title() -> None:
    class FakeDanmakuService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            self.calls.append(name)
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="候选", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

    service = FakeDanmakuService()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="黑夜告白",
        search_enabled=True,
        danmaku_service=service,
    )
    item = PlayItem(title="正片", url="https://stream.example/movie.m3u8", media_title="黑夜告白 (2026)", vod_id="movie-1")

    controller.refresh_danmaku_sources(item)

    assert service.calls == ["黑夜告白"]
    assert item.danmaku_search_title == "黑夜告白"
    assert item.danmaku_search_episode == ""
    assert item.danmaku_search_query == "黑夜告白"


def test_controller_refresh_danmaku_sources_strips_trailing_size_suffix_from_calendar_episode_title() -> None:
    class FakeDanmakuService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            self.calls.append(name)
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="mgtv",
                        provider_label="芒果",
                        options=[DanmakuSourceOption(provider="mgtv", name="候选", url="https://www.mgtv.com/b/1/2.html")],
                    )
                ],
                default_option_url="https://www.mgtv.com/b/1/2.html",
                default_provider="mgtv",
            )

    service = FakeDanmakuService()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="你好星期六",
        search_enabled=True,
        danmaku_service=service,
    )
    item = PlayItem(
        title="20250104期(1.74 GB)",
        url="https://stream.example/1.m3u8",
        media_title="你好星期六",
        vod_id="episode-1",
    )

    controller.refresh_danmaku_sources(item)

    assert service.calls == ["你好星期六 20250104期"]
    assert item.danmaku_search_title == "你好星期六"
    assert item.danmaku_search_episode == "20250104期"
    assert item.danmaku_search_query == "你好星期六 20250104期"


def test_controller_refresh_danmaku_sources_strips_trailing_size_suffix_from_default_media_title() -> None:
    class FakeDanmakuService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            self.calls.append(name)
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="候选", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

    service = FakeDanmakuService()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="玄界之门",
        search_enabled=True,
        danmaku_service=service,
    )
    item = PlayItem(
        title="第12集",
        url="https://stream.example/12.m3u8",
        media_title="玄界之门 特别版(894.9 MB)",
        vod_id="episode-12",
    )

    controller.refresh_danmaku_sources(item)

    assert service.calls == ["玄界之门 特别版 12集"]
    assert item.danmaku_search_title == "玄界之门 特别版"
    assert item.danmaku_search_episode == "12集"
    assert item.danmaku_search_query == "玄界之门 特别版 12集"


def test_controller_refresh_danmaku_sources_retries_title_only_for_auto_inferred_file_episode() -> None:
    class FakeDanmakuService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            self.calls.append(name)
            if name == "灵武大陆 100集":
                return DanmakuSourceSearchResult(groups=[])
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="iqiyi",
                        provider_label="爱奇艺",
                        options=[
                            DanmakuSourceOption(
                                provider="iqiyi",
                                name="灵武大陆",
                                url="https://www.iqiyi.com/v_demo.html",
                            )
                        ],
                    )
                ],
                default_option_url="https://www.iqiyi.com/v_demo.html",
                default_provider="iqiyi",
            )

    service = FakeDanmakuService()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="灵武大陆",
        search_enabled=True,
        danmaku_service=service,
    )
    item = PlayItem(
        title="100.mp4",
        url="http://m/100.mp4",
        media_title="灵武大陆",
        vod_id="http://m/100.mp4",
    )

    controller.refresh_danmaku_sources(item, playlist=[item])

    assert service.calls == ["灵武大陆 100集", "灵武大陆"]
    assert item.danmaku_search_title == "灵武大陆"
    assert item.danmaku_search_episode == "100集"
    assert item.danmaku_search_query == "灵武大陆 100集"
    assert item.selected_danmaku_provider == "iqiyi"
    assert item.selected_danmaku_url == "https://www.iqiyi.com/v_demo.html"


def test_controller_resolve_danmaku_sync_reranks_title_only_retry_results_back_to_requested_episode(
    tmp_path,
) -> None:
    class FakeDanmakuService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.resolve_calls: list[str] = []
            self._reranker = DanmakuService({}, provider_order=[])

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            self.calls.append((name, preferred_page_url))
            if name == "低智商犯罪 9集":
                return DanmakuSourceSearchResult(groups=[])
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="iqiyi",
                        provider_label="爱奇艺",
                        options=[
                            DanmakuSourceOption(
                                provider="iqiyi",
                                name="低智商犯罪第1集",
                                url="https://www.iqiyi.com/v_ep1.html",
                            ),
                            DanmakuSourceOption(
                                provider="iqiyi",
                                name="低智商犯罪第9集",
                                url="https://www.iqiyi.com/v_ep9.html",
                            ),
                        ],
                    )
                ],
                default_option_url="https://www.iqiyi.com/v_ep1.html",
                default_provider="iqiyi",
            )

        def rerank_danmaku_source_search_result(self, result, **kwargs):
            return self._reranker.rerank_danmaku_source_search_result(result, **kwargs)

        def resolve_danmu(self, page_url: str, option=None) -> str:
            self.resolve_calls.append(page_url)
            return f'<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">{page_url}</d></i>'

    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    store.save(
        DanmakuSeriesPreference(
            series_key=build_danmaku_series_key("低智商犯罪"),
            provider="iqiyi",
            page_url="https://www.iqiyi.com/v_ep1.html",
            title="低智商犯罪第1集",
            search_title="低智商犯罪",
            updated_at=0,
        )
    )
    service = FakeDanmakuService()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="低智商犯罪",
        search_enabled=True,
        danmaku_service=service,
        danmaku_preference_store=store,
    )
    item = PlayItem(
        title="09.mp4",
        url="http://m/09.mp4",
        media_title="低智商犯罪",
        vod_id="episode-09",
    )

    controller._resolve_danmaku_sync(item, item.url, [item])

    assert service.calls == [
        ("低智商犯罪 9集", "https://www.iqiyi.com/v_ep1.html"),
        ("低智商犯罪", "https://www.iqiyi.com/v_ep1.html"),
    ]
    assert item.selected_danmaku_provider == "iqiyi"
    assert item.selected_danmaku_url == "https://www.iqiyi.com/v_ep9.html"
    assert item.selected_danmaku_title == "低智商犯罪第9集"
    assert service.resolve_calls == ["https://www.iqiyi.com/v_ep9.html"]


def test_controller_refresh_danmaku_sources_omits_episode_for_movie_like_title() -> None:
    class FakeDanmakuService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            self.calls.append(name)
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="候选", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

    service = FakeDanmakuService()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="名侦探柯南",
        search_enabled=True,
        danmaku_service=service,
    )
    item = PlayItem(
        title="名侦探柯南 剧场版",
        url="https://stream.example/movie.m3u8",
        media_title="名侦探柯南 剧场版",
        vod_id="movie-1",
    )

    controller.refresh_danmaku_sources(item)

    assert service.calls == ["名侦探柯南 剧场版"]
    assert item.danmaku_search_episode == ""
    assert item.danmaku_search_query == "名侦探柯南 剧场版"


def test_controller_refresh_danmaku_sources_omits_episode_for_movie_category() -> None:
    class FakeDanmakuService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            self.calls.append(name)
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="候选", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

    service = FakeDanmakuService()
    controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="孤注一掷",
        search_enabled=True,
        danmaku_service=service,
    )
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="孤注一掷",
        category_name="多多电影",
    )

    controller.refresh_danmaku_sources(item, playlist=[item])

    assert service.calls == ["孤注一掷"]
    assert item.danmaku_search_episode == ""
    assert item.danmaku_search_query == "孤注一掷"


def test_controller_refresh_danmaku_sources_keeps_part_marker_for_movie_category() -> None:
    # 电影分上中下:即使分类是电影,"上集/中集/下集"分部标记也是集数锚点,
    # 不能按电影分类省略,否则三个分段的弹幕无从区分(regression: 曾统一省略)。
    class FakeDanmakuService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            self.calls.append(name)
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="候选", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

    service = FakeDanmakuService()
    controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="诡邻",
        search_enabled=True,
        danmaku_service=service,
    )
    titles = ("上集", "中集", "下集")
    playlist = [
        PlayItem(
            title=title,
            url=f"https://stream.example/{index + 1}.m3u8",
            media_title="诡邻",
            category_name="多多电影",
        )
        for index, title in enumerate(titles)
    ]

    controller.refresh_danmaku_sources(playlist[2], playlist=playlist)

    assert service.calls == ["诡邻 3集"]
    assert playlist[2].danmaku_search_episode == "3集"
    assert playlist[2].danmaku_search_query == "诡邻 3集"


def test_controller_preserves_movie_category_for_drive_replacement_items() -> None:
    class MovieDriveSpider(FakeSpider):
        def danmaku(self):
            return True

        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "伪钞重案",
                        "vod_play_from": "网盘线",
                        "vod_play_url": "正片$https://pan.baidu.com/s/fake-movie",
                    }
                ]
            }

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            calls.append((name, reg_src))
            return []

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.baidu.com/s/fake-movie"
        return {
            "list": [
                {
                    "vod_id": "1$112392$1",
                    "vod_name": "百度资源",
                    "items": [
                        {
                            "title": "4k.mp4(2.45 GB)",
                            "url": "http://192.168.50.60:4567/p/web/1@112392",
                            "path": "/伪钞重案/4k.mp4",
                            "size": 2450000000,
                        }
                    ],
                }
            ]
        }

    controller = SpiderPluginController(
        MovieDriveSpider(),
        plugin_name="伪钞重案",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/movie-drive")
    request.vod.category_name = "多多电影"
    request.playlist[0].category_name = "多多电影"

    assert request.playback_loader is not None
    result = request.playback_loader(request.playlist[0])

    assert result is not None
    replacement = result.replacement_playlist[0]
    _wait_until(lambda: replacement.danmaku_pending is False)

    assert replacement.category_name == "多多电影"
    assert calls == [("伪钞重案", "https://pan.baidu.com/s/fake-movie")]
    assert replacement.danmaku_search_episode == ""
    assert replacement.danmaku_search_query == "伪钞重案"


def test_controller_refresh_danmaku_sources_persists_search_title_only_after_successful_search(tmp_path) -> None:
    class SuccessfulDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="候选", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

    class FailingDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            raise RuntimeError("boom")

    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    series_key = build_danmaku_series_key("玄界之门")
    success_controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=SuccessfulDanmakuService(),
        danmaku_preference_store=store,
    )
    success_item = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="玄界之门", vod_id="1")

    success_controller.refresh_danmaku_sources(
        success_item,
        search_title_override="玄界之门 特别版",
        search_episode_override="1集",
        force_refresh=True,
    )

    saved = store.load(series_key)
    assert saved is not None
    assert saved.search_title == "玄界之门 特别版"
    assert saved.provider == ""
    assert saved.page_url == ""

    failing_controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=FailingDanmakuService(),
        danmaku_preference_store=store,
    )
    failing_item = PlayItem(title="第2集", url="https://stream.example/2.m3u8", media_title="玄界之门", vod_id="2")

    with pytest.raises(RuntimeError, match="boom"):
        failing_controller.refresh_danmaku_sources(
            failing_item,
            search_title_override="失败标题",
            search_episode_override="2集",
            force_refresh=True,
        )

    assert store.load(series_key).search_title == "玄界之门 特别版"


def test_controller_switch_danmaku_source_persists_search_title(tmp_path) -> None:
    class FakeDanmakuService:
        def resolve_danmu(self, page_url: str) -> str:
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>'

    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    series_key = build_danmaku_series_key("玄界之门")
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
        danmaku_preference_store=store,
    )
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="玄界之门",
        vod_id="1",
        danmaku_series_key=series_key,
        danmaku_search_title="玄界之门 特别版",
        danmaku_search_episode="1集",
        danmaku_search_query="玄界之门 特别版 1集",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
            )
        ],
    )

    controller.switch_danmaku_source(item, "https://v.qq.com/demo")

    saved = store.load(series_key)
    assert saved is not None
    assert saved.search_title == "玄界之门 特别版"
    assert saved.provider == "tencent"
    assert saved.page_url == "https://v.qq.com/demo"


def test_controller_refresh_danmaku_sources_emits_log_events() -> None:
    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第2集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

    logs: list[str] = []
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    controller.set_danmaku_log_handler(logs.append)
    item = PlayItem(title="第2集", url="https://stream.example/2.m3u8", media_title="红果短剧", vod_id="2")

    controller.refresh_danmaku_sources(item, force_refresh=True)

    assert logs == [
        "弹幕搜索中: 红果短剧 2集",
        "弹幕搜索成功: 找到 1 个候选",
    ]


def test_controller_switch_danmaku_source_emits_log_events() -> None:
    class FakeDanmakuService:
        def resolve_danmu(self, page_url: str) -> str:
            assert page_url == "https://v.qq.com/demo"
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>'

    logs: list[str] = []
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    controller.set_danmaku_log_handler(logs.append)
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        vod_id="1",
        danmaku_search_query="红果短剧 1集",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
            )
        ],
    )

    controller.switch_danmaku_source(item, "https://v.qq.com/demo")

    assert logs == [
        "弹幕下载中: 腾讯 - 红果短剧 第1集",
        "弹幕下载成功: 1集 (1 条弹幕)",
    ]


def test_controller_resolve_danmaku_sync_emits_final_failure_after_all_candidates_fail() -> None:
    class FailingDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="bilibili",
                        provider_label="B站",
                        options=[DanmakuSourceOption(provider="bilibili", name="世界的主人", url="https://bilibili/1")],
                    ),
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="世界的主人", url="https://qq/1")],
                    ),
                ],
                default_option_url="https://bilibili/1",
                default_provider="bilibili",
            )

        def resolve_danmu(self, page_url: str, option=None) -> str:
            raise DanmakuEmptyResultError(f"未找到弹幕: {page_url}")

    logs: list[str] = []
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="世界的主人",
        search_enabled=True,
        danmaku_service=FailingDanmakuService(),
    )
    controller.set_danmaku_log_handler(logs.append)
    item = PlayItem(title="正片", url="https://stream.example/movie.m3u8", media_title="世界的主人", vod_id="movie-1")

    controller._resolve_danmaku_sync(item, item.url, [item])

    assert logs == [
        "弹幕搜索中: 世界的主人",
        "弹幕搜索成功: 找到 2 个候选",
        "弹幕下载中: B站 - 世界的主人",
        "弹幕下载中: 腾讯 - 世界的主人",
        "弹幕下载失败: 未找到弹幕: https://qq/1",
    ]


def test_controller_uses_cached_danmaku_source_search_result_without_network_lookup(monkeypatch) -> None:
    calls: list[str] = []
    cached_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
            )
        ],
        default_option_url="https://v.qq.com/demo",
        default_provider="tencent",
    )

    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            calls.append(name)
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

    monkeypatch.setattr(controller_module, "load_cached_danmaku_source_search_result", lambda name, reg_src: cached_result)

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="红果短剧")

    controller.refresh_danmaku_sources(item)

    assert calls == []
    assert item.danmaku_search_query == "红果短剧 1集"
    assert item.selected_danmaku_provider == "tencent"
    assert item.selected_danmaku_url == "https://v.qq.com/demo"
    assert item.danmaku_candidates == cached_result.groups


def test_controller_refresh_danmaku_sources_can_bypass_cached_search_result(monkeypatch) -> None:
    calls: list[str] = []
    cached_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="缓存结果", url="https://v.qq.com/cached")],
            )
        ],
        default_option_url="https://v.qq.com/cached",
        default_provider="tencent",
    )
    fresh_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="bilibili",
                provider_label="B站",
                options=[DanmakuSourceOption(provider="bilibili", name="新结果", url="https://www.bilibili.com/video/BV1x")],
            )
        ],
        default_option_url="https://www.bilibili.com/video/BV1x",
        default_provider="bilibili",
    )

    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            calls.append(name)
            return fresh_result

    monkeypatch.setattr(controller_module, "load_cached_danmaku_source_search_result", lambda name, reg_src: cached_result)

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="红果短剧")

    controller.refresh_danmaku_sources(item, force_refresh=True)

    assert calls == ["红果短剧 1集"]
    assert item.selected_danmaku_provider == "bilibili"
    assert item.selected_danmaku_url == "https://www.bilibili.com/video/BV1x"
    assert item.danmaku_candidates == fresh_result.groups


def test_controller_refresh_danmaku_sources_ignores_stale_cached_search_result_without_matching_episode(
    monkeypatch,
) -> None:
    calls: list[str] = []
    cached_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="youku",
                provider_label="优酷",
                options=[DanmakuSourceOption(provider="youku", name="《成何体统》剧情揭秘 第6集", url="https://v.youku.com/v_show/id_noise.html")],
            )
        ],
        default_option_url="https://v.youku.com/v_show/id_noise.html",
        default_provider="youku",
    )
    fresh_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="iqiyi",
                provider_label="爱奇艺",
                options=[DanmakuSourceOption(provider="iqiyi", name="成何体统 第1集", url="https://www.iqiyi.com/v_real_ep1.html")],
            )
        ],
        default_option_url="https://www.iqiyi.com/v_real_ep1.html",
        default_provider="iqiyi",
    )

    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            calls.append(name)
            return fresh_result

        def rerank_danmaku_source_search_result(self, result, **kwargs):
            return result

    monkeypatch.setattr(controller_module, "load_cached_danmaku_source_search_result", lambda name, reg_src: cached_result)

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="成何体统",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(title="01-4K.mp4", url="https://stream.example/1.m3u8", media_title="成何体统")

    controller.refresh_danmaku_sources(item)

    assert calls == ["成何体统 1集"]
    assert item.selected_danmaku_provider == "iqiyi"
    assert item.selected_danmaku_url == "https://www.iqiyi.com/v_real_ep1.html"
    assert item.danmaku_candidates == fresh_result.groups


def test_controller_reuses_cached_search_result_across_different_plugin_sources(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )

    class FirstDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

    class SecondDanmakuService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            self.calls.append(name)
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

    first_controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="插件A",
        search_enabled=True,
        danmaku_service=FirstDanmakuService(),
    )
    first_item = PlayItem(title="第1集", url="https://stream.example/a.m3u8", media_title="玄界之门", vod_id="/play/a")

    first_controller.refresh_danmaku_sources(first_item, query_override="玄界之门 1集", force_refresh=True)

    second_service = SecondDanmakuService()
    second_controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="插件B",
        search_enabled=True,
        danmaku_service=second_service,
    )
    second_item = PlayItem(title="第1集", url="https://stream.example/b.m3u8", media_title="玄界之门", vod_id="/play/b")

    second_controller.refresh_danmaku_sources(second_item, query_override="玄界之门 1集")

    assert second_service.calls == []
    assert second_item.selected_danmaku_url == "https://v.qq.com/demo"


def test_controller_reuses_cached_danmaku_xml_across_different_plugin_sources(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)

    class FirstDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            assert page_url == "https://v.qq.com/demo"
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">缓存弹幕</d></i>'

    class SecondDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            raise AssertionError("should reuse cached danmaku xml")

    first_controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="插件A",
        search_enabled=True,
        danmaku_service=FirstDanmakuService(),
    )
    first_item = PlayItem(title="第1集", url="https://stream.example/a.m3u8", media_title="玄界之门", vod_id="/play/a")
    first_controller.refresh_danmaku_sources(first_item, query_override="玄界之门 1集", force_refresh=True)
    first_controller.switch_danmaku_source(first_item, "https://v.qq.com/demo")

    second_controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="插件B",
        search_enabled=True,
        danmaku_service=SecondDanmakuService(),
    )
    second_item = PlayItem(title="第1集", url="https://stream.example/b.m3u8", media_title="玄界之门", vod_id="/play/b")
    second_controller.refresh_danmaku_sources(second_item, query_override="玄界之门 1集", force_refresh=True)

    xml_text = second_controller.switch_danmaku_source(second_item, "https://v.qq.com/demo")

    assert "缓存弹幕" in xml_text


def test_controller_ignores_stale_preference_cached_danmaku_xml_for_wrong_episode(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)

    stale_xml = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">第1集缓存</d></i>'
    danmaku_cache_module.save_cached_danmaku_xml("低智商犯罪 4集", "https://www.iqiyi.com/v_ep1.html", stale_xml)

    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    store.save(
        DanmakuSeriesPreference(
            series_key=build_danmaku_series_key("低智商犯罪"),
            provider="iqiyi",
            page_url="https://www.iqiyi.com/v_ep1.html",
            title="低智商犯罪第1集",
            search_title="低智商犯罪",
            updated_at=0,
        )
    )

    class FakeDanmakuService:
        def __init__(self) -> None:
            self.search_calls: list[str] = []
            self.resolve_calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ):
            self.search_calls.append(name)
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="iqiyi",
                        provider_label="爱奇艺",
                        options=[DanmakuSourceOption(provider="iqiyi", name="低智商犯罪第4集", url="https://www.iqiyi.com/v_ep4.html")],
                    )
                ],
                default_option_url="https://www.iqiyi.com/v_ep4.html",
                default_provider="iqiyi",
            )

        def resolve_danmu(self, page_url: str, option=None) -> str:
            self.resolve_calls.append(page_url)
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">第4集实时</d></i>'

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="低智商犯罪",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
        danmaku_preference_store=store,
    )
    item = PlayItem(
        title="04.mp4",
        url="http://m/04.mp4",
        media_title="低智商犯罪",
        vod_id="episode-04",
        danmaku_search_title="低智商犯罪",
        danmaku_search_episode="4集",
    )

    controller._resolve_danmaku_sync(item, item.url, [item])

    assert "第4集实时" in item.danmaku_xml
    assert item.selected_danmaku_url == "https://www.iqiyi.com/v_ep4.html"


def test_controller_ignores_stale_cached_candidate_xml_for_wrong_episode(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )

    cached_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="iqiyi",
                provider_label="爱奇艺",
                options=[
                    DanmakuSourceOption(provider="iqiyi", name="低智商犯罪第1集", url="https://www.iqiyi.com/v_ep1.html"),
                    DanmakuSourceOption(provider="iqiyi", name="低智商犯罪第4集", url="https://www.iqiyi.com/v_ep4.html"),
                ],
            )
        ],
        default_option_url="https://www.iqiyi.com/v_ep1.html",
        default_provider="iqiyi",
    )
    danmaku_cache_module.save_cached_danmaku_source_search_result("低智商犯罪 4集", "", cached_result)
    danmaku_cache_module.save_cached_danmaku_xml(
        "低智商犯罪 4集",
        "https://www.iqiyi.com/v_ep1.html",
        '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">第1集缓存</d></i>',
    )

    class FakeDanmakuService:
        def __init__(self) -> None:
            self.search_calls: list[str] = []
            self.resolve_calls: list[str] = []

        def rerank_danmaku_source_search_result(self, result, **kwargs):
            return DanmakuService({}, provider_order=[]).rerank_danmaku_source_search_result(result, **kwargs)

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ):
            self.search_calls.append(name)
            return cached_result

        def resolve_danmu(self, page_url: str, option=None) -> str:
            self.resolve_calls.append(page_url)
            assert page_url == "https://www.iqiyi.com/v_ep4.html"
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">第4集实时</d></i>'

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="低智商犯罪",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(
        title="04.mp4",
        url="http://m/04.mp4",
        media_title="低智商犯罪",
        vod_id="episode-04",
        danmaku_search_title="低智商犯罪",
        danmaku_search_episode="4集",
    )

    controller._resolve_danmaku_sync(item, item.url, [item])

    assert "第4集实时" in item.danmaku_xml
    assert item.selected_danmaku_url == "https://www.iqiyi.com/v_ep4.html"


def test_controller_refresh_danmaku_sources_restores_override_result_cache_after_restart(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    search_calls: list[str] = []
    result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
            )
        ],
        default_option_url="https://v.qq.com/demo",
        default_provider="tencent",
    )

    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            search_calls.append(name)
            return result

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    first = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="玄界之门3D版")

    controller.refresh_danmaku_sources(first, query_override="玄界之门 1集", force_refresh=True)

    restarted = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="玄界之门3D版")

    assert controller.load_cached_danmaku_sources(restarted) is True
    assert search_calls == ["玄界之门 1集"]
    assert restarted.selected_danmaku_provider == "tencent"
    assert restarted.selected_danmaku_url == "https://v.qq.com/demo"
    assert restarted.danmaku_candidates == result.groups


def test_controller_can_restore_cached_iqiyi_source_after_restart(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )

    segment = zlib.compress(
        (
            "<root>"
            "<bulletInfoList>"
            "<bulletInfo><showTime>1500</showTime><content>重启后恢复成功</content><color>255</color></bulletInfo>"
            "</bulletInfoList>"
            "</root>"
        ).encode("utf-8")
    )

    def fake_get(url: str, **kwargs):
        if "mesh.if.iqiyi.com/portal/lw/search/homePageV3" in url:
            return JsonResponse(
                {
                    "data": {
                        "templates": [
                            {
                                "albumInfo": {
                                    "title": "八千里路云和月",
                                    "channel": "电视剧,2",
                                    "siteId": "iqiyi",
                                    "siteName": "爱奇艺",
                                    "qipuId": 6421036798758301,
                                    "videos": [
                                        {
                                            "title": "八千里路云和月第10集",
                                            "qipuId": 123456789000,
                                            "pageUrl": "http://www.iqiyi.com/v_20imo31bths.html",
                                            "duration": 300000,
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                }
            )
        if url == "https://www.iqiyi.com/v_20imo31bths.html":
            return JsonResponse(text="<html><head><title>shell page</title></head><body></body></html>")
        if url.endswith("123456789000_300_1.z"):
            return JsonResponse(content=segment)
        raise AssertionError(f"Unexpected URL: {url}")

    first_controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="八千里路云和月",
        search_enabled=True,
        danmaku_service=DanmakuService({"iqiyi": IqiyiDanmakuProvider(get=fake_get)}, provider_order=["iqiyi"]),
    )
    first_item = PlayItem(title="第10集", url="https://stream.example/10.m3u8", media_title="八千里路云和月")

    first_controller.refresh_danmaku_sources(first_item, query_override="八千里路云和月 第10集", force_refresh=True)

    restarted_controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="八千里路云和月",
        search_enabled=True,
        danmaku_service=DanmakuService({"iqiyi": IqiyiDanmakuProvider(get=fake_get)}, provider_order=["iqiyi"]),
    )
    restarted_item = PlayItem(title="第10集", url="https://stream.example/10.m3u8", media_title="八千里路云和月")

    assert restarted_controller.load_cached_danmaku_sources(restarted_item) is True
    xml_text = restarted_controller.switch_danmaku_source(restarted_item, restarted_item.selected_danmaku_url)

    assert "重启后恢复成功" in xml_text
    assert restarted_item.selected_danmaku_provider == "iqiyi"
    assert restarted_item.selected_danmaku_url == "https://www.iqiyi.com/v_20imo31bths.html"


def test_controller_passes_playitem_duration_to_search_danmu_sources() -> None:
    calls: list[int] = []

    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            calls.append(media_duration_seconds)
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="红果短剧", duration_seconds=1240)

    controller.refresh_danmaku_sources(item, force_refresh=True)

    assert calls == [1240]


def test_controller_reranks_cached_danmaku_source_results_by_media_duration(monkeypatch) -> None:
    cached_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[
                    DanmakuSourceOption(provider="tencent", name="遮天 88集", url="https://v.qq.com/long", duration_seconds=1560),
                    DanmakuSourceOption(provider="tencent", name="遮天 88集", url="https://v.qq.com/best", duration_seconds=1242),
                ],
            )
        ],
        default_option_url="https://v.qq.com/long",
        default_provider="tencent",
    )

    class FakeDanmakuService:
        def rerank_danmaku_source_search_result(self, result, **kwargs):
            assert result == cached_result
            assert kwargs["media_duration_seconds"] == 1240
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[
                            DanmakuSourceOption(provider="tencent", name="遮天 88集", url="https://v.qq.com/best", duration_seconds=1242),
                            DanmakuSourceOption(provider="tencent", name="遮天 88集", url="https://v.qq.com/long", duration_seconds=1560),
                        ],
                    )
                ],
                default_option_url="https://v.qq.com/best",
                default_provider="tencent",
            )

    monkeypatch.setattr(controller_module, "load_cached_danmaku_source_search_result", lambda name, reg_src: cached_result)

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="红果短剧", duration_seconds=1240)

    controller.refresh_danmaku_sources(item)

    assert item.selected_danmaku_provider == "tencent"
    assert item.selected_danmaku_url == "https://v.qq.com/best"
    assert [option.url for option in item.danmaku_candidates[0].options] == [
        "https://v.qq.com/best",
        "https://v.qq.com/long",
    ]


def test_controller_load_cached_danmaku_sources_prefers_matching_episode_over_historical_page_url(
    monkeypatch,
    tmp_path,
) -> None:
    cached_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="iqiyi",
                provider_label="爱奇艺",
                options=[
                    DanmakuSourceOption(provider="iqiyi", name="低智商犯罪第1集", url="https://www.iqiyi.com/v_ep1.html"),
                    DanmakuSourceOption(provider="iqiyi", name="低智商犯罪第8集", url="https://www.iqiyi.com/v_ep8.html"),
                ],
            )
        ],
        default_option_url="https://www.iqiyi.com/v_ep1.html",
        default_provider="iqiyi",
    )

    class FakeDanmakuService:
        def __init__(self) -> None:
            self._reranker = DanmakuService({}, provider_order=[])

        def rerank_danmaku_source_search_result(self, result, **kwargs):
            assert result == cached_result
            assert kwargs["query_name"] == "低智商犯罪 8集"
            return self._reranker.rerank_danmaku_source_search_result(result, **kwargs)

    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    store.save(
        DanmakuSeriesPreference(
            series_key=build_danmaku_series_key("低智商犯罪"),
            provider="iqiyi",
            page_url="https://www.iqiyi.com/v_ep1.html",
            title="低智商犯罪第1集",
            search_title="低智商犯罪",
            updated_at=0,
        )
    )
    monkeypatch.setattr(controller_module, "load_cached_danmaku_source_search_result", lambda name, reg_src: cached_result)

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="低智商犯罪",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
        danmaku_preference_store=store,
    )
    item = PlayItem(title="08.mp4", url="http://m/08.mp4", media_title="低智商犯罪", vod_id="episode-08")

    assert controller.load_cached_danmaku_sources(item, playlist=[item]) is True
    assert item.selected_danmaku_provider == "iqiyi"
    assert item.selected_danmaku_url == "https://www.iqiyi.com/v_ep8.html"


def test_controller_load_cached_danmaku_sources_reuses_series_level_episode_list_without_global_search(
    monkeypatch,
) -> None:
    series_cached_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="sohu",
                provider_label="搜狐",
                options=[
                    DanmakuSourceOption(provider="sohu", name="谁动了我的隐私 第1集", url="https://m.tv.sohu.com/v10234237.shtml"),
                    DanmakuSourceOption(provider="sohu", name="谁动了我的隐私 第4集", url="https://m.tv.sohu.com/v10234281.shtml"),
                ],
            )
        ],
        default_option_url="https://m.tv.sohu.com/v10234237.shtml",
        default_provider="sohu",
    )

    class FakeDanmakuService:
        def __init__(self) -> None:
            self._reranker = DanmakuService({}, provider_order=[])
            self.search_calls = 0

        def rerank_danmaku_source_search_result(self, result, **kwargs):
            assert result == series_cached_result
            assert kwargs["query_name"] == "谁动了我的隐私 4集"
            return self._reranker.rerank_danmaku_source_search_result(result, **kwargs)

        def search_danmu_sources(self, *args, **kwargs):
            self.search_calls += 1
            raise AssertionError("global danmaku search should not run when series-level episode list cache is available")

    def fake_load_cached(name: str, reg_src: str):
        if name == "谁动了我的隐私":
            return series_cached_result
        return None

    monkeypatch.setattr(controller_module, "load_cached_danmaku_source_search_result", fake_load_cached)

    service = FakeDanmakuService()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="谁动了我的隐私",
        search_enabled=True,
        danmaku_service=service,
    )
    item = PlayItem(title="04.mp4", url="http://m/04.mp4", media_title="谁动了我的隐私", vod_id="episode-04")

    assert controller.load_cached_danmaku_sources(item, playlist=[item]) is True
    assert service.search_calls == 0
    assert item.selected_danmaku_provider == "sohu"
    assert item.selected_danmaku_url == "https://m.tv.sohu.com/v10234281.shtml"


def test_controller_refresh_danmaku_sources_saves_series_level_episode_list_cache(
    monkeypatch,
    tmp_path,
) -> None:
    import atv_player.danmaku.cache as danmaku_cache_module

    monkeypatch.setattr(danmaku_cache_module, "danmaku_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )

    result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="sohu",
                provider_label="搜狐",
                options=[
                    DanmakuSourceOption(provider="sohu", name="谁动了我的隐私 第1集", url="https://m.tv.sohu.com/v10234237.shtml"),
                    DanmakuSourceOption(provider="sohu", name="谁动了我的隐私 第4集", url="https://m.tv.sohu.com/v10234281.shtml"),
                ],
            )
        ],
        default_option_url="https://m.tv.sohu.com/v10234237.shtml",
        default_provider="sohu",
    )

    class FakeDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ):
            return result

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="谁动了我的隐私",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    item = PlayItem(title="01.mp4", url="http://m/01.mp4", media_title="谁动了我的隐私", vod_id="episode-01")

    controller.refresh_danmaku_sources(item, force_refresh=True, playlist=[item])

    loaded = danmaku_cache_module.load_cached_danmaku_source_search_result("谁动了我的隐私", "")
    assert loaded is not None
    assert [option.url for option in loaded.groups[0].options] == [
        "https://m.tv.sohu.com/v10234237.shtml",
        "https://m.tv.sohu.com/v10234281.shtml",
    ]


def test_controller_warms_series_level_provider_cache_after_successful_resolution(
    monkeypatch,
    tmp_path,
) -> None:
    import atv_player.danmaku.cache as danmaku_cache_module

    monkeypatch.setattr(danmaku_cache_module, "danmaku_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )

    class LongSeriesDanmakuSpider(PluginLevelDanmakuSpider):
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "假面迷情",
                        "vod_pic": "poster-detail",
                        "vod_play_from": "默认线",
                        "vod_play_url": "#".join(f"第{index}集$/play/{index}" for index in range(1, 15)),
                    }
                ]
            }

    explicit_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="sohu",
                provider_label="搜狐",
                options=[
                    DanmakuSourceOption(provider="sohu", name="假面迷情 第1集", url="https://m.tv.sohu.com/v9545881.shtml"),
                ],
            )
        ],
        default_option_url="https://m.tv.sohu.com/v9545881.shtml",
        default_provider="sohu",
    )
    series_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="sohu",
                provider_label="搜狐",
                options=[
                    DanmakuSourceOption(provider="sohu", name="假面迷情 第1集", url="https://m.tv.sohu.com/v9545881.shtml"),
                    DanmakuSourceOption(provider="sohu", name="假面迷情 第13集", url="https://m.tv.sohu.com/v9545931.shtml"),
                ],
            )
        ],
        default_option_url="https://m.tv.sohu.com/v9545881.shtml",
        default_provider="sohu",
    )

    class FakeDanmakuService:
        def __init__(self) -> None:
            self._reranker = DanmakuService({}, provider_order=[])
            self.search_calls: list[tuple[str, str]] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ):
            self.search_calls.append((name, provider_filter))
            if name == "假面迷情 1集":
                assert provider_filter == ""
                return explicit_result
            if name == "假面迷情":
                assert provider_filter == "sohu"
                assert preferred_provider == "sohu"
                assert preferred_page_url == "https://m.tv.sohu.com/v9545881.shtml"
                return series_result
            raise AssertionError(f"unexpected danmaku search: {name=} {provider_filter=}")

        def rerank_danmaku_source_search_result(self, result, **kwargs):
            return self._reranker.rerank_danmaku_source_search_result(result, **kwargs)

        def resolve_danmu(self, page_url: str, option=None) -> str:
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">ok</d></i>'

    service = FakeDanmakuService()
    controller = SpiderPluginController(
        LongSeriesDanmakuSpider(),
        plugin_name="假面迷情",
        search_enabled=True,
        danmaku_service=service,
    )
    request = controller.build_request("/detail/series-1")
    first = request.playlist[0]
    thirteenth = request.playlist[12]

    controller._resolve_danmaku_sync(first, first.vod_id or first.url, request.playlist)

    assert first.selected_danmaku_provider == "sohu"
    assert first.selected_danmaku_url == "https://m.tv.sohu.com/v9545881.shtml"
    assert _count_danmaku_entries(first.danmaku_xml) == 1

    def warmed_series_cache_ready() -> bool:
        cached = danmaku_cache_module.load_cached_danmaku_source_search_result("假面迷情", "")
        if cached is None or not cached.groups:
            return False
        return any(option.url == "https://m.tv.sohu.com/v9545931.shtml" for option in cached.groups[0].options)

    _wait_until(warmed_series_cache_ready)

    assert service.search_calls == [
        ("假面迷情 1集", ""),
        ("假面迷情", "sohu"),
    ]
    assert controller.load_cached_danmaku_sources(thirteenth, playlist=request.playlist) is True
    assert service.search_calls == [
        ("假面迷情 1集", ""),
        ("假面迷情", "sohu"),
    ]
    assert thirteenth.selected_danmaku_provider == "sohu"
    assert thirteenth.selected_danmaku_url == "https://m.tv.sohu.com/v9545931.shtml"


def test_controller_invalidated_prefetch_discards_stale_result_and_success_log() -> None:
    started = threading.Event()
    release = threading.Event()
    logs: list[str] = []

    class BlockingDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            return [
                DanmakuSearchItem(
                    provider="tencent",
                    name="雨霖铃 第2集",
                    url="https://v.qq.com/x/cover/yll/ep2.html",
                )
            ]

        def resolve_danmu(self, page_url: str) -> str:
            assert page_url == "https://v.qq.com/x/cover/yll/ep2.html"
            started.set()
            assert release.wait(timeout=1)
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">prefetched</d></i>'

    class TwoEpisodeSpider(PluginLevelDanmakuSpider):
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "雨霖铃",
                        "vod_pic": "poster-detail",
                        "vod_play_from": "默认线",
                        "vod_play_url": "第1集$/play/1#第2集$/play/2",
                    }
                ]
            }

    controller = SpiderPluginController(
        TwoEpisodeSpider(),
        plugin_name="雨霖铃",
        search_enabled=True,
        danmaku_service=BlockingDanmakuService(),
    )
    controller.set_danmaku_log_handler(logs.append)
    request = controller.build_request("/detail/1")
    second = request.playlist[1]

    controller.prefetch_next_episode_danmaku(second, request.playlist)
    assert started.wait(timeout=1)

    controller.invalidate_running_danmaku_prefetches()
    release.set()

    _wait_until(lambda: second.danmaku_pending is False)

    assert second.danmaku_xml == ""
    assert all("弹幕预下载成功" not in message for message in logs)


def test_controller_reuses_inflight_series_search_across_episode_switches() -> None:
    search_started = threading.Event()
    release_search = threading.Event()

    shared_result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[
                    DanmakuSourceOption(provider="tencent", name="主角 3集", url="https://v.qq.com/x/cover/zj/ep3.html"),
                    DanmakuSourceOption(provider="tencent", name="主角 5集", url="https://v.qq.com/x/cover/zj/ep5.html"),
                ],
            )
        ],
        default_option_url="https://v.qq.com/x/cover/zj/ep3.html",
        default_provider="tencent",
    )

    class BlockingDanmakuService:
        def __init__(self) -> None:
            self._reranker = DanmakuService({}, provider_order=[])
            self.search_calls: list[str] = []
            self.resolve_calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ):
            self.search_calls.append(name)
            if len(self.search_calls) > 1:
                raise AssertionError("same-series concurrent episode lookups should reuse the inflight search result")
            search_started.set()
            assert release_search.wait(timeout=1)
            return shared_result

        def rerank_danmaku_source_search_result(self, result, **kwargs):
            return self._reranker.rerank_danmaku_source_search_result(result, **kwargs)

        def resolve_danmu(self, page_url: str, option=None) -> str:
            self.resolve_calls.append(page_url)
            return f'<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">{page_url}</d></i>'

    service = BlockingDanmakuService()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="主角",
        search_enabled=True,
        danmaku_service=service,
    )
    third = PlayItem(
        title="第3集",
        url="http://m/3.mp4",
        media_title="主角",
        vod_id="episode-03",
        danmaku_search_title="主角",
        danmaku_search_episode="3集",
    )
    fifth = PlayItem(
        title="第5集",
        url="http://m/5.mp4",
        media_title="主角",
        vod_id="episode-05",
        danmaku_search_title="主角",
        danmaku_search_episode="5集",
    )

    first_thread = threading.Thread(
        target=lambda: controller._resolve_danmaku_sync(third, third.url, [third, fifth]),
        daemon=True,
    )
    second_thread = threading.Thread(
        target=lambda: controller._resolve_danmaku_sync(fifth, fifth.url, [third, fifth]),
        daemon=True,
    )

    first_thread.start()
    assert search_started.wait(timeout=1)
    second_thread.start()
    time.sleep(0.05)
    release_search.set()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)

    assert service.search_calls == ["主角 3集"]
    assert third.selected_danmaku_url == "https://v.qq.com/x/cover/zj/ep3.html"
    assert fifth.selected_danmaku_url == "https://v.qq.com/x/cover/zj/ep5.html"
    assert service.resolve_calls == [
        "https://v.qq.com/x/cover/zj/ep3.html",
        "https://v.qq.com/x/cover/zj/ep5.html",
    ]


def test_controller_tries_next_danmaku_candidate_when_first_candidate_has_no_records() -> None:
    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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


class DanmakuContentShortDramaSpider(FakeSpider):
    """红果形态:短剧条目 + danmakuContent 直供 + 聚合开关关闭。"""

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "归途",
                    "vod_pic": "poster-detail",
                    "type_name": "短剧",
                    "vod_play_from": "红果线路",
                    "vod_play_url": "第1集$/play/1#第2集$/play/2#第3集$/play/3",
                }
            ]
        }

    def danmakuContent(self, flag, id):
        self.danmaku_content_calls = getattr(self, "danmaku_content_calls", []) + [(flag, id)]
        return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">直供弹幕</d></i>'


def test_controller_resolves_danmaku_via_danmaku_content_method_for_short_drama(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(danmaku_cache_module, "danmaku_cache_dir", lambda: tmp_path)
    calls: list[tuple[str, str]] = []
    spider = DanmakuContentShortDramaSpider()

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            calls.append(("search", name))
            return []

        def resolve_danmu(self, page_url: str) -> str:
            calls.append(("resolve", page_url))
            return "unexpected"

    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.danmaku_controller is controller
    assert request.playback_loader is not None
    request.playback_loader(first)

    _wait_until(lambda: first.danmaku_xml != "")

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.danmaku_xml == '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">直供弹幕</d></i>'
    assert first.selected_danmaku_provider == "plugin:红果短剧"
    assert getattr(spider, "danmaku_content_calls", []) == [("红果线路", "/play/1")]
    assert calls == []


def test_controller_uses_cached_direct_danmaku_without_recalling_danmaku_content(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(danmaku_cache_module, "danmaku_cache_dir", lambda: tmp_path)
    # 文件级 autouse 夹具默认禁用持久缓存,这里恢复真实实现以验证直供缓存回读
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_xml",
        danmaku_cache_module.load_cached_danmaku_xml,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_xml",
        danmaku_cache_module.save_cached_danmaku_xml,
    )
    spider = DanmakuContentShortDramaSpider()

    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    _wait_until(lambda: first.danmaku_xml != "")

    assert len(getattr(spider, "danmaku_content_calls", [])) == 1

    replay_request = controller.build_request("/detail/1")
    replay_first = replay_request.playlist[0]
    replay_request.playback_loader(replay_first)

    _wait_until(lambda: replay_first.danmaku_xml != "")

    assert replay_first.danmaku_xml == first.danmaku_xml
    assert len(getattr(spider, "danmaku_content_calls", [])) == 1


class PayloadDanmakuUrlSpider(FakeSpider):
    def playerContent(self, flag, id, vipFlags):
        payload = dict(super().playerContent(flag, id, vipFlags))
        payload["danmaku"] = f"https://comments.example/{id}.xml"
        return payload


def test_controller_downloads_danmaku_from_player_content_payload_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(danmaku_cache_module, "danmaku_cache_dir", lambda: tmp_path)
    fetched: list[tuple[str, dict]] = []

    class FakeResponse:
        text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="2.0,1,25,16777215">url弹幕</d></i>'

        def raise_for_status(self):
            return None

    def fake_http_get(url, headers=None, timeout=None, follow_redirects=False):
        fetched.append((url, dict(headers or {})))
        return FakeResponse()

    monkeypatch.setattr(controller_module, "httpx", SimpleNamespace(get=fake_http_get))

    controller = SpiderPluginController(
        PayloadDanmakuUrlSpider(),
        plugin_name="任意源",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    _wait_until(lambda: first.danmaku_xml != "")

    assert first.danmaku_xml == '<?xml version="1.0" encoding="UTF-8"?><i><d p="2.0,1,25,16777215">url弹幕</d></i>'
    assert first.selected_danmaku_provider == "plugin:任意源"
    assert first.selected_danmaku_url == "https://comments.example//play/1.xml"
    assert fetched == [
        (
            "https://comments.example//play/1.xml",
            {"Referer": "https://site.example"},
        )
    ]


def test_controller_reads_danmaku_from_player_content_payload_inline_xml(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(danmaku_cache_module, "danmaku_cache_dir", lambda: tmp_path)
    inline_xml = '<i><d p="3.0,1,25,16777215">内联弹幕</d></i>'

    class InlinePayloadDanmakuSpider(FakeSpider):
        def playerContent(self, flag, id, vipFlags):
            payload = dict(super().playerContent(flag, id, vipFlags))
            payload["danmaku"] = {"xml": inline_xml}
            return payload

    controller = SpiderPluginController(
        InlinePayloadDanmakuSpider(),
        plugin_name="任意源",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    _wait_until(lambda: first.danmaku_xml != "")

    assert first.danmaku_xml == inline_xml
    assert first.selected_danmaku_url == ""


def test_controller_falls_back_to_aggregate_search_when_danmaku_content_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(danmaku_cache_module, "danmaku_cache_dir", lambda: tmp_path)
    calls: list[tuple[str, str]] = []

    class FlakyDanmakuContentSpider(PluginLevelDanmakuSpider):
        def danmakuContent(self, flag, id):
            calls.append(("direct", id))
            raise RuntimeError("sign expired")

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            calls.append(("search", name))
            return [
                DanmakuSearchItem(
                    provider="tencent",
                    name="归途 第1集",
                    url="https://v.qq.com/x/cover/demo.html",
                )
            ]

        def resolve_danmu(self, page_url: str) -> str:
            calls.append(("resolve", page_url))
            return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">聚合弹幕</d></i>'

    controller = SpiderPluginController(
        FlakyDanmakuContentSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    _wait_until(lambda: first.danmaku_xml != "")

    assert ("direct", "/play/1") in calls
    assert any(entry[0] == "search" for entry in calls)
    assert first.danmaku_xml == '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">聚合弹幕</d></i>'


def test_controller_records_direct_danmaku_error_when_no_aggregate_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(danmaku_cache_module, "danmaku_cache_dir", lambda: tmp_path)
    calls: list[tuple[str, str]] = []

    class BrokenDanmakuContentSpider(DanmakuContentShortDramaSpider):
        def danmakuContent(self, flag, id):
            calls.append(("direct", id))
            raise RuntimeError("sign expired")

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            calls.append(("search", name))
            return []

        def resolve_danmu(self, page_url: str) -> str:
            calls.append(("resolve", page_url))
            return "unexpected"

    controller = SpiderPluginController(
        BrokenDanmakuContentSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/1")
    first = request.playlist[0]

    assert request.playback_loader is not None
    request.playback_loader(first)

    _wait_until(lambda: first.danmaku_pending is False)

    assert first.url == "https://stream.example/play/1.m3u8"
    assert first.danmaku_xml == ""
    assert first.danmaku_error == "sign expired"
    assert calls == [("direct", "/play/1")]


def test_controller_prefetches_direct_danmaku_for_short_drama_next_episode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(danmaku_cache_module, "danmaku_cache_dir", lambda: tmp_path)
    spider = DanmakuContentShortDramaSpider()

    controller = SpiderPluginController(
        spider,
        plugin_name="红果短剧",
        search_enabled=True,
    )

    request = controller.build_request("/detail/1")
    playlist = request.playlist
    next_item = playlist[1]

    controller.prefetch_next_episode_danmaku(next_item, playlist)

    _wait_until(lambda: next_item.danmaku_xml != "")

    assert next_item.danmaku_xml != ""
    assert getattr(spider, "danmaku_content_calls", []) == [("红果线路", "/play/2")]


def test_controller_ignores_danmaku_resolution_failures_without_breaking_playback(caplog) -> None:
    class FailingDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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
    assert first.danmaku_search_query == "红果短剧 1集"
    assert calls == []


def test_spider_controller_downloads_episode_url_without_replacing_candidates(
    monkeypatch,
) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.resolve_calls: list[str] = []

        def provider_key_for_url(self, page_url: str) -> str:
            assert page_url == "https://v.qq.com/x/cover/demo/ep1.html"
            return "tencent"

        def resolve_danmu(self, page_url: str) -> str:
            self.resolve_calls.append(page_url)
            return '<i><d p="1,1,25,16777215">manual</d></i>'

    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", lambda _name, _url: "")
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_xml",
        lambda name, url, _xml: saved.append((name, url)),
    )
    service = RecordingService()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=service,
    )
    candidates = [
        DanmakuSourceGroup(
            provider="youku",
            provider_label="优酷",
            options=[
                DanmakuSourceOption(
                    provider="youku",
                    name="旧候选",
                    url="https://youku/old",
                )
            ],
        )
    ]
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        vod_id="item-1",
        media_title="红果短剧",
        danmaku_search_query="红果短剧 1集",
        danmaku_candidates=candidates,
    )

    xml = controller.download_danmaku_from_url(
        item,
        " https://v.qq.com/x/cover/demo/ep1.html ",
    )

    assert "manual" in xml
    assert service.resolve_calls == ["https://v.qq.com/x/cover/demo/ep1.html"]
    assert item.danmaku_candidates is candidates
    assert item.selected_danmaku_provider == "tencent"
    assert item.selected_danmaku_url == "https://v.qq.com/x/cover/demo/ep1.html"
    assert item.selected_danmaku_title == "第1集"
    assert ("红果短剧 1集", "https://v.qq.com/x/cover/demo/ep1.html") in saved


def test_spider_controller_episode_url_failure_preserves_loaded_source(
    monkeypatch,
) -> None:
    class FailingService:
        def provider_key_for_url(self, _page_url: str) -> str:
            return "tencent"

        def resolve_danmu(self, _page_url: str) -> str:
            raise RuntimeError("boom")

    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", lambda _name, _url: "")
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        danmaku_service=FailingService(),
    )
    candidates = [DanmakuSourceGroup(provider="youku", provider_label="优酷", options=[])]
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_search_query="红果短剧 1集",
        danmaku_candidates=candidates,
        danmaku_xml="<i>old</i>",
        selected_danmaku_provider="youku",
        selected_danmaku_url="https://youku/old",
        selected_danmaku_title="旧来源",
    )

    with pytest.raises(RuntimeError, match="boom"):
        controller.download_danmaku_from_url(
            item,
            "https://v.qq.com/x/cover/demo/ep1.html",
        )

    assert item.danmaku_candidates is candidates
    assert item.danmaku_xml == "<i>old</i>"
    assert item.selected_danmaku_provider == "youku"
    assert item.selected_danmaku_url == "https://youku/old"
    assert item.selected_danmaku_title == "旧来源"


def test_controller_uses_default_query_xml_cache_alias_after_manual_override_restart(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    calls: list[tuple[str, str]] = []
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">cached</d></i>'

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            calls.append(("search", f"{name}|{reg_src}"))
            return []

        def resolve_danmu(self, page_url: str) -> str:
            calls.append(("resolve", page_url))
            return xml_text

    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=FakeDanmakuService(),
    )
    current = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="玄界之门3D版",
        danmaku_search_query="玄界之门 1集",
        danmaku_search_query_overridden=True,
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
            )
        ],
    )

    controller.switch_danmaku_source(current, "https://v.qq.com/demo")

    restarted = PlayItem(title="第1集", url="https://stream.example/1.m3u8", media_title="玄界之门3D版")

    controller._resolve_danmaku_sync(restarted, restarted.url)

    assert restarted.danmaku_xml == xml_text
    assert calls == [("resolve", "https://v.qq.com/demo")]


def test_controller_rebuild_request_auto_loads_danmaku_xml_after_manual_override_restart(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">cached</d></i>'
    first_calls: list[tuple[str, str]] = []
    second_calls: list[tuple[str, str]] = []

    class FirstDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            first_calls.append(("search", f"{name}|{reg_src}"))
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第1集", url="https://v.qq.com/demo")],
                    )
                ],
                default_option_url="https://v.qq.com/demo",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            first_calls.append(("resolve", page_url))
            return xml_text

    class SecondDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            second_calls.append(("search", f"{name}|{reg_src}"))
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            second_calls.append(("search-legacy", f"{name}|{reg_src}"))
            return []

        def resolve_danmu(self, page_url: str) -> str:
            second_calls.append(("resolve", page_url))
            return ""

    first_controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=FirstDanmakuService(),
    )
    first_request = first_controller.build_request("/detail/1")
    first_item = first_request.playlist[0]

    assert first_request.playback_loader is not None
    first_request.playback_loader(first_item)
    _wait_until(lambda: first_item.danmaku_pending is False and first_item.url != "")
    first_controller.refresh_danmaku_sources(first_item, query_override="玄界之门 1集", force_refresh=True)
    first_controller.switch_danmaku_source(first_item, "https://v.qq.com/demo")

    second_controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=SecondDanmakuService(),
    )
    second_request = second_controller.build_request("/detail/1")
    second_item = second_request.playlist[0]

    assert second_request.playback_loader is not None
    second_request.playback_loader(second_item)
    _wait_until(lambda: second_item.danmaku_pending is False and second_item.url != "")

    assert second_item.danmaku_xml == xml_text
    assert second_calls == []


def test_controller_rebuild_request_reuses_prefetched_next_episode_danmaku_after_restart(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">cached-next</d></i>'
    first_calls: list[tuple[str, str]] = []
    second_calls: list[tuple[str, str]] = []

    class FirstDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            first_calls.append(("search", f"{name}|{reg_src}"))
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第2集", url="https://v.qq.com/ep2")],
                    )
                ],
                default_option_url="https://v.qq.com/ep2",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            first_calls.append(("resolve", page_url))
            return xml_text

    class SecondDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            second_calls.append(("search", f"{name}|{reg_src}"))
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            second_calls.append(("search-legacy", f"{name}|{reg_src}"))
            return []

        def resolve_danmu(self, page_url: str) -> str:
            second_calls.append(("resolve", page_url))
            raise AssertionError("restart should reuse prefetched next-episode danmaku")

    first_controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=FirstDanmakuService(),
    )
    first_request = first_controller.build_request("/detail/1")
    second_item = first_request.playlist[1]

    first_controller.prefetch_next_episode_danmaku(second_item, first_request.playlist)
    _wait_until(lambda: second_item.danmaku_pending is False and second_item.danmaku_xml == xml_text)

    assert first_request.playback_loader is not None
    first_request.playback_loader(second_item)
    assert second_item.url == "https://media.example/2.m3u8"
    assert second_item.danmaku_xml == xml_text

    second_controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=SecondDanmakuService(),
    )
    second_request = second_controller.build_request("/detail/1")
    restarted_second = second_request.playlist[1]

    assert second_request.playback_loader is not None
    second_request.playback_loader(restarted_second)
    _wait_until(lambda: restarted_second.danmaku_pending is False and restarted_second.url != "")

    assert restarted_second.danmaku_xml == xml_text
    assert second_calls == []


def test_controller_rebuild_request_reuses_prefetched_next_episode_danmaku_after_restart_for_playercontent_routes(
    monkeypatch,
    tmp_path,
) -> None:
    class TwoEpisodeDanmakuSpider(FakeSpider):
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "玄界之门3D版",
                        "vod_pic": "poster-detail",
                        "vod_play_from": "默认线",
                        "vod_play_url": "第1集$/play/1#第2集$/play/2",
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):
            return {"parse": 0, "url": f"https://stream.example{id}.m3u8"}

        def danmaku(self):
            return True

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">cached-playercontent-next</d></i>'
    first_calls: list[tuple[str, str]] = []
    second_calls: list[tuple[str, str]] = []

    class FirstDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            first_calls.append(("search", f"{name}|{reg_src}"))
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="tencent",
                        provider_label="腾讯",
                        options=[DanmakuSourceOption(provider="tencent", name="玄界之门 第2集", url="https://v.qq.com/ep2")],
                    )
                ],
                default_option_url="https://v.qq.com/ep2",
                default_provider="tencent",
            )

        def resolve_danmu(self, page_url: str) -> str:
            first_calls.append(("resolve", page_url))
            return xml_text

    class SecondDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            second_calls.append(("search", f"{name}|{reg_src}"))
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            second_calls.append(("search-legacy", f"{name}|{reg_src}"))
            return []

        def resolve_danmu(self, page_url: str) -> str:
            second_calls.append(("resolve", page_url))
            raise AssertionError("restart should reuse prefetched playerContent next-episode danmaku")

    first_controller = SpiderPluginController(
        TwoEpisodeDanmakuSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=FirstDanmakuService(),
    )
    first_request = first_controller.build_request("/detail/1")
    prefetched_second = first_request.playlist[1]

    first_controller.prefetch_next_episode_danmaku(prefetched_second, first_request.playlist)
    _wait_until(lambda: prefetched_second.danmaku_pending is False and prefetched_second.danmaku_xml == xml_text)

    assert first_request.playback_loader is not None
    first_request.playback_loader(prefetched_second)
    assert prefetched_second.url == "https://stream.example/play/2.m3u8"
    assert prefetched_second.danmaku_xml == xml_text

    second_controller = SpiderPluginController(
        TwoEpisodeDanmakuSpider(),
        plugin_name="玄界之门3D版",
        search_enabled=True,
        danmaku_service=SecondDanmakuService(),
    )
    second_request = second_controller.build_request("/detail/1")
    restarted_second = second_request.playlist[1]

    assert second_request.playback_loader is not None
    second_request.playback_loader(restarted_second)
    _wait_until(lambda: restarted_second.danmaku_pending is False and restarted_second.url != "")

    assert restarted_second.danmaku_xml == xml_text
    assert second_calls == []


def test_controller_rebuild_request_reuses_prefetched_next_episode_danmaku_after_restart_for_drive_replacement_playlist(
    monkeypatch,
    tmp_path,
) -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">cached-drive-next</d></i>'
    second_calls: list[tuple[str, str]] = []

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

    class FirstDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            return [DanmakuSearchItem(provider="tencent", name=name, url="https://v.qq.com/x/cover/demo.html")]

        def resolve_danmu(self, page_url: str) -> str:
            assert page_url == "https://v.qq.com/x/cover/demo.html"
            return xml_text

    class SecondDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            second_calls.append(("search", f"{name}|{reg_src}"))
            return []

        def resolve_danmu(self, page_url: str) -> str:
            second_calls.append(("resolve", page_url))
            raise AssertionError("restart should reuse prefetched drive next-episode danmaku")

    first_controller = SpiderPluginController(
        DanmakuDriveLinkSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=FirstDanmakuService(),
    )
    first_request = first_controller.build_request("/detail/drive")
    assert first_request.playback_loader is not None
    first_result = first_request.playback_loader(first_request.playlists[0][0])

    assert first_result is not None
    first, second = first_result.replacement_playlist
    _wait_until(lambda: first.danmaku_pending is False and first.danmaku_xml == xml_text)

    first_controller.prefetch_next_episode_danmaku(second, first_result.replacement_playlist)
    _wait_until(lambda: second.danmaku_pending is False and second.danmaku_xml == xml_text)

    first_request.playback_loader(second)
    assert second.danmaku_xml == xml_text

    second_controller = SpiderPluginController(
        DanmakuDriveLinkSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=SecondDanmakuService(),
    )
    second_request = second_controller.build_request("/detail/drive")
    assert second_request.playback_loader is not None
    second_result = second_request.playback_loader(second_request.playlists[0][0])

    assert second_result is not None
    restarted_second = second_result.replacement_playlist[1]
    second_request.playback_loader(restarted_second)
    _wait_until(lambda: restarted_second.danmaku_pending is False)

    assert restarted_second.danmaku_xml == xml_text
    assert second_calls == []


def test_controller_rebuild_request_reuses_prefetched_next_episode_danmaku_via_saved_page_url_when_search_cache_misses(
    monkeypatch,
    tmp_path,
) -> None:
    class TwoEpisodeDanmakuSpider(FakeSpider):
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "盗妖行",
                        "vod_pic": "poster-detail",
                        "vod_play_from": "默认线",
                        "vod_play_url": "第7集$/play/7#第8集$/play/8",
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):
            return {"parse": 0, "url": f"http://192.168.50.60:4567/p/web{id}"}

        def danmaku(self):
            return True

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">cached-by-page-url</d></i>'
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")

    class FirstDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="bilibili",
                        provider_label="B站",
                        options=[DanmakuSourceOption(provider="bilibili", name="《盗妖行》第8话", url="https://www.bilibili.com/video/BVprefetch8")],
                    )
                ],
                default_option_url="https://www.bilibili.com/video/BVprefetch8",
                default_provider="bilibili",
            )

        def resolve_danmu(self, page_url: str) -> str:
            assert page_url == "https://www.bilibili.com/video/BVprefetch8"
            return xml_text

    class SecondDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            raise AssertionError("restart should use saved page_url xml cache before searching")

        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            raise AssertionError("restart should use saved page_url xml cache before legacy searching")

        def resolve_danmu(self, page_url: str) -> str:
            raise AssertionError("restart should use cached xml by saved page_url")

    first_controller = SpiderPluginController(
        TwoEpisodeDanmakuSpider(),
        plugin_name="盗妖行",
        search_enabled=True,
        danmaku_service=FirstDanmakuService(),
        danmaku_preference_store=store,
    )
    first_request = first_controller.build_request("/detail/1")
    prefetched_second = first_request.playlist[1]

    first_controller.prefetch_next_episode_danmaku(prefetched_second, first_request.playlist)
    _wait_until(lambda: prefetched_second.danmaku_pending is False and prefetched_second.danmaku_xml == xml_text)

    monkeypatch.setattr(controller_module, "load_cached_danmaku_source_search_result", lambda name, reg_src: None)

    second_controller = SpiderPluginController(
        TwoEpisodeDanmakuSpider(),
        plugin_name="盗妖行",
        search_enabled=True,
        danmaku_service=SecondDanmakuService(),
        danmaku_preference_store=store,
    )
    second_request = second_controller.build_request("/detail/1")
    restarted_second = second_request.playlist[1]

    assert second_request.playback_loader is not None
    second_request.playback_loader(restarted_second)
    _wait_until(lambda: restarted_second.danmaku_pending is False and restarted_second.url != "")

    assert restarted_second.danmaku_xml == xml_text


def test_controller_resolve_danmaku_sync_reuses_prefetched_xml_when_reg_src_changes_and_search_cache_misses(
    monkeypatch,
    tmp_path,
) -> None:
    class DanmakuEnabledFakeSpider(FakeSpider):
        def danmaku(self):
            return True

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "load_cached_danmaku_source_search_result", lambda name, reg_src: None)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_source_search_result", danmaku_cache_module.save_cached_danmaku_source_search_result)
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">cached-by-prefetch-page-url</d></i>'
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")

    class FirstDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="bilibili",
                        provider_label="B站",
                        options=[DanmakuSourceOption(provider="bilibili", name="《盗妖行》第8话", url="https://www.bilibili.com/video/BVprefetch8")],
                    )
                ],
                default_option_url="https://www.bilibili.com/video/BVprefetch8",
                default_provider="bilibili",
            )

        def resolve_danmu(self, page_url: str) -> str:
            assert page_url == "https://www.bilibili.com/video/BVprefetch8"
            return xml_text

    class SecondDanmakuService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
        ):
            raise AssertionError("should reuse prefetched xml via saved page_url before searching")

        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            raise AssertionError("should reuse prefetched xml via saved page_url before legacy searching")

        def resolve_danmu(self, page_url: str) -> str:
            raise AssertionError("should reuse prefetched xml via saved page_url before resolving")

    first_controller = SpiderPluginController(
        DanmakuEnabledFakeSpider(),
        plugin_name="盗妖行",
        search_enabled=True,
        danmaku_service=FirstDanmakuService(),
        danmaku_preference_store=store,
    )
    prefetched = PlayItem(title="第8集", url="", vod_id="/prefetch/8", media_title="盗妖行")

    first_controller.prefetch_next_episode_danmaku(prefetched, [PlayItem(title="第7集", url=""), prefetched])
    _wait_until(lambda: prefetched.danmaku_pending is False and prefetched.danmaku_xml == xml_text)

    second_controller = SpiderPluginController(
        DanmakuEnabledFakeSpider(),
        plugin_name="盗妖行",
        search_enabled=True,
        danmaku_service=SecondDanmakuService(),
        danmaku_preference_store=store,
    )
    restarted = PlayItem(
        title="08-第8话 不止杀人，还会玩人！",
        url="http://192.168.50.60:4567/p/web/1@111724",
        media_title="盗妖行",
    )

    second_controller._resolve_danmaku_sync(restarted, restarted.url, [PlayItem(title="第7集", url="x"), restarted])

    assert restarted.danmaku_xml == xml_text


def test_controller_resolve_danmaku_sync_reuses_cached_candidate_xml_across_plugins_without_duplicate_logs(
    monkeypatch,
    tmp_path,
) -> None:
    class DanmakuEnabledFakeSpider(FakeSpider):
        def danmaku(self):
            return True

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">cached-by-candidate-page-url</d></i>'
    query_name = "盗妖行 4集"
    page_url = "https://www.bilibili.com/video/BVprefetch4"
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    store.save(
        controller_module.DanmakuSeriesPreference(
            series_key=build_danmaku_series_key("盗妖行"),
            provider="bilibili",
            page_url="https://www.bilibili.com/video/BVprefetch5",
            title="《盗妖行》第5话 回娘家",
            search_title="盗妖行",
        )
    )
    danmaku_cache_module.save_cached_danmaku_source_search_result(
        query_name,
        "",
        DanmakuSourceSearchResult(
            groups=[
                DanmakuSourceGroup(
                    provider="bilibili",
                    provider_label="B站",
                    options=[
                        DanmakuSourceOption(
                            provider="bilibili",
                            name="《盗妖行》第4话 啥！啥！这是啥啊！",
                            url=page_url,
                        )
                    ],
                )
            ],
            default_option_url=page_url,
            default_provider="bilibili",
        ),
    )
    danmaku_cache_module.save_cached_danmaku_xml(query_name, page_url, xml_text)

    class NoNetworkDanmakuService:
        def rerank_danmaku_source_search_result(self, result, **kwargs):
            return result

        def search_danmu_sources(self, *args, **kwargs):
            raise AssertionError("should reuse cached candidate xml before searching")

        def search_danmu(self, *args, **kwargs):
            raise AssertionError("should reuse cached candidate xml before legacy searching")

        def resolve_danmu(self, *args, **kwargs):
            raise AssertionError("should reuse cached candidate xml before resolving")

    controller = SpiderPluginController(
        DanmakuEnabledFakeSpider(),
        plugin_name="盗妖行",
        search_enabled=True,
        danmaku_service=NoNetworkDanmakuService(),
        danmaku_preference_store=store,
    )
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)
    restarted = PlayItem(
        title="04-第4话 啥！啥！这是啥啊！-1080P 高码率-HEVC-2026-03-03",
        url="http://192.168.50.60:4567/p/web/1@111720",
        media_title="盗妖行",
    )

    controller._resolve_danmaku_sync(restarted, restarted.url, [PlayItem(title="第3集", url="x"), restarted])

    assert restarted.danmaku_xml == xml_text
    assert restarted.selected_danmaku_provider == "bilibili"
    assert restarted.selected_danmaku_url == page_url
    assert logs == []


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


def test_controller_replaces_only_current_magnet_item_when_offline_download_returns_files() -> None:
    class MagnetSpider(FakeSpider):
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "磁力片源",
                        "vod_play_from": "磁力线",
                        "vod_play_url": (
                            "第1集$magnet:?xt=urn:btih:8a06396e03acb19d72eb2d779a22b2dc00f66a33#"
                            "第2集$magnet:?xt=urn:btih:bbbb6396e03acb19d72eb2d779a22b2dc00f66bb"
                        ),
                    }
                ]
            }

    offline_calls: list[str] = []
    resolve_calls: list[str] = []

    def load_offline_detail(link: str) -> dict:
        offline_calls.append(link)
        if link.endswith("66a33"):
            return {
                "list": [
                    {
                        "vod_id": "1$107919$1",
                        "vod_name": "离线下载结果 A",
                        "vod_play_from": "丫仙女",
                        "vod_play_url": "离线A1.mp4(6.11 GB)$1@107920@0@0",
                        "path": "/我的115云盘/alist-tvbox-offline/A/~playlist",
                        "items": [],
                    }
                ]
            }
        return {
            "list": [
                {
                    "vod_id": "1$107929$1",
                    "vod_name": "离线下载结果 B",
                    "vod_play_from": "丫仙女",
                    "vod_play_url": "离线B1.mp4(1 GB)$1@107930@0@0#离线B2.mp4(1 GB)$1@107931@0@1",
                    "path": "/我的115云盘/alist-tvbox-offline/B/~playlist",
                    "items": [],
                }
            ]
        }

    def load_drive_detail(vod_id: str) -> dict:
        resolve_calls.append(vod_id)
        assert vod_id == "1@107920@0@0"
        return {
            "list": [
                {
                    "vod_id": "1@107920@0@0",
                    "vod_name": "离线文件.mp4",
                    "items": [{"url": "http://192.168.50.60:4567/p/web/1@107920?ac=web&ids=1$107920$1"}],
                }
            ]
        }

    controller = SpiderPluginController(
        MagnetSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        offline_download_detail_loader=load_offline_detail,
    )

    request = controller.build_request("/detail/magnet")
    session = PlayerController(type("Api", (), {"get_history": lambda self, vod_id: None})()).create_session(
        request.vod,
        request.playlist,
        clicked_index=0,
        playlists=request.playlists,
        playlist_index=request.playlist_index,
        detail_resolver=request.detail_resolver,
        resolved_vod_by_id=request.resolved_vod_by_id,
        use_local_history=request.use_local_history,
        playback_loader=request.playback_loader,
        async_playback_loader=request.async_playback_loader,
        detail_action_runner=request.detail_action_runner,
        danmaku_controller=request.danmaku_controller,
    )
    assert request.playback_loader is not None
    first_result = request.playback_loader(session, request.playlists[0][0])

    assert first_result is not None
    assert offline_calls == ["magnet:?xt=urn:btih:8a06396e03acb19d72eb2d779a22b2dc00f66a33"]
    assert [item.title for item in first_result.replacement_playlist] == ["离线A1.mp4(6.11 GB)", "第2集"]
    assert [item.vod_id for item in first_result.replacement_playlist] == ["1@107920@0@0", "magnet:?xt=urn:btih:bbbb6396e03acb19d72eb2d779a22b2dc00f66bb"]
    assert first_result.replacement_start_index == 0

    session.playlist = first_result.replacement_playlist
    session.playlists[session.playlist_index] = session.playlist
    resolved = session.detail_resolver(session.playlist[0])
    assert resolved is not None
    session.playlist[0].url = resolved.items[0].url
    assert session.playlist[0].title == "离线A1.mp4(6.11 GB)"

    second_result = request.playback_loader(session, session.playlist[1])

    assert second_result is not None
    assert offline_calls == [
        "magnet:?xt=urn:btih:8a06396e03acb19d72eb2d779a22b2dc00f66a33",
        "magnet:?xt=urn:btih:bbbb6396e03acb19d72eb2d779a22b2dc00f66bb",
    ]
    assert [item.title for item in second_result.replacement_playlist] == [
        "离线A1.mp4(6.11 GB)",
        "离线B1.mp4(1 GB)",
        "离线B2.mp4(1 GB)",
    ]
    assert [item.vod_id for item in second_result.replacement_playlist] == [
        "1@107920@0@0",
        "1@107930@0@0",
        "1@107931@0@1",
    ]
    assert second_result.replacement_start_index == 1

    assert session.detail_resolver is not None
    assert resolve_calls == ["1@107920@0@0"]
    assert resolved.items[0].title == ""
    assert resolved.items[0].url == "http://192.168.50.60:4567/p/web/1@107920?ac=web&ids=1$107920$1"


def test_controller_preserves_original_media_title_for_offline_download_replacement_items() -> None:
    class MagnetSpider(FakeSpider):
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "喀什恋歌 (2026)",
                        "vod_play_from": "磁力线",
                        "vod_play_url": "第1集$magnet:?xt=urn:btih:8a06396e03acb19d72eb2d779a22b2dc00f66a33",
                    }
                ]
            }

    def load_offline_detail(link: str) -> dict:
        assert link == "magnet:?xt=urn:btih:8a06396e03acb19d72eb2d779a22b2dc00f66a33"
        return {
            "list": [
                {
                    "vod_id": "1$107919$1",
                    "vod_name": "离线下载结果 A",
                    "vod_play_from": "丫仙女",
                    "vod_play_url": "Bloom.Life.2026.EP01-08.HD1080P.X264.AAC.Mandarin.CHS.XLYS.mkv$1@107920@0@0",
                    "path": "/我的115云盘/alist-tvbox-offline/A/~playlist",
                    "items": [],
                }
            ]
        }

    class FakeDanmakuService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ):
            self.calls.append(name)
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

    service = FakeDanmakuService()
    controller = SpiderPluginController(
        MagnetSpider(),
        plugin_name="喀什恋歌",
        search_enabled=True,
        offline_download_detail_loader=load_offline_detail,
        danmaku_service=service,
    )

    request = controller.build_request("/detail/magnet")

    assert request.playback_loader is not None
    result = request.playback_loader(request.playlist[0])

    assert result is not None
    replacement = result.replacement_playlist[0]
    assert replacement.title == "Bloom.Life.2026.EP01-08.HD1080P.X264.AAC.Mandarin.CHS.XLYS.mkv"
    assert replacement.media_title == "喀什恋歌 (2026)"

    controller.refresh_danmaku_sources(replacement)

    assert service.calls == ["喀什恋歌 1集"]
    assert replacement.danmaku_search_title == "喀什恋歌"
    assert replacement.danmaku_search_query == "喀什恋歌 1集"


def test_controller_resolves_player_content_magnet_url_via_offline_download_loader() -> None:
    class PlayerContentMagnetSpider(FakeSpider):
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "磁力片源",
                        "vod_play_from": "磁力线",
                        "vod_play_url": "磁力1$/play/1",
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):  # noqa: A002
            assert flag == "磁力线"
            assert id == "/play/1"
            return {
                "parse": 0,
                "jx": 0,
                "playUrl": "",
                "url": "magnet:?xt=urn:btih:98c6ca6ef890af0e858852e462282cd7f17a86a4",
                "header": {},
            }

    offline_calls: list[str] = []

    def load_offline_detail(link: str) -> dict:
        offline_calls.append(link)
        return {
            "list": [
                {
                    "vod_id": "1$107919$1",
                    "vod_name": "离线下载结果 A",
                    "vod_play_from": "丫仙女",
                    "vod_play_url": "离线A1.mp4(6.11 GB)$1@107920@0@0",
                    "path": "/我的115云盘/alist-tvbox-offline/A/~playlist",
                    "items": [],
                }
            ]
        }

    controller = SpiderPluginController(
        PlayerContentMagnetSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        offline_download_detail_loader=load_offline_detail,
    )

    request = controller.build_request("/detail/magnet")

    assert request.playback_loader is not None
    result = request.playback_loader(request.playlist[0])

    assert result is not None
    assert offline_calls == ["magnet:?xt=urn:btih:98c6ca6ef890af0e858852e462282cd7f17a86a4"]
    assert [item.title for item in result.replacement_playlist] == ["离线A1.mp4(6.11 GB)"]
    assert [item.vod_id for item in result.replacement_playlist] == ["1@107920@0@0"]
    assert result.replacement_start_index == 0


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
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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
    _wait_until(lambda: result.replacement_playlist[0].danmaku_pending is False)
    assert calls == [("search", "网盘剧集 1集|https://pan.quark.cn/s/f518510ef92a")]


def test_controller_extracts_episode_number_from_sxxexx_style_titles() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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
    _wait_until(lambda: result.replacement_playlist[0].danmaku_pending is False)
    assert calls == [("search", "网盘剧集 25集|https://pan.quark.cn/s/f518510ef92a")]


def test_controller_extracts_episode_number_from_numeric_title_with_size_suffix() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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
    _wait_until(lambda: result.replacement_playlist[0].danmaku_pending is False)
    assert calls == [("search", "网盘剧集 12集|https://pan.quark.cn/s/f518510ef92a")]


def test_controller_uses_media_title_only_for_year_prefixed_movie_filename() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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


def test_controller_uses_media_title_only_for_single_quality_filename_drive_item() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "长夜将尽",
                        "vod_play_from": "百度",
                        "vod_play_url": "正片$https://pan.quark.cn/s/f518510ef92a",
                    }
                ]
            }

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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
                        {
                            "title": "4k.mp4(3.39 GB)",
                            "url": "http://m/1.mp4",
                            "path": "/长夜将尽/4k.mp4",
                            "size": 3639986176,
                        }
                    ],
                }
            ]
        }

    controller = SpiderPluginController(
        DanmakuDriveLinkSpider(),
        plugin_name="长夜将尽",
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
    assert calls == [("search", "长夜将尽|http://m/1.mp4")]


def test_controller_movie_category_name_overrides_non_movie_type_name_for_drive_item() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "长夜将尽",
                        "type_name": "剧情,爱情,短片",
                        "vod_play_from": "百度",
                        "vod_play_url": "正片$https://pan.quark.cn/s/f518510ef92a",
                    }
                ]
            }

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
            calls.append((name, reg_src))
            return []

    def load_drive_detail(link: str) -> dict:
        assert link == "https://pan.quark.cn/s/f518510ef92a"
        return {
            "list": [
                {
                    "vod_id": "1$94954$1",
                    "vod_name": "夸克资源",
                    "items": [
                        {"title": "4k.mp4(3.39 GB)", "url": "http://m/1.mp4", "path": "/长夜将尽/4k.mp4", "size": 3639986176},
                        {"title": "1080p.mp4(1.28 GB)", "url": "http://m/2.mp4", "path": "/长夜将尽/1080p.mp4", "size": 1374389534},
                        {"title": "720p.mp4(0.74 GB)", "url": "http://m/3.mp4", "path": "/长夜将尽/720p.mp4", "size": 794568949},
                    ],
                }
            ]
        }

    controller = SpiderPluginController(
        DanmakuDriveLinkSpider(),
        plugin_name="长夜将尽",
        search_enabled=True,
        drive_detail_loader=load_drive_detail,
        danmaku_service=FakeDanmakuService(),
    )

    request = controller.build_request("/detail/drive")
    request.vod.category_name = "多多电影"
    request.playlist[0].category_name = "多多电影"

    assert request.playback_loader is not None
    result = request.playback_loader(request.playlist[0])

    assert result is not None
    replacement = result.replacement_playlist[0]
    _wait_until(lambda: replacement.danmaku_pending is False)

    assert replacement.category_name == "多多电影"
    assert calls == [("长夜将尽", "https://pan.quark.cn/s/f518510ef92a")]
    assert replacement.danmaku_search_episode == ""


def test_controller_uses_replacement_playlist_index_when_drive_titles_have_no_episode_number() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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
    _wait_until(
        lambda: result.replacement_playlist[0].danmaku_pending is False
        and result.replacement_playlist[1].danmaku_pending is False
    )
    _wait_until(lambda: len(calls) == 2)

    assert sorted(calls) == sorted(
        [
        ("search", "网盘剧集 1集|https://pan.quark.cn/s/f518510ef92a"),
        ("search", "网盘剧集 2集|http://m/2.mp4"),
        ]
    )


def test_controller_prefers_cjk_bar_separated_drive_titles_over_playlist_position() -> None:
    class DanmakuDriveLinkSpider(DriveLinkSpider):
        def danmaku(self):
            return True

    calls: list[tuple[str, str]] = []

    class FakeDanmakuService:
        def search_danmu(self, name: str, reg_src: str = "", provider_filter: str = ""):
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
                        {"title": "01~(894.9 MB)", "url": "http://m/1.mp4", "path": "/S1/01~4K.mp4", "size": 11},
                        {"title": "01丨(974.69 MB)", "url": "http://m/1b.mp4", "path": "/S1/01丨4K.mp4", "size": 12},
                        {"title": "02丨(819.27 MB)", "url": "http://m/2.mp4", "path": "/S1/02丨4K.mp4", "size": 13},
                        {"title": "03-(704.61 MB)", "url": "http://m/3.mp4", "path": "/S1/03-4K.mp4", "size": 14},
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
    request.playback_loader(result.replacement_playlist[2])
    _wait_until(lambda: len(calls) == 3)

    assert sorted(calls) == sorted(
        [
            ("search", "网盘剧集 1集|https://pan.quark.cn/s/f518510ef92a"),
            ("search", "网盘剧集 1集|http://m/1b.mp4"),
            ("search", "网盘剧集 2集|http://m/2.mp4"),
        ]
    )


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


def test_spider_controller_maps_detailcontent_collection_actions_and_playercontent_item_actions_to_play_item() -> None:
    controller = SpiderPluginController(ActionPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")
    session = PlayerController(type("Api", (), {"get_history": lambda self, _key: None})()).create_session(
        request.vod,
        request.playlist,
        request.clicked_index,
        playlists=request.playlists,
        playlist_index=request.playlist_index,
        playback_loader=request.playback_loader,
        async_playback_loader=request.async_playback_loader,
        detail_action_runner=request.detail_action_runner,
    )

    assert session.playback_loader is not None
    session.playback_loader(session.playlist[0])

    assert session.playlist[0].detail_actions == [
        PlaybackDetailAction(id="favorite_album", label="收藏专辑", active=True, tooltip="已收藏"),
        PlaybackDetailAction(id="favorite_track", label="收藏歌曲", enabled=False),
    ]


def test_spider_controller_applies_detailcontent_collection_actions_before_playercontent_load() -> None:
    controller = SpiderPluginController(ActionPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")

    assert request.playlist[0].detail_actions == [
        PlaybackDetailAction(id="favorite_album", label="收藏专辑", active=True, tooltip="已收藏"),
    ]


def test_spider_controller_detail_action_runner_returns_refreshed_actions() -> None:
    controller = SpiderPluginController(ActionPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")

    assert request.detail_action_runner is not None
    refreshed = request.detail_action_runner(request.playlist[0], "favorite_track")

    assert refreshed == [
        PlaybackDetailAction(id="favorite_album", label="已收藏专辑", active=True),
        PlaybackDetailAction(id="favorite_track", label="已收藏歌曲", active=True),
    ]


def test_spider_controller_detail_action_runner_preserves_existing_item_actions_when_refresh_is_partial() -> None:
    controller = SpiderPluginController(PartialActionRefreshSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")
    session = PlayerController(type("Api", (), {"get_history": lambda self, _key: None})()).create_session(
        request.vod,
        request.playlist,
        request.clicked_index,
        playlists=request.playlists,
        playlist_index=request.playlist_index,
        playback_loader=request.playback_loader,
        async_playback_loader=request.async_playback_loader,
        detail_action_runner=request.detail_action_runner,
    )

    assert session.playback_loader is not None
    session.playback_loader(session.playlist[0])
    assert request.detail_action_runner is not None

    refreshed = request.detail_action_runner(session.playlist[0], "favorite_album")

    assert refreshed == [
        PlaybackDetailAction(id="favorite_album", label="已收藏专辑", active=True),
        PlaybackDetailAction(id="favorite_track", label="收藏歌曲", enabled=False),
    ]


def test_spider_controller_maps_detailcontent_ext_to_vod_detail_fields() -> None:
    controller = SpiderPluginController(DetailFieldPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")

    assert request.vod.detail_fields == [
        PlaybackDetailField(label="播放", value="12万"),
        PlaybackDetailField(label="更新", value="2026-05-08"),
    ]
    assert request.playlist[0].detail_fields == []


def test_spider_controller_maps_clickable_detailcontent_ext_value_objects() -> None:
    controller = SpiderPluginController(ClickableDetailFieldPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")

    assert _detail_field_signature(request.vod.detail_fields) == [
        ("演员", [("演员1", "search", "演员1"), ("演员2", "detail", "actor-2")]),
        ("标签", [("动作", None, None), ("冒险", None, None)]),
    ]


def test_spider_controller_downgrades_invalid_detail_field_actions_to_plain_text() -> None:
    controller = SpiderPluginController(InvalidClickableDetailFieldPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")

    assert _detail_field_signature(request.vod.detail_fields) == [
        ("导演", [("导演1", None, None)]),
    ]


def test_spider_controller_maps_playercontent_ext_to_current_play_item_detail_fields() -> None:
    controller = SpiderPluginController(DetailFieldPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")
    session = PlayerController(type("Api", (), {"get_history": lambda self, _key: None})()).create_session(
        request.vod,
        request.playlist,
        request.clicked_index,
        playlists=request.playlists,
        playlist_index=request.playlist_index,
        playback_loader=request.playback_loader,
        async_playback_loader=request.async_playback_loader,
        detail_action_runner=request.detail_action_runner,
    )

    assert session.playback_loader is not None
    session.playback_loader(session.playlist[0])

    assert session.playlist[0].detail_fields == [
        PlaybackDetailField(label="播放", value="18万"),
        PlaybackDetailField(label="热度", value="95"),
    ]


def test_spider_controller_clears_stale_item_detail_fields_when_playercontent_ext_is_invalid() -> None:
    controller = SpiderPluginController(DetailFieldPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")
    session = PlayerController(type("Api", (), {"get_history": lambda self, _key: None})()).create_session(
        request.vod,
        request.playlist,
        request.clicked_index,
        playlists=request.playlists,
        playlist_index=request.playlist_index,
        playback_loader=request.playback_loader,
        async_playback_loader=request.async_playback_loader,
        detail_action_runner=request.detail_action_runner,
    )

    assert session.playback_loader is not None
    session.playback_loader(session.playlist[0])
    assert session.playlist[0].detail_fields == [
        PlaybackDetailField(label="播放", value="18万"),
        PlaybackDetailField(label="热度", value="95"),
    ]

    second_item = session.playlist[1]
    second_item.detail_fields = [PlaybackDetailField(label="旧值", value="stale")]
    session.playback_loader(second_item)

    assert second_item.detail_fields == []


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


def test_controller_logs_spider_method_calls_with_params(caplog) -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="调试插件", search_enabled=True)

    with caplog.at_level(logging.INFO):
        controller.load_items("tv", 2, {"area": "CN"})
        controller.search_items("庆余年", 3, "tv")
        request = controller.build_request("detail-1")

        assert request.playback_loader is not None
        request.playback_loader(request.playlist[0])

    assert "method=categoryContent" in caplog.text
    assert "tid='tv'" in caplog.text
    assert "pg=2" in caplog.text
    assert "filter=False" in caplog.text
    assert "extend={'area': 'CN'}" in caplog.text

    assert "method=searchContent" in caplog.text
    assert "key='庆余年'" in caplog.text
    assert "quick=False" in caplog.text
    assert "category='tv'" in caplog.text

    assert "method=detailContent" in caplog.text
    assert "ids=['detail-1']" in caplog.text

    assert "method=playerContent" in caplog.text
    assert "flag='备用线'" in caplog.text
    assert "id='/play/1'" in caplog.text
    assert "vipFlags=[]" in caplog.text


def test_spider_controller_keeps_numbered_legacy_routes_as_flat_playlists() -> None:
    class GroupedRouteSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "红果短剧",
                        "vod_play_from": "解析1$$$百度1$$$百度2$$$夸克1$$$夸克2$$$夸克3$$$磁力1",
                        "vod_play_url": (
                            "第1集$http://parse/1.m3u8"
                            "$$$第1集$http://baidu1/1.m3u8"
                            "$$$第1集$http://baidu2/1.m3u8"
                            "$$$第1集$http://quark1/1.m3u8"
                            "$$$第1集$http://quark2/1.m3u8"
                            "$$$第1集$http://quark3/1.m3u8"
                            "$$$磁力1$magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                        ),
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):
            return {"parse": 0, "url": id}

    controller = SpiderPluginController(GroupedRouteSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")

    assert request.source_groups == []
    assert request.source_group_index == 0
    assert request.source_index == 0
    assert len(request.playlists) == 7


def test_spider_controller_keeps_spaced_legacy_routes_as_flat_playlists() -> None:
    class LegacyRouteSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "电影",
                        "vod_play_from": "播放源 1$$$播放源 2",
                        "vod_play_url": "正片$http://a/1.m3u8$$$正片$http://b/1.m3u8",
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):
            return {"parse": 0, "url": id}

    controller = SpiderPluginController(LegacyRouteSpider(), plugin_name="电影", search_enabled=True)
    request = controller.build_request("detail-1")

    assert request.source_groups == []
    assert len(request.playlists) == 2
    assert [playlist[0].play_source for playlist in request.playlists] == ["播放源 1", "播放源 2"]


def test_spider_controller_does_not_infer_secondary_groups_from_legacy_routes() -> None:
    class GroupedRouteSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "红果短剧",
                        "vod_play_from": "解析1$$$百度1$$$百度2",
                        "vod_play_url": (
                            "第1集$http://parse/1.m3u8"
                            "$$$第1集$http://baidu1/1.m3u8"
                            "$$$第1集$http://baidu2/1.m3u8"
                        ),
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):
            return {"parse": 0, "url": id}

    controller = SpiderPluginController(GroupedRouteSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")

    assert request.source_groups == []
    assert len(request.playlists) == 3
    assert [playlist[0].play_source for playlist in request.playlists] == ["解析1", "百度1", "百度2"]


def test_spider_controller_uses_detail_group_payload_when_present() -> None:
    class GroupPayloadSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "红果短剧",
                        "vod_play_from": "旧线路",
                        "vod_play_url": "第1集$http://legacy/1.m3u8",
                        "group": [
                            {
                                "name": "百度",
                                "media": [
                                    {"name": "影视标题1", "url": "https://pan.baidu.com/s/xxx"},
                                ],
                            },
                            {
                                "name": "夸克",
                                "media": [
                                    {"name": "影视标题10", "url": "https://pan.quark.cn/s/yyy"},
                                ],
                            },
                        ],
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):
            return {"parse": 0, "url": id}

    controller = SpiderPluginController(GroupPayloadSpider(), plugin_name="红果短剧", search_enabled=True)
    request = controller.build_request("detail-1")

    assert [group.label for group in request.source_groups] == ["百度", "夸克"]
    assert [source.label for source in request.source_groups[0].sources] == ["影视标题1"]
    assert [source.label for source in request.source_groups[1].sources] == ["影视标题10"]
    assert len(request.playlists) == 2
    assert request.playlists[0][0].title == "影视标题1"
    assert request.playlists[0][0].url == ""
    assert request.playlists[0][0].vod_id == "https://pan.baidu.com/s/xxx"


def test_spider_controller_falls_back_to_legacy_routes_when_group_payload_is_invalid() -> None:
    class InvalidGroupSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "电影",
                        "vod_play_from": "备用线$$$极速线",
                        "vod_play_url": "正片$http://a/1.m3u8$$$正片$http://b/1.m3u8",
                        "group": [
                            {"name": "百度", "media": []},
                            {"name": "", "media": [{"name": "坏数据", "url": "https://pan.baidu.com/s/bad"}]},
                        ],
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):
            return {"parse": 0, "url": id}

    controller = SpiderPluginController(InvalidGroupSpider(), plugin_name="电影", search_enabled=True)
    request = controller.build_request("detail-1")

    assert request.source_groups == []
    assert request.playlists[0][0].url == "http://a/1.m3u8"
    assert request.playlists[1][0].url == "http://b/1.m3u8"


def test_spider_controller_maps_direct_media_urls_from_group_payload() -> None:
    class DirectMediaGroupSpider:
        def detailContent(self, ids):
            return {
                "list": [
                    {
                        "vod_id": ids[0],
                        "vod_name": "纪录片",
                        "group": [
                            {
                                "name": "直链",
                                "media": [
                                    {"name": "正片", "url": "https://media.example/movie.m3u8"},
                                ],
                            }
                        ],
                    }
                ]
            }

        def playerContent(self, flag, id, vipFlags):
            return {"parse": 0, "url": id}

    controller = SpiderPluginController(DirectMediaGroupSpider(), plugin_name="纪录片", search_enabled=True)
    request = controller.build_request("detail-1")

    assert request.playlists[0][0].url == "https://media.example/movie.m3u8"
    assert request.playlists[0][0].vod_id == ""
    assert request.playlists[0][0].title == "正片"


def test_prefetch_next_episode_danmaku_skips_when_should_not_prefetch() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)
    e1 = PlayItem(title="1", url="https://example.com/e1.mp4")
    e2 = PlayItem(title="2", url="https://example.com/e2.mp4")
    captured: list[tuple] = []
    controller._maybe_resolve_danmaku = lambda *args, **kwargs: captured.append((args, kwargs))

    controller.prefetch_next_episode_danmaku(e2, [e1, e2])

    assert captured == []


def test_prefetch_next_episode_danmaku_skips_when_url_and_vod_id_blank() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)
    e1 = PlayItem(title="第1集", url="https://example.com/e1.mp4")
    e2 = PlayItem(title="第2集", url="", vod_id="")
    captured: list[tuple] = []
    controller._maybe_resolve_danmaku = lambda *args, **kwargs: captured.append((args, kwargs))

    controller.prefetch_next_episode_danmaku(e2, [e1, e2])

    assert captured == []


def test_prefetch_next_episode_danmaku_invokes_resolver_when_eligible() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)
    e1 = PlayItem(title="第1集", url="https://example.com/e1.mp4")
    e2 = PlayItem(title="第2集", url="https://example.com/e2.mp4")
    playlist = [e1, e2]
    captured: list[tuple] = []
    controller._maybe_resolve_danmaku = lambda item, url, playlist=None, **kwargs: captured.append((item, url, playlist))

    controller.prefetch_next_episode_danmaku(e2, playlist)

    assert captured == [(e2, "https://example.com/e2.mp4", playlist)]


def test_prefetch_next_episode_danmaku_prefers_url_over_vod_id() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)
    e1 = PlayItem(title="第1集", url="https://example.com/e1.mp4")
    e2 = PlayItem(title="第2集", url="https://example.com/e2.mp4", vod_id="vod-2")
    captured: list[tuple] = []
    controller._maybe_resolve_danmaku = lambda item, url, playlist=None, **kwargs: captured.append((item, url, playlist))

    controller.prefetch_next_episode_danmaku(e2, [e1, e2])

    assert captured[0][1] == "https://example.com/e2.mp4"


def test_prefetch_next_episode_danmaku_falls_back_to_vod_id() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)
    e1 = PlayItem(title="第1集", url="https://example.com/e1.mp4")
    e2 = PlayItem(title="第2集", url="", vod_id="vod-2")
    captured: list[tuple] = []
    controller._maybe_resolve_danmaku = lambda item, url, playlist=None, **kwargs: captured.append((item, url, playlist))

    controller.prefetch_next_episode_danmaku(e2, [e1, e2])

    assert captured[0][1] == "vod-2"


def test_set_danmaku_log_handler_stores_callable() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)

    controller._log_danmaku_event("测试事件")

    assert logs == ["测试事件"]


def test_log_danmaku_event_includes_item_title_and_detail() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)
    item = PlayItem(title="第3集", url="https://example.com/e3.mp4")

    controller._log_danmaku_event("弹幕下载中", item, detail="腾讯视频")

    assert logs == ["弹幕下载中: 第3集: 腾讯视频"]


def test_log_danmaku_event_falls_back_to_media_title() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)
    item = PlayItem(title="", url="x", media_title="剧名")

    controller._log_danmaku_event("弹幕预下载中", item)

    assert logs == ["弹幕预下载中: 剧名"]


def test_log_danmaku_event_noop_when_handler_is_none() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)

    controller._log_danmaku_event("测试")  # should not raise


def test_log_danmaku_event_swallows_handler_exceptions() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)

    def raise_handler(message: str) -> None:
        raise RuntimeError("boom")

    controller.set_danmaku_log_handler(raise_handler)
    controller._log_danmaku_event("测试")  # should not raise


def test_prefetch_next_episode_danmaku_emits_prefetch_start_log() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)
    e1 = PlayItem(title="第1集", url="https://example.com/e1.mp4", media_title="凡人修仙传")
    e2 = PlayItem(title="第2集", url="https://example.com/e2.mp4", media_title="凡人修仙传")
    controller._maybe_resolve_danmaku = lambda *args, **kwargs: None

    controller.prefetch_next_episode_danmaku(e2, [e1, e2])

    assert logs == ["弹幕预下载中: 凡人修仙传 2集"]


def test_resolve_play_item_existing_media_url_passes_session_playlist_to_danmaku() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True, danmaku_service=object())
    request = controller.build_request("/detail/1")
    captured: list[tuple[PlayItem, str, list[PlayItem] | None]] = []
    controller._maybe_resolve_danmaku = lambda item, url, playlist=None, **kwargs: captured.append((item, url, playlist))

    assert request.playback_loader is not None
    direct_item = request.playlist[1]
    request.playback_loader(direct_item)

    assert captured == [(direct_item, "https://media.example/2.m3u8", request.playlist)]


def test_prefetch_next_episode_danmaku_skips_log_when_already_resolved() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)
    logs: list[str] = []
    controller.set_danmaku_log_handler(logs.append)
    e1 = PlayItem(title="第1集", url="https://example.com/e1.mp4")
    e2 = PlayItem(title="第2集", url="https://example.com/e2.mp4", danmaku_xml="<i></i>")
    controller._maybe_resolve_danmaku = lambda *args, **kwargs: None

    controller.prefetch_next_episode_danmaku(e2, [e1, e2])

    assert logs == []


def test_prefetch_next_episode_danmaku_skips_when_cached_xml_already_exists(
    monkeypatch,
    tmp_path,
) -> None:
    class DanmakuEnabledFakeSpider(FakeSpider):
        def danmaku(self):
            return True

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">cached</d></i>'
    danmaku_cache_module.save_cached_danmaku_xml("凡人修仙传 5集", "https://example.com/e5.mp4", xml_text)

    controller = SpiderPluginController(
        DanmakuEnabledFakeSpider(),
        plugin_name="测试",
        search_enabled=True,
        danmaku_service=object(),
    )
    logs: list[str] = []
    captured: list[tuple] = []
    controller.set_danmaku_log_handler(logs.append)
    controller._maybe_resolve_danmaku = lambda *args, **kwargs: captured.append((args, kwargs))
    e1 = PlayItem(title="第4集", url="https://example.com/e4.mp4", media_title="凡人修仙传")
    e2 = PlayItem(title="第5集", url="https://example.com/e5.mp4", media_title="凡人修仙传")

    controller.prefetch_next_episode_danmaku(e2, [e1, e2])

    assert e2.danmaku_xml == xml_text
    assert logs == []
    assert captured == []


def test_prefetch_next_episode_danmaku_reuses_cached_candidate_xml_across_plugins_without_duplicate_logs(
    monkeypatch,
    tmp_path,
) -> None:
    class DanmakuEnabledFakeSpider(FakeSpider):
        def danmaku(self):
            return True

    monkeypatch.setattr(danmaku_cache_module, "app_cache_dir", lambda: tmp_path / "app-cache")
    monkeypatch.setattr(controller_module, "load_cached_danmaku_xml", danmaku_cache_module.load_cached_danmaku_xml)
    monkeypatch.setattr(controller_module, "save_cached_danmaku_xml", danmaku_cache_module.save_cached_danmaku_xml)
    monkeypatch.setattr(
        controller_module,
        "load_cached_danmaku_source_search_result",
        danmaku_cache_module.load_cached_danmaku_source_search_result,
    )
    monkeypatch.setattr(
        controller_module,
        "save_cached_danmaku_source_search_result",
        danmaku_cache_module.save_cached_danmaku_source_search_result,
    )
    xml_text = '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">cached-prefetch-by-candidate-page-url</d></i>'
    query_name = "盗妖行 5集"
    page_url = "https://www.bilibili.com/video/BVprefetch5"
    store = DanmakuSeriesPreferenceStore(tmp_path / "danmaku-series.json")
    store.save(
        controller_module.DanmakuSeriesPreference(
            series_key=build_danmaku_series_key("盗妖行"),
            provider="bilibili",
            page_url="https://www.bilibili.com/video/BVprefetch4",
            title="《盗妖行》第4话 啥！啥！这是啥啊！",
            search_title="盗妖行",
        )
    )
    danmaku_cache_module.save_cached_danmaku_source_search_result(
        query_name,
        "",
        DanmakuSourceSearchResult(
            groups=[
                DanmakuSourceGroup(
                    provider="bilibili",
                    provider_label="B站",
                    options=[
                        DanmakuSourceOption(
                            provider="bilibili",
                            name="《盗妖行》第5话 回娘家",
                            url=page_url,
                        )
                    ],
                )
            ],
            default_option_url=page_url,
            default_provider="bilibili",
        ),
    )
    danmaku_cache_module.save_cached_danmaku_xml(query_name, page_url, xml_text)

    class NoNetworkDanmakuService:
        def rerank_danmaku_source_search_result(self, result, **kwargs):
            return result

        def search_danmu_sources(self, *args, **kwargs):
            raise AssertionError("should reuse cached candidate xml before prefetch search")

        def search_danmu(self, *args, **kwargs):
            raise AssertionError("should reuse cached candidate xml before prefetch legacy search")

        def resolve_danmu(self, *args, **kwargs):
            raise AssertionError("should reuse cached candidate xml before prefetch resolve")

    controller = SpiderPluginController(
        DanmakuEnabledFakeSpider(),
        plugin_name="盗妖行",
        search_enabled=True,
        danmaku_service=NoNetworkDanmakuService(),
        danmaku_preference_store=store,
    )
    logs: list[str] = []
    captured: list[tuple] = []
    controller.set_danmaku_log_handler(logs.append)
    controller._maybe_resolve_danmaku = lambda *args, **kwargs: captured.append((args, kwargs))
    e4 = PlayItem(title="第4集", url="https://example.com/e4.mp4", media_title="盗妖行")
    e5 = PlayItem(title="第5集", url="http://192.168.50.60:4567/p/web/1@111721", media_title="盗妖行")

    controller.prefetch_next_episode_danmaku(e5, [e4, e5])

    assert e5.danmaku_xml == xml_text
    assert e5.selected_danmaku_provider == "bilibili"
    assert e5.selected_danmaku_url == page_url
    assert logs == []
    assert captured == []


def test_prefetch_next_episode_danmaku_passes_is_prefetch_to_maybe_resolve() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="测试", search_enabled=True)
    captured: list[dict] = []
    controller._maybe_resolve_danmaku = lambda item, url, playlist=None, **kwargs: captured.append(kwargs)
    e1 = PlayItem(title="第1集", url="https://example.com/e1.mp4")
    e2 = PlayItem(title="第2集", url="https://example.com/e2.mp4")

    controller.prefetch_next_episode_danmaku(e2, [e1, e2])

    assert captured == [{"is_prefetch": True}]


def test_count_danmaku_entries_counts_d_elements() -> None:
    xml = '<?xml version="1.0"?><i><d p="0,1,25,16777215,0,0,0,0">a</d><d p="1,1,25,16777215,0,0,0,0">b</d></i>'

    assert _count_danmaku_entries(xml) == 2


def test_count_danmaku_entries_handles_empty_or_missing_d() -> None:
    assert _count_danmaku_entries("") == 0
    assert _count_danmaku_entries("<i></i>") == 0


def test_count_danmaku_entries_ignores_similar_tags() -> None:
    xml = "<i><dialog>x</dialog><d p='1'>y</d></i>"

    assert _count_danmaku_entries(xml) == 1


def test_spider_plugin_request_exposes_metadata_hydrator_when_plugin_danmaku_is_enabled() -> None:
    hydrator = object()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        metadata_hydrator_factory=lambda **_: hydrator,
    )

    request = controller.build_request("/detail/1")

    assert request.metadata_hydrator is hydrator


def test_spider_plugin_request_disables_metadata_hydrator_when_plugin_danmaku_is_disabled() -> None:
    hydrator = object()
    controller = SpiderPluginController(
        FakeSpider(),
        plugin_name="红果短剧",
        search_enabled=True,
        metadata_hydrator_factory=lambda **_: hydrator,
    )

    request = controller.build_request("/detail/1")

    assert request.metadata_hydrator is None


def test_spider_plugin_short_drama_collection_disables_metadata_and_danmaku() -> None:
    hydrator = object()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="短剧合集",
        search_enabled=True,
        danmaku_service=object(),
        metadata_hydrator_factory=lambda **_: hydrator,
    )
    request = controller.build_request("/detail/1")

    assert request.metadata_hydrator is None
    assert request.metadata_scrape_service is None
    assert request.episode_title_enhancer is None
    assert request.danmaku_controller is None


def test_spider_plugin_request_seeds_original_titles_for_playlist_items() -> None:
    controller = SpiderPluginController(FakeSpider(), plugin_name="红果短剧", search_enabled=True)

    request = controller.build_request("/detail/1")

    assert [item.original_title for item in request.playlist] == ["第1集", "第2集"]


def test_plugin_metadata_provider_maps_custom_metadata_payload() -> None:
    payload = {
        "id": "plugin:1",
        "title": "插件标题",
        "overview": "插件简介",
        "rating": "9.3",
        "imdb_id": "tt1234567",
        "detail_fields": [{"label": "别名", "value": "深空彼岸"}],
    }

    record = CustomPluginProvider().record_from_payload(payload)

    assert record.provider == "plugin"
    assert record.provider_id == "plugin:1"
    assert record.title == "插件标题"
    assert record.overview == "插件简介"
    assert record.rating == "9.3"
    assert record.imdb_id == "tt1234567"


def test_extract_episode_label_uses_variety_issue_for_plugin_variety_item() -> None:
    # Plugin sources compose the danmaku query from _extract_episode_label; a
    # variety filename must yield a date+issue+part label (not "N集"), otherwise
    # the service's variety branch has no issue key to match on.
    item = PlayItem(
        title="2026.08.10-第2期上.mp4",
        url="http://host/1",
        media_title="心动的信号 第9季",
        type_name="真人秀",
    )

    assert controller_module._extract_episode_label(item, [item]) == "20260810 第2期上"


def test_extract_episode_label_uses_metadata_type_when_filename_is_generic() -> None:
    # Filename carries no 第N期 marker; the 综艺 genre from metadata is what
    # routes it onto the variety path.
    item = PlayItem(title="2026.08.10.mp4", url="http://host/1", media_title="节目", type_name="综艺")

    assert controller_module._extract_episode_label(item, [item]) == "20260810"


def test_extract_episode_label_keeps_episode_numbering_for_dated_drama_files() -> None:
    # A bare air date in an ordinary episode filename must NOT trigger the
    # variety path (regression guard for dated anime/drama filenames).
    item = PlayItem(
        title="04-第4话 啥！啥！这是啥啊！-1080P 高码率-HEVC-2026-03-03",
        url="http://host/1",
        media_title="盗妖行",
    )

    assert controller_module._extract_episode_label(item, [item]) == "4集"


def test_extract_episode_label_falls_back_to_regular_numbering_for_variety_collection() -> None:
    # 综艺合集用常规集命名（"第N集"，无期号/完整日期）时集数不能为空：空集数会把
    # 弹幕搜索退化成纯标题查询，候选被时长过滤清空，弹幕源对话框也填不出集数。
    playlist = [
        PlayItem(
            title=f"第{index:02d}集",
            url=f"http://host/{index}",
            media_title="奔跑吧",
            type_name="综艺",
            index=index - 1,
        )
        for index in range(1, 4)
    ]

    assert controller_module._extract_episode_label(playlist[0], playlist) == "1集"


def test_extract_episode_label_keeps_hint_only_variety_titles_without_episode() -> None:
    # 文件名只有花絮提示词（纯享/加更/直拍…）而无期号/日期：保持无集数标签，
    # 按播放位置硬推集数会指错期。
    item = PlayItem(title="纯享版", url="http://host/1", media_title="奔跑吧", type_name="综艺")
    playlist = [item, PlayItem(title="第2期", url="http://host/2", media_title="奔跑吧", type_name="综艺")]

    assert controller_module._extract_episode_label(item, playlist) == ""


def test_metadata_danmaku_preference_pins_provider_from_official_link() -> None:
    controller = SpiderPluginController(DanmakuEnabledFakeSpider(), plugin_name="p", search_enabled=True)
    item = PlayItem(
        title="第1集",
        url="http://host/1",
        media_title="心动的信号 第9季",
        metadata_provider_url="https://v.qq.com/x/cover/mzc002003kpyd2m/w4102gzejm3.html",
    )

    preference = controller._metadata_danmaku_preference(item, "series-key")

    assert preference is not None
    assert preference.provider == "tencent"
    assert preference.page_url == "https://v.qq.com/x/cover/mzc002003kpyd2m/w4102gzejm3.html"
    assert preference.search_title == ""


def test_metadata_danmaku_preference_returns_none_without_usable_url() -> None:
    controller = SpiderPluginController(DanmakuEnabledFakeSpider(), plugin_name="p", search_enabled=True)
    bare = PlayItem(title="第1集", url="http://host/1", media_title="剧")
    unknown = PlayItem(
        title="第1集",
        url="http://host/1",
        media_title="剧",
        metadata_provider_url="https://example.com/watch/1",
    )

    assert controller._metadata_danmaku_preference(bare, "k") is None
    assert controller._metadata_danmaku_preference(unknown, "k") is None


def test_controller_does_not_cache_other_fallback_only_danmaku_search(monkeypatch) -> None:
    class OtherFallbackService:
        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ):
            del name, preferred_provider, preferred_page_url, media_duration_seconds, provider_filter
            return DanmakuSourceSearchResult(
                groups=[
                    DanmakuSourceGroup(
                        provider="other",
                        provider_label="other",
                        options=[DanmakuSourceOption(provider="other", name="冷门剧 1集", url=reg_src)],
                    )
                ],
                default_option_url=reg_src,
                default_provider="other",
            )

    saved: list[str] = []
    monkeypatch.setattr(controller_module, "load_cached_danmaku_source_search_result", lambda *_args: None)
    monkeypatch.setattr(
        controller_module,
        "_save_cached_danmaku_source_search_result_variants",
        lambda name, reg_src, result: saved.append(name),
    )
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="网盘资源",
        search_enabled=True,
        danmaku_service=OtherFallbackService(),
    )
    item = PlayItem(
        title="第1集",
        url="https://stream.example/play/1.m3u8",
        media_title="冷门剧",
        vod_id="https://v.qq.com/x/cover/xyz/abc.html",
    )

    controller.refresh_danmaku_sources(item, playlist=[item])

    # 纯 other 兜底结果不写共享缓存，但当前条目仍可兜底展示候选。
    assert saved == []
    assert [group.provider for group in item.danmaku_candidates] == ["other"]


def test_controller_rejects_stale_other_fallback_cached_danmaku_sources(monkeypatch) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.search_calls: list[str] = []

        def search_danmu_sources(
            self,
            name: str,
            reg_src: str = "",
            preferred_provider: str = "",
            preferred_page_url: str = "",
            media_duration_seconds: int = 0,
            provider_filter: str = "",
        ):
            del reg_src, preferred_provider, preferred_page_url, media_duration_seconds, provider_filter
            self.search_calls.append(name)
            return DanmakuSourceSearchResult(groups=[], default_option_url="", default_provider="")

    poisoned = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="other",
                provider_label="other",
                options=[DanmakuSourceOption(provider="other", name="冷门剧 1集", url="https://pan.baidu.com/s/1ZuAV?pwd=9527")],
            )
        ],
        default_option_url="https://pan.baidu.com/s/1ZuAV?pwd=9527",
        default_provider="other",
    )
    monkeypatch.setattr(controller_module, "load_cached_danmaku_source_search_result", lambda *_args: poisoned)
    monkeypatch.setattr(controller_module, "_save_cached_danmaku_source_search_result_variants", lambda *_args: None)
    service = RecordingService()
    controller = SpiderPluginController(
        PluginLevelDanmakuSpider(),
        plugin_name="网盘资源",
        search_enabled=True,
        danmaku_service=service,
    )
    item = PlayItem(
        title="第1集",
        url="https://stream.example/play/1.m3u8",
        media_title="冷门剧",
        vod_id="/play/1",
    )

    controller.refresh_danmaku_sources(item, playlist=[item])

    # 历史污染缓存（other 候选指向别的 reg_src）被拒用，回退到新搜索。
    assert service.search_calls == ["冷门剧 1集"]
