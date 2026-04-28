import pytest

from atv_player.danmaku.providers.bilibili import BilibiliDanmakuProvider
from atv_player.danmaku.errors import DanmakuSearchError


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
