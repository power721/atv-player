import json
import pytest

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.models import DanmakuSearchItem
from atv_player.danmaku.providers.bilibili import BilibiliDanmakuProvider


class JsonResponse:
    def __init__(self, payload, text="") -> None:
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def test_bilibili_search_orders_bangumi_and_ft_results_and_skips_normal_video_search() -> None:
    search_types: list[str] = []
    search_payloads = {
        "media_bangumi": {
            "code": 0,
            "data": {
                "result": [
                    {
                        "title": '<em class="keyword">凡人修仙传</em> 第1集',
                        "media_type": 1,
                        "season_id": 4001,
                        "ep_id": 5001,
                        "bvid": "BVbangumi1",
                        "url": "//www.bilibili.com/bangumi/play/ep5001",
                    }
                ]
            },
        },
        "media_ft": {
            "code": 0,
            "data": {
                "result": [
                    {
                        "title": "凡人修仙传 特别篇",
                        "season_id": 4002,
                        "ep_id": 5002,
                        "bvid": "BVft1",
                        "url": "//www.bilibili.com/bangumi/play/ep5002",
                    }
                ]
            },
        },
        "video": {
            "code": 0,
            "data": {
                "result": [
                    {
                        "title": '<em class="keyword">凡人修仙传</em> P1',
                        "bvid": "BVvideo1",
                        "aid": 9001,
                        "arcurl": "https://www.bilibili.com/video/BVvideo1",
                    }
                ]
            },
        },
    }

    def fake_get(url: str, **kwargs):
        params = kwargs.get("params") or {}
        if "search/type" in url:
            search_types.append(params["search_type"])
            return JsonResponse(search_payloads[params["search_type"]])
        if "nav" in url:
            return JsonResponse(
                {
                    "code": 0,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/abc123.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/def456.png",
                        }
                    },
                }
            )
        return JsonResponse({"code": 0, "data": {}}, text="")

    provider = BilibiliDanmakuProvider(get=fake_get)

    items = provider.search("凡人修仙传 第1集")

    assert search_types == ["media_bangumi", "media_ft"]
    assert [item.search_type for item in items] == ["media_bangumi", "media_ft"]
    assert items[0].url == "https://www.bilibili.com/bangumi/play/ep5001"
    assert items[0].ep_id == 5001
    assert items[0].season_id == 4001
    assert items[0].bvid == "BVbangumi1"
    assert items[1].url == "https://www.bilibili.com/bangumi/play/ep5002"


def test_bilibili_search_retries_once_after_ticket_refresh() -> None:
    calls: list[str] = []
    search_attempts = {"count": 0}

    def fake_get(url: str, **kwargs):
        calls.append(url)
        if "x/web-interface/nav" in url:
            return JsonResponse(
                {
                    "code": 0,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/abc123.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/def456.png",
                        }
                    },
                }
            )
        if "GenWebTicket" in url:
            return JsonResponse({"code": 0, "data": {"ticket": "ok"}})
        if "search/type" in url:
            search_attempts["count"] += 1
            if search_attempts["count"] == 1:
                return JsonResponse({"code": -352, "message": "risk control"})
            return JsonResponse({"code": 0, "data": {"result": []}})
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)

    items = provider.search("凡人修仙传 第1集")

    assert items == []
    assert search_attempts["count"] == 3
    assert any("GenWebTicket" in url for url in calls)


def test_bilibili_search_expands_season_result_into_episode_candidates() -> None:
    def fake_get(url: str, **kwargs):
        params = kwargs.get("params") or {}
        if "x/frontend/finger/spi" in url:
            return JsonResponse({"code": 0, "data": {"b_3": "buvid3-demo", "b_4": "buvid4-demo"}})
        if "x/web-interface/nav" in url:
            return JsonResponse(
                {
                    "code": 0,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/abc123.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/def456.png",
                        }
                    },
                }
            )
        if "search/type" in url:
            if params["search_type"] == "media_bangumi":
                return JsonResponse(
                    {
                        "code": 0,
                        "data": {
                            "result": [
                                {
                                    "title": "牧神记",
                                    "season_id": 45969,
                                    "url": "//www.bilibili.com/bangumi/play/ss45969",
                                }
                            ]
                        },
                    }
                )
            return JsonResponse({"code": 0, "data": {"result": []}})
        if "pgc/view/web/season" in url and params.get("season_id") == 45969:
            return JsonResponse(
                {
                    "code": 0,
                    "result": {
                        "title": "牧神记",
                        "episodes": [],
                        "section": [
                            {
                                "title": "正片",
                                "episodes": [
                                    {
                                        "ep_id": 9001,
                                        "cid": 7001,
                                        "bvid": "BVep9001",
                                        "share_copy": "牧神记 第1集",
                                    },
                                    {
                                        "ep_id": 9002,
                                        "cid": 7002,
                                        "bvid": "BVep9002",
                                        "share_copy": "牧神记 第2集",
                                    },
                                ],
                            }
                        ],
                    },
                }
            )
        return JsonResponse({"code": 0, "data": {}}, text="")

    provider = BilibiliDanmakuProvider(get=fake_get)

    items = provider.search("牧神记")

    assert [(item.name, item.url, item.ep_id, item.cid) for item in items] == [
        ("牧神记 第1集", "https://www.bilibili.com/bangumi/play/ep9001", 9001, 7001),
        ("牧神记 第2集", "https://www.bilibili.com/bangumi/play/ep9002", 9002, 7002),
    ]


def test_bilibili_search_raises_after_second_risk_control_failure() -> None:
    def fake_get(url: str, **kwargs):
        if "x/web-interface/nav" in url:
            return JsonResponse(
                {
                    "code": 0,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/abc123.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/def456.png",
                        }
                    },
                }
            )
        if "GenWebTicket" in url:
            return JsonResponse({"code": 0, "data": {"ticket": "ok"}})
        if "search/type" in url:
            return JsonResponse({"code": -352, "message": "risk control"})
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)

    with pytest.raises(DanmakuSearchError, match="Bilibili search failed"):
        provider.search("凡人修仙传 第1集")


def test_bilibili_search_primes_spi_before_nav_and_uses_browser_headers() -> None:
    seen: list[tuple[str, dict]] = []

    def fake_get(url: str, **kwargs):
        seen.append((url, kwargs))
        params = kwargs.get("params") or {}
        if "x/frontend/finger/spi" in url:
            return JsonResponse({"code": 0, "data": {"b_3": "buvid3-demo", "b_4": "buvid4-demo"}})
        if "x/web-interface/nav" in url:
            return JsonResponse(
                {
                    "code": 0,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/abc123.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/def456.png",
                        }
                    },
                }
            )
        if "search/type" in url:
            return JsonResponse({"code": 0, "data": {"result": []}})
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)

    provider.search("凡人修仙传 第1集")

    assert [url for url, _ in seen[:3]] == [
        "https://api.bilibili.com/x/frontend/finger/spi",
        "https://api.bilibili.com/x/web-interface/nav",
        "https://api.bilibili.com/x/web-interface/wbi/search/type",
    ]
    for _, kwargs in seen[:3]:
        headers = kwargs["headers"]
        assert headers["user-agent"].startswith("Mozilla/5.0")
        assert headers["referer"] == "https://www.bilibili.com/"


def test_bilibili_search_raises_clear_error_when_nav_returns_html_412() -> None:
    class Html412Response:
        status_code = 412
        text = "<html><body>412 Precondition Failed</body></html>"

        def json(self):
            raise json.JSONDecodeError("Expecting value", self.text, 0)

    def fake_get(url: str, **kwargs):
        if "x/frontend/finger/spi" in url:
            return JsonResponse({"code": 0, "data": {"b_3": "buvid3-demo", "b_4": "buvid4-demo"}})
        if "x/web-interface/nav" in url:
            return Html412Response()
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)

    with pytest.raises(DanmakuSearchError, match="Bilibili nav request failed with HTTP 412"):
        provider.search("凡人修仙传 第1集")


def test_bilibili_resolve_uses_browser_headers_for_season_request() -> None:
    seen: list[tuple[str, dict]] = []

    def fake_get(url: str, **kwargs):
        seen.append((url, kwargs))
        params = kwargs.get("params") or {}
        if "pgc/view/web/season" in url and params.get("season_id") == 45969:
            return JsonResponse(
                {
                    "code": 0,
                    "result": {
                        "episodes": [
                            {
                                "ep_id": 9001,
                                "cid": 7001,
                                "share_copy": "牧神记 第1集",
                            }
                        ]
                    },
                }
            )
        if "comment.bilibili.com/7001.xml" in url:
            return JsonResponse(
                {"code": 0},
                text='<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215,0,0,0,0">ok</d></i>',
            )
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)
    provider._metadata_by_url["https://www.bilibili.com/bangumi/play/ss45969"] = DanmakuSearchItem(
        provider="bilibili",
        name="牧神记",
        url="https://www.bilibili.com/bangumi/play/ss45969",
        season_id=45969,
        search_type="media_bangumi",
    )

    provider.resolve("https://www.bilibili.com/bangumi/play/ss45969")

    season_headers = next(kwargs["headers"] for url, kwargs in seen if "pgc/view/web/season" in url)
    assert season_headers["user-agent"].startswith("Mozilla/5.0")
    assert season_headers["referer"] == "https://www.bilibili.com/"
    assert season_headers["origin"] == "https://www.bilibili.com"
    assert season_headers["accept-language"].startswith("en-US,en;q=0.9")
    assert season_headers["accept"] == "*/*"
    assert "cookie" not in season_headers


def test_bilibili_resolve_prefers_cached_candidate_cid_and_parses_xml() -> None:
    def fake_get(url: str, **kwargs):
        if "x/web-interface/nav" in url:
            return JsonResponse(
                {
                    "code": 0,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/abc123.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/def456.png",
                        }
                    },
                }
            )
        if "search/type" in url:
            return JsonResponse(
                {
                    "code": 0,
                    "data": {
                        "result": [
                            {
                                "title": "凡人修仙传 第1集",
                                "url": "//www.bilibili.com/bangumi/play/ep5001",
                                "cid": 777001,
                                "ep_id": 5001,
                                "season_id": 4001,
                            }
                        ]
                    },
                }
            )
        if "comment.bilibili.com/777001.xml" in url:
            return JsonResponse(
                {"code": 0},
                text='<?xml version="1.0" encoding="UTF-8"?><i><d p="1.5,1,25,16777215,0,0,0,0">第一条</d></i>',
            )
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)
    items = provider.search("凡人修仙传 第1集")

    records = provider.resolve(items[0].url)

    assert len(records) == 1
    assert records[0].time_offset == 1.5
    assert records[0].pos == 1
    assert records[0].color == "16777215"
    assert records[0].content == "第一条"


def test_bilibili_resolve_uses_season_api_then_pagelist_then_html_fallback() -> None:
    def fake_get(url: str, **kwargs):
        params = kwargs.get("params") or {}
        if "pgc/view/web/season" in url and params.get("ep_id") == 5002:
            return JsonResponse({"code": 0, "result": {"episodes": [{"ep_id": 5002, "cid": 888002, "bvid": "BVep5002"}]}})
        if "x/player/pagelist" in url and params.get("bvid") == "BVvideo2":
            return JsonResponse({"code": 0, "data": [{"cid": 999003, "part": "第1集"}]})
        if "video/BVhtml1" in url:
            return JsonResponse({"code": 0}, text='<script>window.__INITIAL_STATE__={"videoData":{"cid":666004}}</script>')
        if "comment.bilibili.com/888002.xml" in url:
            return JsonResponse(
                {"code": 0},
                text='<?xml version="1.0" encoding="UTF-8"?><i><d p="2.0,1,25,255,0,0,0,0">season</d></i>',
            )
        if "comment.bilibili.com/999003.xml" in url:
            return JsonResponse(
                {"code": 0},
                text='<?xml version="1.0" encoding="UTF-8"?><i><d p="3.0,1,25,65280,0,0,0,0">pagelist</d></i>',
            )
        if "comment.bilibili.com/666004.xml" in url:
            return JsonResponse(
                {"code": 0},
                text='<?xml version="1.0" encoding="UTF-8"?><i><d p="4.0,1,25,16711680,0,0,0,0">html</d></i>',
            )
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)
    provider._metadata_by_url["https://www.bilibili.com/bangumi/play/ep5002"] = DanmakuSearchItem(
        provider="bilibili",
        name="凡人修仙传 第2集",
        url="https://www.bilibili.com/bangumi/play/ep5002",
        ep_id=5002,
        season_id=4002,
    )
    provider._metadata_by_url["https://www.bilibili.com/video/BVvideo2"] = DanmakuSearchItem(
        provider="bilibili",
        name="凡人修仙传 第1集",
        url="https://www.bilibili.com/video/BVvideo2",
        bvid="BVvideo2",
        search_type="video",
    )
    provider._metadata_by_url["https://www.bilibili.com/video/BVhtml1"] = DanmakuSearchItem(
        provider="bilibili",
        name="凡人修仙传 PV",
        url="https://www.bilibili.com/video/BVhtml1",
        bvid="BVhtml1",
        search_type="video",
    )

    assert provider.resolve("https://www.bilibili.com/bangumi/play/ep5002")[0].content == "season"
    assert provider.resolve("https://www.bilibili.com/video/BVvideo2")[0].content == "pagelist"
    assert provider.resolve("https://www.bilibili.com/video/BVhtml1")[0].content == "html"


def test_bilibili_resolve_uses_section_episodes_for_season_only_candidate() -> None:
    def fake_get(url: str, **kwargs):
        params = kwargs.get("params") or {}
        if "pgc/view/web/season" in url and params.get("season_id") == 45969:
            return JsonResponse(
                {
                    "code": 0,
                    "result": {
                        "title": "牧神记",
                        "episodes": [],
                        "section": [
                            {
                                "title": "正片",
                                "episodes": [
                                    {
                                        "ep_id": 9001,
                                        "cid": 7001,
                                        "bvid": "BVep9001",
                                        "share_copy": "牧神记 第1集",
                                    }
                                ],
                            }
                        ],
                    },
                }
            )
        if "comment.bilibili.com/7001.xml" in url:
            return JsonResponse(
                {"code": 0},
                text='<?xml version="1.0" encoding="UTF-8"?><i><d p="5.0,1,25,16777215,0,0,0,0">season-section</d></i>',
            )
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)
    provider._metadata_by_url["https://www.bilibili.com/bangumi/play/ss45969"] = DanmakuSearchItem(
        provider="bilibili",
        name="牧神记",
        url="https://www.bilibili.com/bangumi/play/ss45969",
        season_id=45969,
        search_type="media_bangumi",
    )

    records = provider.resolve("https://www.bilibili.com/bangumi/play/ss45969")

    assert [record.content for record in records] == ["season-section"]


def test_bilibili_resolve_prefers_matching_pagelist_part_before_first_entry() -> None:
    def fake_get(url: str, **kwargs):
        params = kwargs.get("params") or {}
        if "x/player/pagelist" in url and params.get("bvid") == "BVmatch1":
            return JsonResponse(
                {
                    "code": 0,
                    "data": [
                        {"cid": 123001, "part": "预告"},
                        {"cid": 123002, "part": "第1集"},
                    ],
                }
            )
        if "comment.bilibili.com/123002.xml" in url:
            return JsonResponse(
                {"code": 0},
                text='<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215,0,0,0,0">matched</d></i>',
            )
        if "comment.bilibili.com/123001.xml" in url:
            return JsonResponse(
                {"code": 0},
                text='<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215,0,0,0,0">wrong</d></i>',
            )
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)
    provider._metadata_by_url["https://www.bilibili.com/video/BVmatch1"] = DanmakuSearchItem(
        provider="bilibili",
        name="凡人修仙传 第1集",
        url="https://www.bilibili.com/video/BVmatch1",
        bvid="BVmatch1",
        search_type="video",
    )

    records = provider.resolve("https://www.bilibili.com/video/BVmatch1")

    assert [record.content for record in records] == ["matched"]


def test_bilibili_resolve_raises_clear_error_when_no_cid_can_be_found() -> None:
    def fake_get(url: str, **kwargs):
        params = kwargs.get("params") or {}
        if "x/player/pagelist" in url and params.get("bvid") == "BVnone":
            return JsonResponse({"code": 0, "data": []})
        if "video/BVnone" in url:
            return JsonResponse({"code": 0}, text="<html><body>missing cid</body></html>")
        return JsonResponse({"code": 0, "data": {}})

    provider = BilibiliDanmakuProvider(get=fake_get)
    provider._metadata_by_url["https://www.bilibili.com/video/BVnone"] = DanmakuSearchItem(
        provider="bilibili",
        name="空页面",
        url="https://www.bilibili.com/video/BVnone",
        bvid="BVnone",
        search_type="video",
    )

    with pytest.raises(DanmakuResolveError, match="missing cid"):
        provider.resolve("https://www.bilibili.com/video/BVnone")
