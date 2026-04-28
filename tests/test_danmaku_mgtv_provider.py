import httpx
import pytest

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.models import DanmakuRecord
from atv_player.danmaku.providers.mgtv import MgtvDanmakuProvider


def test_mgtv_search_filters_non_imgo_and_invalid_urls() -> None:
    def fake_get(url: str, **kwargs):
        assert url == "https://mobileso.bz.mgtv.com/msite/search/v2"
        assert kwargs["params"]["q"] == "歌手2026"
        return httpx.Response(
            200,
            json={
                "data": {
                    "contents": [
                        {
                            "type": "media",
                            "data": [
                                {
                                    "source": "imgo",
                                    "title": "<em>歌手2026</em>",
                                    "url": "https://www.mgtv.com/b/777/1.html",
                                },
                                {
                                    "source": "other",
                                    "title": "外站结果",
                                    "url": "https://example.com/b/888/1.html",
                                },
                                {
                                    "source": "imgo",
                                    "title": "坏结果",
                                    "url": "https://www.mgtv.com/not-a-play-url.html",
                                },
                            ],
                        }
                    ]
                }
            },
        )

    provider = MgtvDanmakuProvider(get=fake_get)
    provider._expand_candidate = lambda title, collection_id: []

    items = provider.search("歌手2026")

    assert items == []


def test_mgtv_search_expands_collection_into_episode_candidates() -> None:
    def fake_get(url: str, **kwargs):
        return httpx.Response(
            200,
            json={
                "data": {
                    "contents": [
                        {
                            "type": "media",
                            "data": [
                                {
                                    "source": "imgo",
                                    "title": "<em>歌手2026</em>",
                                    "url": "https://www.mgtv.com/b/555/1.html",
                                }
                            ],
                        }
                    ]
                }
            },
        )

    provider = MgtvDanmakuProvider(get=fake_get)
    provider._expand_candidate = lambda title, collection_id: [
        ("歌手2026 第1期", "https://www.mgtv.com/b/555/1001.html"),
        ("歌手2026 第2期", "https://www.mgtv.com/b/555/1002.html"),
    ]

    items = provider.search("歌手2026")

    assert [(item.provider, item.name, item.url) for item in items] == [
        ("mgtv", "歌手2026 第1期", "https://www.mgtv.com/b/555/1001.html"),
        ("mgtv", "歌手2026 第2期", "https://www.mgtv.com/b/555/1002.html"),
    ]


def test_mgtv_search_skips_expansion_for_unrelated_search_hits() -> None:
    expanded_ids: list[str] = []

    def fake_get(url: str, **kwargs):
        return httpx.Response(
            200,
            json={
                "data": {
                    "contents": [
                        {
                            "type": "media",
                            "data": [
                                {
                                    "source": "imgo",
                                    "title": "黑夜告白",
                                    "url": "https://www.mgtv.com/b/555/1.html",
                                },
                                {
                                    "source": "imgo",
                                    "title": "歌手2026",
                                    "url": "https://www.mgtv.com/b/777/1.html",
                                },
                                {
                                    "source": "imgo",
                                    "title": "你好，星期六",
                                    "url": "https://www.mgtv.com/b/888/1.html",
                                },
                            ],
                        }
                    ]
                }
            },
        )

    provider = MgtvDanmakuProvider(get=fake_get)

    def fake_expand(title: str, collection_id: str):
        expanded_ids.append(collection_id)
        return [("黑夜告白 第1集", f"https://www.mgtv.com/b/{collection_id}/1001.html")]

    provider._expand_candidate = fake_expand

    items = provider.search("黑夜告白")

    assert expanded_ids == ["555"]
    assert [(item.name, item.url) for item in items] == [
        ("黑夜告白 第1集", "https://www.mgtv.com/b/555/1001.html")
    ]


def test_mgtv_search_raises_for_invalid_payload() -> None:
    provider = MgtvDanmakuProvider(get=lambda url, **kwargs: httpx.Response(200, json={"oops": 1}))

    with pytest.raises(DanmakuSearchError, match="MGTV"):
        provider.search("歌手2026")


def test_mgtv_search_uses_full_query_params_required_by_api() -> None:
    seen_params: dict | None = None
    seen_headers: dict | None = None

    def fake_get(url: str, **kwargs):
        nonlocal seen_params
        nonlocal seen_headers
        seen_params = kwargs["params"]
        seen_headers = kwargs["headers"]
        return httpx.Response(200, json={"data": {"contents": []}})

    provider = MgtvDanmakuProvider(get=fake_get)

    provider.search("夏末初见")

    assert seen_params == {
        "q": "夏末初见",
        "pc": 30,
        "pn": 1,
        "sort": -99,
        "ty": 0,
        "du": 0,
        "pt": 0,
        "corr": 1,
        "abroad": 0,
        "_support": 10000000000000000,
    }
    assert seen_headers == {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://www.mgtv.com/",
    }


def test_mgtv_search_raises_clear_error_when_response_is_not_json() -> None:
    def fake_get(url: str, **kwargs):
        return httpx.Response(401, text="<html>unauthorized</html>")

    provider = MgtvDanmakuProvider(get=fake_get)

    with pytest.raises(DanmakuSearchError, match="MGTV.*401"):
        provider.search("夏末初见")


def test_mgtv_search_expands_month_tabs_and_filters_preview_titles() -> None:
    calls: list[tuple[str, dict]] = []

    def fake_get(url: str, **kwargs):
        params = kwargs.get("params") or {}
        calls.append((url, params))
        if "msite/search/v2" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "contents": [
                            {
                                "type": "media",
                                "data": [
                                    {
                                        "source": "imgo",
                                        "title": "歌手2026",
                                        "url": "https://www.mgtv.com/b/555/1.html",
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        if "variety/showlist" in url and params.get("month", "") == "":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "tab_m": [{"m": "2026-04"}, {"m": "2026-05"}],
                        "list": [
                            {"t2": "第1期", "t1": "", "video_id": "1001", "isnew": "1", "src_clip_id": "555"},
                            {"t2": "第1期 预告", "t1": "", "video_id": "100x", "isnew": "2", "src_clip_id": "555"},
                        ],
                    }
                },
            )
        if "variety/showlist" in url and params.get("month") == "2026-05":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "tab_m": [{"m": "2026-04"}, {"m": "2026-05"}],
                        "list": [
                            {"t2": "第2期", "t1": "", "video_id": "1002", "isnew": "1", "src_clip_id": "555"},
                        ],
                    }
                },
            )
        raise AssertionError((url, kwargs))

    provider = MgtvDanmakuProvider(get=fake_get)

    items = provider.search("歌手2026")

    assert [(item.name, item.url) for item in items] == [
        ("歌手2026 第1期", "https://www.mgtv.com/b/555/1001.html"),
        ("歌手2026 第2期", "https://www.mgtv.com/b/555/1002.html"),
    ]
    assert [params.get("month", "") for url, params in calls if "variety/showlist" in url] == ["", "2026-05"]


def test_mgtv_expand_candidate_selects_best_movie_item() -> None:
    def fake_get(url: str, **kwargs):
        if "variety/showlist" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "list": [
                            {"t3": "预告", "video_id": "9000", "isnew": "2"},
                            {"t3": "正片", "video_id": "9001", "isIntact": "1"},
                        ]
                    }
                },
            )
        raise AssertionError((url, kwargs))

    provider = MgtvDanmakuProvider(get=fake_get)

    assert provider._expand_candidate("电影名", "777") == [
        ("电影名 正片", "https://www.mgtv.com/b/777/9001.html")
    ]


def test_mgtv_resolve_uses_cdn_segments_when_control_metadata_exists() -> None:
    def fake_get(url: str, **kwargs):
        if "video/info" in url:
            return httpx.Response(200, json={"data": {"info": {"time": "01:35"}}})
        if "getctlbarrage" in url:
            return httpx.Response(200, json={"data": {"cdn_list": "bullet.mgtv.com,backup.mgtv.com", "cdn_version": "v2"}})
        if url == "https://bullet.mgtv.com/v2/0.json":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "time": 1500,
                                "content": "第一条",
                                "v2_position": 1,
                                "v2_color": {"color_left": "rgb(255,0,0)", "color_right": "rgb(255,0,0)"},
                            }
                        ]
                    }
                },
            )
        if url == "https://bullet.mgtv.com/v2/1.json":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "time": 61000,
                                "content": "第二条",
                                "v2_position": 2,
                                "v2_color": {"color_left": "rgb(0,255,0)", "color_right": "rgb(0,255,0)"},
                            }
                        ]
                    }
                },
            )
        raise AssertionError((url, kwargs))

    provider = MgtvDanmakuProvider(get=fake_get)

    records = provider.resolve("https://www.mgtv.com/b/555/1001.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (1.5, 5, "16711680", "第一条"),
        (61.0, 4, "65280", "第二条"),
    ]


def test_mgtv_resolve_falls_back_to_rdbarrage_when_control_metadata_is_missing() -> None:
    requested: list[str] = []

    def fake_get(url: str, **kwargs):
        requested.append(url)
        if "video/info" in url:
            return httpx.Response(200, json={"data": {"info": {"time": "00:59"}}})
        if "getctlbarrage" in url:
            return httpx.Response(200, json={"data": {}})
        if "rdbarrage" in url:
            return httpx.Response(200, json={"data": {"items": [{"time": 3000, "content": "回退路径"}]}})
        raise AssertionError((url, kwargs))

    provider = MgtvDanmakuProvider(get=fake_get)

    records = provider.resolve("https://www.mgtv.com/b/555/1001.html")

    assert [(record.time_offset, record.content) for record in records] == [(3.0, "回退路径")]
    assert any("rdbarrage" in url for url in requested)


def test_mgtv_resolve_rejects_invalid_play_url() -> None:
    provider = MgtvDanmakuProvider(get=lambda url, **kwargs: httpx.Response(200, json={}))

    with pytest.raises(DanmakuResolveError, match="MGTV"):
        provider.resolve("https://www.mgtv.com/not-valid.html")


def test_mgtv_provider_supports_only_mgtv_urls() -> None:
    provider = MgtvDanmakuProvider()

    assert provider.supports("https://www.mgtv.com/b/555/1001.html") is True
    assert provider.supports("https://v.qq.com/x/cover/demo/vid123.html") is False


def test_mgtv_resolve_ignores_empty_segment_items() -> None:
    def fake_get(url: str, **kwargs):
        if "video/info" in url:
            return httpx.Response(200, json={"data": {"info": {"time": "00:30"}}})
        if "getctlbarrage" in url:
            return httpx.Response(200, json={"data": {"cdn_list": "bullet.mgtv.com", "cdn_version": "v1"}})
        if url == "https://bullet.mgtv.com/v1/0.json":
            return httpx.Response(
                200,
                json={"data": {"items": [{"time": 1000, "content": ""}, {"time": 2000, "content": "保留"}]}},
            )
        raise AssertionError((url, kwargs))

    provider = MgtvDanmakuProvider(get=fake_get)

    records = provider.resolve("https://www.mgtv.com/b/555/1001.html")

    assert records == [DanmakuRecord(time_offset=2.0, pos=1, color="16777215", content="保留")]
