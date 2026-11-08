import httpx
import pytest
import hashlib
import json
import threading
import time

from atv_player.danmaku.errors import DanmakuResolveError
from atv_player.danmaku.providers.sohu import SohuDanmakuProvider


def test_sohu_search_filters_trailer_noise_and_expands_episode_candidates() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://m.so.tv.sohu.com/search/pc/keyword":
            assert kwargs["params"]["key"] == "剑来"
            assert kwargs["params"]["tabsChosen"] == "0"
            assert kwargs["params"]["page_size"] == "20"
            assert kwargs["headers"]["Referer"] == "https://tv.sohu.com/"
            assert kwargs["headers"]["Origin"] == "https://tv.sohu.com"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "aid": "noise-1",
                                "album_name": "剑来预告",
                                "is_trailer": 1,
                            },
                            {
                                "aid": "200001",
                                "album_name": "剑来",
                                "year": 2026,
                                "meta": [{"txt": "动漫 | 内地 | 2026年"}],
                            },
                        ]
                    }
                },
            )
        if url == "https://pl.hd.sohu.com/videolist":
            assert kwargs["params"]["playlistid"] == "200001"
            return httpx.Response(
                200,
                json={
                    "videos": [
                        {
                            "vid": "9001",
                            "video_name": "第1集",
                            "url_html5": "https://tv.sohu.com/v/dXMvOTAwMS8=.html",
                            "playLength": 1420,
                        },
                        {
                            "vid": "9002",
                            "video_name": "第2集",
                            "url_html5": "https://tv.sohu.com/v/dXMvOTAwMi8=.html",
                            "playLength": 1438,
                        },
                    ]
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)

    items = provider.search("剑来", original_name="剑来 2集")

    assert [(item.provider, item.name, item.url, item.duration_seconds) for item in items] == [
        ("sohu", "剑来 第2集", "https://tv.sohu.com/v/dXMvOTAwMi8=.html", 1438),
    ]
    assert items[0].resolve_context["aid"] == "200001"
    assert items[0].resolve_context["vid"] == "9002"


def test_sohu_search_parses_json_from_text_response_when_json_method_cannot_be_used() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://m.so.tv.sohu.com/search/pc/keyword":
            return httpx.Response(
                200,
                text=json.dumps(
                    {
                        "status": 200,
                        "data": {
                            "items": [
                                {
                                    "aid": "200001",
                                    "album_name": "剑来",
                                    "year": 2026,
                                    "meta": [{"txt": "动漫 | 内地 | 2026年"}],
                                    "videos": [
                                        {
                                            "vid": "9001",
                                            "video_name": "剑来第1集",
                                            "url_html5": "http://m.tv.sohu.com/v9001.shtml",
                                            "playLength": 1420,
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
            )
        if url == "https://pl.hd.sohu.com/videolist":
            raise AssertionError("embedded search videos should avoid playlist lookup")
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)

    items = provider.search("剑来", original_name="剑来 1集")

    assert [(item.name, item.url) for item in items] == [("剑来 第1集", "https://m.tv.sohu.com/v9001.shtml")]


def test_sohu_search_prefers_single_main_movie_candidate() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://m.so.tv.sohu.com/search/pc/keyword":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "aid": "300001",
                                "album_name": "疯狂动物城2",
                                "year": 2026,
                                "meta": [{"txt": "电影 | 美国 | 2026年"}],
                            }
                        ]
                    }
                },
            )
        if url == "https://pl.hd.sohu.com/videolist":
            return httpx.Response(
                200,
                json={
                    "videos": [
                        {
                            "vid": "movie-main",
                            "video_name": "疯狂动物城2",
                            "url_html5": "https://tv.sohu.com/v/movie-main.html",
                            "playLength": 5935,
                        },
                        {
                            "vid": "movie-trailer",
                            "video_name": "疯狂动物城2 预告片",
                            "url_html5": "https://tv.sohu.com/v/movie-trailer.html",
                            "playLength": 95,
                        },
                    ]
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)

    items = provider.search("疯狂动物城2")

    assert [(item.name, item.url, item.duration_seconds) for item in items] == [
        ("疯狂动物城2", "https://tv.sohu.com/v/movie-main.html", 5935),
    ]


def test_sohu_search_keeps_variety_issue_candidates() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://m.so.tv.sohu.com/search/pc/keyword":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "aid": "400001",
                                "album_name": "哈哈哈哈哈第6季",
                                "year": 2026,
                                "meta": [{"txt": "综艺 | 内地 | 2026年"}],
                            }
                        ]
                    }
                },
            )
        if url == "https://pl.hd.sohu.com/videolist":
            return httpx.Response(
                200,
                json={
                    "videos": [
                        {
                            "vid": "issue-0405",
                            "video_name": "20260405期 第1期下",
                            "url_html5": "https://tv.sohu.com/v/issue-0405.html",
                            "playLength": 5480,
                        },
                        {
                            "vid": "issue-0411",
                            "video_name": "20260411期 第2期上",
                            "url_html5": "https://tv.sohu.com/v/issue-0411.html",
                            "playLength": 5511,
                        },
                    ]
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)

    items = provider.search("哈哈哈哈哈第六季", original_name="哈哈哈哈哈第六季 20260411期 第2期上")

    assert [item.name for item in items] == ["哈哈哈哈哈第6季 20260411期 第2期上"]
    assert items[0].resolve_context["variety_year"] == "20260411"


def test_sohu_search_uses_page_url_when_playlist_lacks_url_html5() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://m.so.tv.sohu.com/search/pc/keyword":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "aid": "500001",
                                "album_name": "难哄",
                                "year": 2026,
                                "meta": [{"txt": "电视剧 | 内地 | 2026年"}],
                            }
                        ]
                    }
                },
            )
        if url == "https://pl.hd.sohu.com/videolist":
            return httpx.Response(
                200,
                json={
                    "videos": [
                        {
                            "vid": "310168357",
                            "video_name": "第1集",
                            "pageUrl": "http://tv.sohu.com/20260218/n310168357.shtml",
                            "playLength": 2710,
                        }
                    ]
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)

    items = provider.search("难哄", original_name="难哄 1集")

    assert [(item.name, item.url, item.duration_seconds) for item in items] == [
        ("难哄 第1集", "https://tv.sohu.com/20260218/n310168357.shtml", 2710),
    ]
    assert items[0].resolve_context["vid"] == "310168357"


def test_sohu_search_normalizes_first_season_titles_for_title_only_queries() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://m.so.tv.sohu.com/search/pc/keyword":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "aid": "9986818",
                                "album_name": "<<<都是她的错>>>第一季（All Her Fault Season 1）",
                                "year": 2025,
                                "meta": [{"txt": "电视剧 | 美国 | 2025年"}],
                                "videos": [
                                    {
                                        "aid": 9986818,
                                        "vid": 10305888,
                                        "video_name": "都是她的错第一季（All Her Fault Season 1）第1集",
                                        "video_order": 1,
                                        "url_html5": "http://m.tv.sohu.com/v10305888.shtml",
                                        "isFee": 0,
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        if url == "https://pl.hd.sohu.com/videolist":
            return httpx.Response(
                200,
                json={
                    "videos": [
                        {
                            "vid": "10305888",
                            "video_name": "都是她的错第一季（All Her Fault Season 1）第1集",
                            "url_html5": "http://m.tv.sohu.com/v10305888.shtml",
                            "playLength": 2710,
                        }
                    ]
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)

    items = provider.search("都是她的错", original_name="都是她的错 1集")

    assert [(item.name, item.url) for item in items] == [
        ("都是她的错 第1集", "https://m.tv.sohu.com/v10305888.shtml"),
    ]


def test_sohu_search_uses_embedded_search_videos_when_playlist_api_is_unavailable() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://m.so.tv.sohu.com/search/pc/keyword":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "aid": 9981602,
                                "album_name": "<<<谁动了我的隐私>>>",
                                "year": 2026,
                                "meta": [{"txt": "电视剧 | 内地 | 2026年"}],
                                "videos": [
                                    {
                                        "aid": 9981602,
                                        "vid": 10234237,
                                        "video_name": "谁动了我的隐私第1集",
                                        "video_order": 1,
                                        "url_html5": "http://m.tv.sohu.com/v10234237.shtml",
                                        "isFee": 0,
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        if url == "https://pl.hd.sohu.com/videolist":
            raise AssertionError("playlist API should not be required when search payload already includes videos")
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)

    items = provider.search("谁动了我的隐私", original_name="谁动了我的隐私 1集")

    assert [(item.name, item.url) for item in items] == [
        ("谁动了我的隐私 第1集", "https://m.tv.sohu.com/v10234237.shtml"),
    ]


def test_sohu_search_skips_cross_site_embedded_videos() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://m.so.tv.sohu.com/search/pc/keyword":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "aid": 1001352658,
                                "album_name": "<<<不识君>>>",
                                "year": 2025,
                                "meta": [
                                    {"txt": "24集全"},
                                    {"txt": "电视剧 | 内地 | 2025年"},
                                    {"txt": "播放源：爱奇艺"},
                                ],
                                "videos": [
                                    {
                                        "aid": 1001352658,
                                        "vid": 458728253,
                                        "video_name": "不识君第2集",
                                        "video_order": 2,
                                        "url_html5": "http://www.iqiyi.com/v_gjfsxj94o8.html",
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        if url == "https://pl.hd.sohu.com/videolist":
            raise AssertionError("embedded cross-site videos should not trigger fallback lookup")
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)

    items = provider.search("不识君", original_name="不识君 2集")

    assert items == []


def test_sohu_search_ignores_non_mapping_search_items_and_embedded_videos() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://m.so.tv.sohu.com/search/pc/keyword":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            "bad-item",
                            {
                                "aid": 9981602,
                                "album_name": "<<<谁动了我的隐私>>>",
                                "year": 2026,
                                "corner_mark": "bad-corner",
                                "meta": ["bad-meta", {"txt": "电视剧 | 内地 | 2026年"}],
                                "videos": [
                                    "bad-video",
                                    {
                                        "aid": 9981602,
                                        "vid": 10234237,
                                        "video_name": "谁动了我的隐私第1集",
                                        "video_order": 1,
                                        "url_html5": "http://m.tv.sohu.com/v10234237.shtml",
                                        "isFee": 0,
                                    },
                                ],
                            },
                        ]
                    }
                },
            )
        if url == "https://pl.hd.sohu.com/videolist":
            raise AssertionError("playlist API should not be required when embedded search videos exist")
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)

    items = provider.search("谁动了我的隐私", original_name="谁动了我的隐私 1集")

    assert [(item.name, item.url) for item in items] == [
        ("谁动了我的隐私 第1集", "https://m.tv.sohu.com/v10234237.shtml"),
    ]


def test_sohu_search_signs_request_with_fpc_timestamp_and_code() -> None:
    captured: dict[str, dict] = {}

    def fake_get(url: str, **kwargs):
        if url == "https://m.so.tv.sohu.com/search/pc/keyword":
            captured.update(kwargs["params"])
            return httpx.Response(200, json={"data": {"items": []}})
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)

    assert provider.search("剑来") == []

    params = captured
    assert params["fpc"] and params["timeStamp"]
    expected_code = hashlib.md5(
        f"{params['timeStamp']}{params['fpc']}vxWaXm3C5SA9&fpc".encode()
    ).hexdigest()
    assert params["code"] == expected_code


def test_sohu_search_accepts_video_only_items_without_album_fields() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://m.so.tv.sohu.com/search/pc/keyword":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "vid": "700001",
                                "video_name": "剑来第1集",
                                "year_name": "2026",
                            }
                        ]
                    }
                },
            )
        if url == "https://pl.hd.sohu.com/videolist":
            assert kwargs["params"]["playlistid"] == "700001"
            return httpx.Response(
                200,
                json={
                    "videos": [
                        {
                            "vid": "700001",
                            "name": "剑来第1集",
                            "pageUrl": "https://tv.sohu.com/v/700001.html",
                            "playLength": 1420,
                        }
                    ]
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)

    items = provider.search("剑来", original_name="剑来 1集")

    assert [(item.name, item.url, item.duration_seconds) for item in items] == [
        ("剑来 第1集", "https://tv.sohu.com/v/700001.html", 1420),
    ]
    assert items[0].resolve_context["aid"] == "700001"
    assert items[0].resolve_context["year"] == 2026


def test_sohu_playlist_decodes_gbk_encoded_titles() -> None:
    payload = {
        "videos": [
            {
                "vid": "800001",
                "video_name": "难哄第1集",
                "url_html5": "https://tv.sohu.com/v/800001.html",
                "playLength": 1420,
            }
        ]
    }

    def fake_get(url: str, **kwargs):
        if url == "https://m.so.tv.sohu.com/search/pc/keyword":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "aid": "800001",
                                "album_name": "难哄",
                                "meta": ["2026年", "电视剧 | 内地"],
                            }
                        ]
                    }
                },
            )
        if url == "https://pl.hd.sohu.com/videolist":
            return httpx.Response(
                200,
                content=json.dumps(payload, ensure_ascii=False).encode("gbk"),
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)

    items = provider.search("难哄", original_name="难哄 1集")

    assert [(item.name, item.duration_seconds) for item in items] == [("难哄 第1集", 1420)]
    assert items[0].resolve_context["year"] == 2026


def test_sohu_resolve_uses_primed_context_and_maps_segment_comments() -> None:
    calls: list[tuple[str, dict | None]] = []

    def fake_get(url: str, **kwargs):
        calls.append((url, kwargs.get("params")))
        if url == "https://api.danmu.tv.sohu.com/dmh5/dmListAll":
            return httpx.Response(
                200,
                json={
                    "info": {
                        "comments": [
                            {
                                "i": "c1",
                                "c": "第一条",
                                "v": 12.5,
                                "uid": "u1",
                                "created": "1710000000",
                                "t": {"p": 1, "c": "#ffffff"},
                            },
                            {
                                "i": "c2",
                                "c": "顶部",
                                "v": 18.0,
                                "uid": "u2",
                                "created": "1710000001",
                                "t": {"p": 4, "c": "#ff0000"},
                            },
                        ]
                    }
                },
            )
        if url == "https://pl.hd.sohu.com/videolist":
            return httpx.Response(200, json={"videos": [{"vid": "9002", "playLength": 120}]})
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)
    provider.prime_resolve_context(
        "https://tv.sohu.com/v/dXMvOTAwMi8=.html",
        {"aid": "200001", "vid": "9002", "duration_seconds": 120},
    )

    records = provider.resolve("https://tv.sohu.com/v/dXMvOTAwMi8=.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (12.5, 1, "16777215", "第一条"),
        (18.0, 5, "16711680", "顶部"),
    ]
    assert all(url != "https://tv.sohu.com/v/dXMvOTAwMi8=.html" for url, _ in calls)


def test_sohu_resolve_parses_decimal_comment_colors() -> None:
    # 搜狐接口实际返回十进制颜色字符串(danmu_api 05c6ed8),#RRGGBB/裸十六进制继续兼容
    def fake_get(url: str, **kwargs):
        if url == "https://api.danmu.tv.sohu.com/dmh5/dmListAll":
            return httpx.Response(
                200,
                json={
                    "info": {
                        "comments": [
                            {
                                "i": "c1",
                                "c": "红色十进制",
                                "v": 1.0,
                                "t": {"p": 1, "c": "16711680"},
                            },
                            {
                                "i": "c2",
                                "c": "裸十六进制",
                                "v": 2.0,
                                "t": {"p": 1, "c": "ff0000"},
                            },
                            {
                                "i": "c3",
                                "c": "非法值",
                                "v": 3.0,
                                "t": {"p": 1, "c": "#zzzzzz"},
                            },
                        ]
                    }
                },
            )
        if url == "https://pl.hd.sohu.com/videolist":
            videos = [{"vid": "9002", "playLength": 120}]
            return httpx.Response(200, json={"videos": videos})
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)
    provider.prime_resolve_context(
        "https://tv.sohu.com/v/dXMvOTAwMi8=.html",
        {"aid": "200001", "vid": "9002", "duration_seconds": 120},
    )

    records = provider.resolve("https://tv.sohu.com/v/dXMvOTAwMi8=.html")

    assert [(record.color, record.content) for record in records] == [
        ("16711680", "红色十进制"),
        ("16711680", "裸十六进制"),
        ("16777215", "非法值"),
    ]


def test_sohu_resolve_fetches_segments_concurrently() -> None:
    lock = threading.Lock()
    active_requests = 0
    max_active_requests = 0

    def fake_get(url: str, **kwargs):
        nonlocal active_requests, max_active_requests
        if url == "https://api.danmu.tv.sohu.com/dmh5/dmListAll":
            start = int(kwargs["params"]["time_begin"])
            with lock:
                active_requests += 1
                max_active_requests = max(max_active_requests, active_requests)
            time.sleep(0.05)
            with lock:
                active_requests -= 1
            return httpx.Response(
                200,
                json={
                    "info": {
                        "comments": [
                            {
                                "i": f"c{start}",
                                "c": f"第{start}段",
                                "v": float(start),
                                "uid": "u1",
                                "created": "1710000000",
                                "t": {"p": 1, "c": "#ffffff"},
                            }
                        ]
                    }
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)
    provider.prime_resolve_context(
        "https://tv.sohu.com/v/demo.html",
        {"aid": "500001", "vid": "9999", "duration_seconds": 1200},
    )

    records = provider.resolve("https://tv.sohu.com/v/demo.html")

    assert max_active_requests >= 2
    assert [record.content for record in records] == ["第0段", "第300段", "第600段", "第900段"]


def test_sohu_resolve_falls_back_to_page_html_for_aid_and_vid() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://tv.sohu.com/v/demo.html":
            return httpx.Response(
                200,
                text='<html><input id="aid" value="500001" /><script>var vid="9999";</script></html>',
            )
        if url == "https://api.danmu.tv.sohu.com/dmh5/dmListAll":
            return httpx.Response(
                200,
                json={
                    "info": {
                        "comments": [
                            {
                                "i": "c1",
                                "c": "回退成功",
                                "v": 3.0,
                                "uid": "u1",
                                "created": "1710000002",
                                "t": {"p": 5, "c": "#00ff00"},
                            }
                        ]
                    }
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)
    provider._duration_for_video = lambda aid, vid: 60

    records = provider.resolve("https://tv.sohu.com/v/demo.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (3.0, 4, "65280", "回退成功"),
    ]


def test_sohu_resolve_parses_jsonp_playlist_response_for_duration_lookup() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://pl.hd.sohu.com/videolist":
            return httpx.Response(
                200,
                text='jsonp({"videos":[{"vid":"9999","playLength":60}]})',
            )
        if url == "https://api.danmu.tv.sohu.com/dmh5/dmListAll":
            return httpx.Response(
                200,
                json={
                    "info": {
                        "comments": [
                            {
                                "i": "c1",
                                "c": "jsonp成功",
                                "v": 5.0,
                                "uid": "u1",
                                "created": "1710000003",
                                "t": {"p": 1, "c": "#ffffff"},
                            }
                        ]
                    }
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)
    provider.prime_resolve_context(
        "https://m.tv.sohu.com/v9999.shtml",
        {"aid": "500001", "vid": "9999", "duration_seconds": 0},
    )

    records = provider.resolve("https://m.tv.sohu.com/v9999.shtml")

    assert [(record.time_offset, record.content) for record in records] == [(5.0, "jsonp成功")]


def test_sohu_resolve_ignores_non_mapping_playlist_items_when_looking_up_duration() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://pl.hd.sohu.com/videolist":
            return httpx.Response(
                200,
                json={"videos": ["bad-item", {"vid": "9999", "playLength": 60}]},
            )
        if url == "https://api.danmu.tv.sohu.com/dmh5/dmListAll":
            return httpx.Response(
                200,
                json={
                    "info": {
                        "comments": [
                            {
                                "i": "c1",
                                "c": "playlist容错",
                                "v": 6.0,
                                "uid": "u1",
                                "created": "1710000004",
                                "t": {"p": 1, "c": "#ffffff"},
                            }
                        ]
                    }
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)
    provider.prime_resolve_context(
        "https://m.tv.sohu.com/v9999.shtml",
        {"aid": "500001", "vid": "9999", "duration_seconds": 0},
    )

    records = provider.resolve("https://m.tv.sohu.com/v9999.shtml")

    assert [(record.time_offset, record.content) for record in records] == [(6.0, "playlist容错")]


def test_sohu_resolve_ignores_non_mapping_comment_entries() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://api.danmu.tv.sohu.com/dmh5/dmListAll":
            return httpx.Response(
                200,
                json={
                    "info": {
                        "comments": [
                            "bad-comment",
                            {
                                "i": "c1",
                                "c": "评论容错",
                                "v": 7.0,
                                "uid": "u1",
                                "created": "1710000005",
                                "t": {"p": 5, "c": "#00ff00"},
                            },
                        ]
                    }
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)
    provider.prime_resolve_context(
        "https://tv.sohu.com/v/demo.html",
        {"aid": "500001", "vid": "9999", "duration_seconds": 60},
    )

    records = provider.resolve("https://tv.sohu.com/v/demo.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (7.0, 4, "65280", "评论容错"),
    ]


def test_sohu_resolve_ignores_non_mapping_comment_style_payload() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://api.danmu.tv.sohu.com/dmh5/dmListAll":
            return httpx.Response(
                200,
                json={
                    "info": {
                        "comments": [
                            {
                                "i": "c1",
                                "c": "样式容错",
                                "v": 8.0,
                                "uid": "u1",
                                "created": "1710000006",
                                "t": "bad-style",
                            }
                        ]
                    }
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)
    provider.prime_resolve_context(
        "https://tv.sohu.com/v/demo.html",
        {"aid": "500001", "vid": "9999", "duration_seconds": 60},
    )

    records = provider.resolve("https://tv.sohu.com/v/demo.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (8.0, 1, "16777215", "样式容错"),
    ]


def test_sohu_resolve_falls_back_when_comment_position_is_not_numeric() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://api.danmu.tv.sohu.com/dmh5/dmListAll":
            return httpx.Response(
                200,
                json={
                    "info": {
                        "comments": [
                            {
                                "i": "c1",
                                "c": "位置容错",
                                "v": 9.0,
                                "uid": "u1",
                                "created": "1710000007",
                                "t": {"p": "t", "c": "#ff0000"},
                            }
                        ]
                    }
                },
            )
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)
    provider.prime_resolve_context(
        "https://tv.sohu.com/v/demo.html",
        {"aid": "500001", "vid": "9999", "duration_seconds": 60},
    )

    records = provider.resolve("https://tv.sohu.com/v/demo.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (9.0, 1, "16711680", "位置容错"),
    ]


def test_sohu_resolve_raises_when_all_segments_are_unusable() -> None:
    def fake_get(url: str, **kwargs):
        if url == "https://api.danmu.tv.sohu.com/dmh5/dmListAll":
            return httpx.Response(200, text="{bad json")
        raise AssertionError(url)

    provider = SohuDanmakuProvider(get=fake_get)
    provider.prime_resolve_context(
        "https://tv.sohu.com/v/demo.html",
        {"aid": "500001", "vid": "9999", "duration_seconds": 60},
    )

    with pytest.raises(DanmakuResolveError, match="搜狐弹幕分段解析失败"):
        provider.resolve("https://tv.sohu.com/v/demo.html")
