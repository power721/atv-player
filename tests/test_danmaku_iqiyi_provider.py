import json
import zlib

import pytest

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.providers.iqiyi import IqiyiDanmakuProvider


class JsonResponse:
    def __init__(self, payload=None, text: str = "", status_code: int = 200, content: bytes = b"") -> None:
        self._payload = payload
        self.text = text
        self.status_code = status_code
        self.content = content

    def json(self):
        return self._payload


def test_iqiyi_search_filters_noise_and_returns_episode_candidates() -> None:
    def fake_get(url: str, **kwargs):
        assert url == "https://search.video.iqiyi.com/o"
        assert kwargs["params"]["key"] == "剑来"
        return JsonResponse(
            {
                "data": {
                    "docinfos": [
                        {
                            "albumDocInfo": {
                                "douban_score": 8.1,
                                "channel": "教育",
                                "itemTotalNumber": 12,
                                "albumTitle": "剑来",
                                "videoinfos": [
                                    {"itemTitle": "剑来 第1集", "itemLink": "https://www.iqiyi.com/v_noise.html"}
                                ],
                            }
                        },
                        {
                            "albumDocInfo": {
                                "douban_score": 8.1,
                                "channel": "动漫",
                                "itemTotalNumber": 12,
                                "albumTitle": "剑来 精彩片段",
                                "videoinfos": [
                                    {"itemTitle": "剑来 花絮", "itemLink": "https://www.iqiyi.com/v_clip.html"}
                                ],
                            }
                        },
                        {
                            "albumDocInfo": {
                                "douban_score": 8.6,
                                "channel": "动漫",
                                "itemTotalNumber": 12,
                                "albumTitle": "剑来",
                                "videoinfos": [
                                    {"itemTitle": "剑来 第1集", "itemLink": "https://www.iqiyi.com/v_19rr1lm35o.html"},
                                    {"itemTitle": "剑来 第2集", "itemLink": "https://www.iqiyi.com/v_19rr1lm35p.html"},
                                ],
                            }
                        },
                    ]
                }
            }
        )

    provider = IqiyiDanmakuProvider(get=fake_get)

    items = provider.search("剑来")

    assert [(item.name, item.url) for item in items] == [
        ("剑来 第1集", "https://www.iqiyi.com/v_19rr1lm35o.html"),
        ("剑来 第2集", "https://www.iqiyi.com/v_19rr1lm35p.html"),
    ]


def test_iqiyi_search_raises_for_invalid_payload() -> None:
    provider = IqiyiDanmakuProvider(get=lambda url, **kwargs: JsonResponse({"oops": 1}))

    with pytest.raises(DanmakuSearchError, match="爱奇艺弹幕搜索结果解析失败"):
        provider.search("剑来")


def test_iqiyi_search_keeps_episode_items_when_album_score_is_missing() -> None:
    def fake_get(url: str, **kwargs):
        return JsonResponse(
            {
                "data": {
                    "docinfos": [
                        {
                            "albumDocInfo": {
                                "channel": "电视剧",
                                "itemTotalNumber": 36,
                                "albumTitle": "八千里路云和月",
                            },
                            "videoinfos": [
                                {
                                    "itemTitle": "八千里路云和月第10集",
                                    "itemNumber": 10,
                                    "itemLink": "http://www.iqiyi.com/v_kjnf5f02xg.html",
                                }
                            ],
                        }
                    ]
                }
            }
        )

    provider = IqiyiDanmakuProvider(get=fake_get)

    items = provider.search("八千里路云和月")

    assert [(item.name, item.url) for item in items] == [
        ("八千里路云和月第10集", "https://www.iqiyi.com/v_kjnf5f02xg.html")
    ]


def test_iqiyi_search_expands_album_link_when_search_result_skips_middle_episodes() -> None:
    album_page = """
    <html><body>
    <input type="hidden" id="album-avlist-data" value='{"albumId":"6421036798758301","hasMore":false,"epsodelist":[
        {"order":1,"tvId":3023864436566800,"shortTitle":"八千里路云和月第1集","playUrl":"http://www.iqiyi.com/v_twylt9v918.html","duration":"46:02"},
        {"order":14,"tvId":7033140000000014,"shortTitle":"八千里路云和月第14集","playUrl":"http://www.iqiyi.com/v_target14.html","duration":"45:10"},
        {"order":40,"tvId":7033140000000040,"shortTitle":"八千里路云和月第40集","playUrl":"http://www.iqiyi.com/v_last40.html","duration":"45:01"}
    ]}'/>
    </body></html>
    """

    def fake_get(url: str, **kwargs):
        if url == "https://search.video.iqiyi.com/o":
            return JsonResponse(
                {
                    "data": {
                        "docinfos": [
                            {
                                "albumDocInfo": {
                                    "albumId": 6421036798758301,
                                    "channel": "电视剧,2",
                                    "itemTotalNumber": 40,
                                    "albumTitle": "八千里路云和月",
                                    "albumLink": "http://www.iqiyi.com/a_1qzrer2eqcx.html",
                                },
                                "videoinfos": [
                                    {"itemTitle": "八千里路云和月第1集", "itemNumber": 1, "itemLink": "http://www.iqiyi.com/v_twylt9v918.html"},
                                    {"itemTitle": "八千里路云和月第10集", "itemNumber": 10, "itemLink": "http://www.iqiyi.com/v_kjnf5f02xg.html"},
                                    {"itemTitle": "八千里路云和月第31集", "itemNumber": 31, "itemLink": "http://www.iqiyi.com/v_163w0yrpbso.html"},
                                    {"itemTitle": "八千里路云和月第40集", "itemNumber": 40, "itemLink": "http://www.iqiyi.com/v_1lzng74uft4.html"},
                                ],
                            }
                        ]
                    }
                }
            )
        if url == "https://www.iqiyi.com/a_1qzrer2eqcx.html?jump=0":
            return JsonResponse(text=album_page)
        raise AssertionError(f"Unexpected URL: {url}")

    provider = IqiyiDanmakuProvider(get=fake_get)

    items = provider.search("八千里路云和月")

    assert ("八千里路云和月第14集", "https://www.iqiyi.com/v_target14.html") in [
        (item.name, item.url) for item in items
    ]


def test_iqiyi_search_expands_album_link_via_album_avlist_api_config() -> None:
    album_page = """
    <html><body>
    <input type="hidden" id="album-avlist-data" value='{"key":"albumAvlist","urlParam":"/albums/album/avlistinfo?aid=6421036798758301&page=1&size=100"}'/>
    </body></html>
    """
    avlist_payload = {
        "data": {
            "epsodelist": [
                {
                    "order": 14,
                    "tvId": 7033140000000014,
                    "shortTitle": "八千里路云和月第14集",
                    "playUrl": "http://www.iqiyi.com/v_target14.html",
                    "duration": "45:10",
                }
            ]
        }
    }

    def fake_get(url: str, **kwargs):
        if url == "https://search.video.iqiyi.com/o":
            return JsonResponse(
                {
                    "data": {
                        "docinfos": [
                            {
                                "albumDocInfo": {
                                    "albumId": 6421036798758301,
                                    "channel": "电视剧,2",
                                    "itemTotalNumber": 40,
                                    "albumTitle": "八千里路云和月",
                                    "albumLink": "http://www.iqiyi.com/a_1qzrer2eqcx.html",
                                },
                                "videoinfos": [
                                    {"itemTitle": "八千里路云和月第1集", "itemNumber": 1, "itemLink": "http://www.iqiyi.com/v_twylt9v918.html"},
                                    {"itemTitle": "八千里路云和月第10集", "itemNumber": 10, "itemLink": "http://www.iqiyi.com/v_kjnf5f02xg.html"},
                                    {"itemTitle": "八千里路云和月第31集", "itemNumber": 31, "itemLink": "http://www.iqiyi.com/v_163w0yrpbso.html"},
                                    {"itemTitle": "八千里路云和月第40集", "itemNumber": 40, "itemLink": "http://www.iqiyi.com/v_1lzng74uft4.html"},
                                ],
                            }
                        ]
                    }
                }
            )
        if url == "https://www.iqiyi.com/a_1qzrer2eqcx.html?jump=0":
            return JsonResponse(text=album_page)
        if url == "https://www.iqiyi.com/albums/album/avlistinfo?aid=6421036798758301&page=1&size=100":
            return JsonResponse(avlist_payload)
        raise AssertionError(f"Unexpected URL: {url}")

    provider = IqiyiDanmakuProvider(get=fake_get)

    items = provider.search("八千里路云和月")

    assert ("八千里路云和月第14集", "https://www.iqiyi.com/v_target14.html") in [
        (item.name, item.url) for item in items
    ]


def test_iqiyi_resolve_falls_back_to_cached_search_metadata_when_page_lacks_play_page_info() -> None:
    segment = zlib.compress(
        (
            "<root>"
            "<bulletInfoList>"
            "<bulletInfo><showTime>1500</showTime><content>缓存元数据解析</content><color>255</color></bulletInfo>"
            "</bulletInfoList>"
            "</root>"
        ).encode("utf-8")
    )

    def fake_get(url: str, **kwargs):
        if "search.video.iqiyi.com/o" in url:
            return JsonResponse(
                {
                    "data": {
                        "docinfos": [
                            {
                                "albumDocInfo": {
                                    "channel": "电视剧,2",
                                    "itemTotalNumber": 36,
                                    "albumTitle": "八千里路云和月",
                                },
                                "videoinfos": [
                                    {
                                        "itemTitle": "八千里路云和月第10集",
                                        "itemNumber": 10,
                                        "itemLink": "http://www.iqiyi.com/v_20imo31bths.html",
                                        "tvId": 123456789000,
                                        "albumId": 6421036798758301,
                                    }
                                ],
                            }
                        ]
                    }
                }
            )
        if url == "https://www.iqiyi.com/v_20imo31bths.html":
            return JsonResponse(text="<html><head><title>shell page</title></head><body></body></html>")
        if url.endswith("123456789000_300_1.z"):
            assert kwargs["params"]["categoryid"] == 2
            assert kwargs["params"]["albumid"] == 6421036798758301
            return JsonResponse(content=segment)
        raise AssertionError(f"Unexpected URL: {url}")

    provider = IqiyiDanmakuProvider(get=fake_get)
    items = provider.search("八千里路云和月")

    records = provider.resolve(items[0].url)

    assert [(record.time_offset, record.content, record.color) for record in records] == [
        (1.5, "缓存元数据解析", "255")
    ]


def test_iqiyi_resolve_uses_cached_duration_to_fetch_multiple_segments_when_page_is_shell() -> None:
    seen_urls: list[str] = []
    segment_1 = zlib.compress(
        (
            "<root><bulletInfoList>"
            "<bulletInfo><showTime>1000</showTime><content>第一页</content><color>255</color></bulletInfo>"
            "</bulletInfoList></root>"
        ).encode("utf-8")
    )
    segment_2 = zlib.compress(
        (
            "<root><bulletInfoList>"
            "<bulletInfo><showTime>301000</showTime><content>第二页</content><color>65280</color></bulletInfo>"
            "</bulletInfoList></root>"
        ).encode("utf-8")
    )

    def fake_get(url: str, **kwargs):
        seen_urls.append(url)
        if "search.video.iqiyi.com/o" in url:
            return JsonResponse(
                {
                    "data": {
                        "docinfos": [
                            {
                                "albumDocInfo": {
                                    "channel": "电视剧,2",
                                    "itemTotalNumber": 36,
                                    "albumTitle": "八千里路云和月",
                                },
                                "videoinfos": [
                                    {
                                        "itemTitle": "八千里路云和月第10集",
                                        "itemNumber": 10,
                                        "itemLink": "http://www.iqiyi.com/v_20imo31bths.html",
                                        "tvId": 3063170563116300,
                                        "albumId": 6421036798758301,
                                        "timeLength": 301,
                                    }
                                ],
                            }
                        ]
                    }
                }
            )
        if url == "https://www.iqiyi.com/v_20imo31bths.html":
            return JsonResponse(text="<html><body>shell only</body></html>")
        if url.endswith("3063170563116300_300_1.z"):
            return JsonResponse(content=segment_1)
        if url.endswith("3063170563116300_300_2.z"):
            return JsonResponse(content=segment_2)
        raise AssertionError(f"Unexpected URL: {url}")

    provider = IqiyiDanmakuProvider(get=fake_get)
    items = provider.search("八千里路云和月")

    records = provider.resolve(items[0].url)

    assert [record.content for record in records] == ["第一页", "第二页"]
    assert seen_urls == [
        "https://search.video.iqiyi.com/o",
        "https://www.iqiyi.com/v_20imo31bths.html",
        "https://cmts.iqiyi.com/bullet/63/00/3063170563116300_300_1.z",
        "https://cmts.iqiyi.com/bullet/63/00/3063170563116300_300_2.z",
    ]


def test_iqiyi_resolve_parses_page_info_downloads_segments_and_dedupes_records() -> None:
    calls: list[str] = []
    page_info = {
        "duration": "00:08:20",
        "tvName": "剑来 第1集",
        "albumId": 2024,
        "tvId": 987654321,
        "cid": 4,
    }
    segment_1 = zlib.compress(
        (
            "<danmu>"
            "<bulletInfo><showTime>1000</showTime><content>第一条</content><color>16777215</color><font>25</font></bulletInfo>"
            "<bulletInfo><showTime>2000</showTime><content>重复</content><color>255</color><font>18</font></bulletInfo>"
            "</danmu>"
        ).encode("utf-8")
    )
    segment_2 = zlib.compress(
        (
            "<danmu>"
            "<bulletInfo><showTime>2000</showTime><content>重复</content><color>255</color><font>18</font></bulletInfo>"
            "<bulletInfo><showTime>3500</showTime><content>第三条</content><color>65280</color><font>0</font></bulletInfo>"
            "</danmu>"
        ).encode("utf-8")
    )

    def fake_get(url: str, **kwargs):
        calls.append(url)
        if url == "https://www.iqiyi.com/v_19rr1lm35o.html":
            return JsonResponse(
                text=f'<html><script>window.Q.PageInfo.playPageInfo={json.dumps(page_info)};</script></html>'
            )
        if url.endswith("_300_1.z"):
            return JsonResponse(content=segment_1)
        if url.endswith("_300_2.z"):
            return JsonResponse(content=segment_2)
        raise AssertionError(f"Unexpected URL: {url}")

    provider = IqiyiDanmakuProvider(get=fake_get)

    records = provider.resolve("https://www.iqiyi.com/v_19rr1lm35o.html")

    assert [record.content for record in records] == ["第一条", "重复", "第三条"]
    assert [record.time_offset for record in records] == [1.0, 2.0, 3.5]
    assert [record.color for record in records] == ["16777215", "255", "65280"]
    assert calls == [
        "https://www.iqiyi.com/v_19rr1lm35o.html",
        "https://cmts.iqiyi.com/bullet/43/21/987654321_300_1.z",
        "https://cmts.iqiyi.com/bullet/43/21/987654321_300_2.z",
    ]


def test_iqiyi_resolve_treats_small_show_time_values_as_seconds_not_milliseconds() -> None:
    page_info = {
        "duration": "00:46:02",
        "tvName": "八千里路云和月 第17集",
        "albumId": 6421036798758301,
        "tvId": 3831645445180500,
        "cid": 2,
    }
    segment = zlib.compress(
        (
            "<danmu><data><entry><list>"
            "<bulletInfo><showTime>2</showTime><content>第二秒</content><color>ffffff</color></bulletInfo>"
            "<bulletInfo><showTime>175</showTime><content>一百七十五秒</content><color>FFFFFF</color></bulletInfo>"
            "</list></entry></data></danmu>"
        ).encode("utf-8")
    )

    def fake_get(url: str, **kwargs):
        if url == "https://www.iqiyi.com/v_demo_seconds.html":
            return JsonResponse(
                text=f'<html><script>window.Q.PageInfo.playPageInfo={json.dumps(page_info)};</script></html>'
            )
        return JsonResponse(content=segment)

    provider = IqiyiDanmakuProvider(get=fake_get)

    records = provider.resolve("https://www.iqiyi.com/v_demo_seconds.html")

    assert [(record.time_offset, record.content) for record in records[:2]] == [
        (2.0, "第二秒"),
        (175.0, "一百七十五秒"),
    ]


def test_iqiyi_resolve_raises_when_page_info_is_missing() -> None:
    provider = IqiyiDanmakuProvider(get=lambda url, **kwargs: JsonResponse(text="<html></html>"))

    with pytest.raises(DanmakuResolveError, match="爱奇艺页面缺少 playPageInfo"):
        provider.resolve("https://www.iqiyi.com/v_demo.html")


def test_iqiyi_resolve_raises_when_all_segments_fail_to_decompress() -> None:
    page_info = {
        "duration": "00:00:10",
        "tvName": "剑来 第1集",
        "albumId": 2024,
        "tvId": 987654321,
        "cid": 4,
    }

    def fake_get(url: str, **kwargs):
        if url == "https://www.iqiyi.com/v_demo.html":
            return JsonResponse(
                text=f'<html><script>window.Q.PageInfo.playPageInfo={json.dumps(page_info)};</script></html>'
            )
        return JsonResponse(content=b"not-zlib")

    provider = IqiyiDanmakuProvider(get=fake_get)

    with pytest.raises(DanmakuResolveError, match="爱奇艺弹幕分片解析失败"):
        provider.resolve("https://www.iqiyi.com/v_demo.html")


def test_iqiyi_resolve_supports_nested_play_page_data_and_bullet_info_list_payload() -> None:
    page_info = {
        "duration": "00:00:10",
        "tvName": "剑来 第1集",
        "playPageData": {
            "albumId": 2024,
            "tvId": 987654321,
            "cid": 4,
        },
    }
    segment = zlib.compress(
        (
            "<root>"
            "<bulletInfoList>"
            "<bulletInfo><showTime>1250</showTime><content>嵌套结构</content><color>16777215</color></bulletInfo>"
            "</bulletInfoList>"
            "</root>"
        ).encode("utf-8")
    )

    def fake_get(url: str, **kwargs):
        if url == "https://www.iqiyi.com/v_nested.html":
            return JsonResponse(
                text=(
                    "<html><script>"
                    f"window.Q.PageInfo.playPageInfo={json.dumps(page_info)};"
                    "</script></html>"
                )
            )
        return JsonResponse(content=segment)

    provider = IqiyiDanmakuProvider(get=fake_get)

    records = provider.resolve("https://www.iqiyi.com/v_nested.html")

    assert [(record.time_offset, record.content) for record in records] == [(1.25, "嵌套结构")]
