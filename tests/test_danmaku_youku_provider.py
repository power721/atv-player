import httpx

from atv_player.danmaku.providers.youku import YoukuDanmakuProvider


def test_youku_provider_search_maps_candidates_from_search_payload() -> None:
    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert "search.youku.com" in url
        return httpx.Response(
            200,
            json={
                "pageComponentList": [
                    {
                        "commonData": {
                            "titleDTO": {"displayName": "剑来 第1集"},
                            "updateNotice": "第1集",
                            "showId": "show123",
                            "videoLink": "https://v.youku.com/v_show/id_demo123.html",
                        }
                    }
                ]
            },
        )

    provider = YoukuDanmakuProvider(get=fake_get)

    items = provider.search("剑来 第1集")

    assert len(items) == 1
    assert items[0].provider == "youku"
    assert items[0].name == "剑来 第1集"
    assert items[0].url == "https://v.youku.com/v_show/id_demo123.html"


def test_youku_provider_search_maps_candidates_from_series_payload() -> None:
    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert "search.youku.com" in url
        return httpx.Response(
            200,
            json={
                "status": "success",
                "sourceName": "优酷",
                "serisesList": [
                    {
                        "videoId": "XMjQ4MTc0ODMyOA==",
                        "title": "月鳞绮纪 01",
                        "displayName": "1",
                        "showVideoStage": "第1期",
                    },
                    {
                        "videoId": "",
                        "title": "invalid",
                    },
                ],
            },
        )

    provider = YoukuDanmakuProvider(get=fake_get)

    items = provider.search("月鳞绮纪")

    assert len(items) == 1
    assert items[0].provider == "youku"
    assert items[0].name == "月鳞绮纪 01"
    assert items[0].url == "https://v.youku.com/v_show/id_XMjQ4MTc0ODMyOA==.html"


def test_youku_provider_resolve_extracts_vid_and_uses_data_version() -> None:
    calls: list[str] = []

    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        calls.append(url)
        if url == "https://v.youku.com/v_show/id_demo123.html":
            return httpx.Response(200, text='{"vid":"demo123","duration":120} <div dataVersion="42"></div>')
        if "acs.youku.com/h5/mopen.youku.danmu.list/1.0/" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "result": [
                            {"playat": 1500, "propertis": '{"pos":1,"color":16777215}', "content": "优酷第一条"},
                            {"playat": 3200, "propertis": '{"pos":4,"color":255}', "content": "优酷第二条"},
                        ]
                    }
                },
            )
        raise AssertionError(url)

    provider = YoukuDanmakuProvider(get=fake_get)

    records = provider.resolve("https://v.youku.com/v_show/id_demo123.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (1.5, 1, "16777215", "优酷第一条"),
        (3.2, 4, "255", "优酷第二条"),
    ]
    assert calls[0] == "https://v.youku.com/v_show/id_demo123.html"


def test_youku_provider_supports_youku_urls() -> None:
    provider = YoukuDanmakuProvider()

    assert provider.supports("https://v.youku.com/v_show/id_demo123.html") is True
    assert provider.supports("https://v.qq.com/x/cover/demo/vid123.html") is False


def test_youku_provider_raises_when_vid_is_missing() -> None:
    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        return httpx.Response(200, text="<html>no vid</html>")

    provider = YoukuDanmakuProvider(get=fake_get)

    try:
        provider.resolve("https://v.youku.com/v_show/id_demo123.html")
    except Exception as exc:
        assert "vid" in str(exc)
    else:
        raise AssertionError("Expected Youku provider to reject pages without vid")
