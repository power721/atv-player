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


def test_bilibili_search_orders_bangumi_ft_before_video_and_preserves_metadata() -> None:
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

    assert [item.search_type for item in items] == ["media_bangumi", "media_ft", "video"]
    assert items[0].url == "https://www.bilibili.com/bangumi/play/ep5001"
    assert items[0].ep_id == 5001
    assert items[0].season_id == 4001
    assert items[0].bvid == "BVbangumi1"
    assert items[2].aid == 9001
    assert items[2].url == "https://www.bilibili.com/video/BVvideo1"


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
    assert search_attempts["count"] == 4
    assert any("GenWebTicket" in url for url in calls)


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
