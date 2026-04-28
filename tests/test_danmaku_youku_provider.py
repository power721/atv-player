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


def test_youku_provider_search_maps_episode_candidates_from_page_component_payload() -> None:
    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert "search.youku.com" in url
        assert params == {
            "keyword": "黑夜告白",
            "userAgent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            ),
            "site": 1,
            "categories": 0,
            "ftype": 0,
            "ob": 0,
            "pg": 1,
        }
        return httpx.Response(
            200,
            json={
                "pageComponentList": [
                    {
                        "commonData": {
                            "isYouku": 1,
                            "hasYouku": 1,
                            "titleDTO": {"displayName": "黑夜告白"},
                        },
                        "componentMap": {
                            "1035": {
                                "data": [
                                    {
                                        "videoId": "XNjUxODQ1MzE1Ng==",
                                        "title": "黑夜告白 01",
                                        "action": {
                                            "value": "youku://play?source=search&vid=XNjUxODQ1MzE1Ng==&showid=dccc1a382ea3456eaa77"
                                        },
                                    },
                                    {
                                        "videoId": "XNjUxOTE5NjQwOA==",
                                        "title": "黑夜告白 02",
                                        "action": {
                                            "value": "youku://play?source=search&vid=XNjUxOTE5NjQwOA==&showid=dccc1a382ea3456eaa77"
                                        },
                                    },
                                ]
                            }
                        },
                    }
                ]
            },
        )

    provider = YoukuDanmakuProvider(get=fake_get)

    items = provider.search("黑夜告白")

    assert [(item.name, item.url) for item in items] == [
        ("黑夜告白 01", "https://v.youku.com/v_show/id_XNjUxODQ1MzE1Ng==.html"),
        ("黑夜告白 02", "https://v.youku.com/v_show/id_XNjUxOTE5NjQwOA==.html"),
    ]


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


def test_youku_provider_search_drops_series_payload_when_title_does_not_match_query() -> None:
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
                        "title": "三生三世十里桃花 01",
                        "displayName": "1",
                        "showVideoStage": "第1期",
                    }
                ],
            },
        )

    provider = YoukuDanmakuProvider(get=fake_get)

    items = provider.search("黑夜告白")

    assert items == []


def test_youku_provider_resolve_fetches_signed_segments_using_vid_from_url() -> None:
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
            return httpx.Response(200, text="<html><title>demo</title></html>")
        if url == (
            "https://openapi.youku.com/v2/videos/show.json"
            "?client_id=53e6cc67237fc59a&video_id=demo123&package=com.huawei.hwvplayer.youku&ext=show"
        ):
            return httpx.Response(200, json={"duration": "61.0"})
        if url == "https://log.mmstat.com/eg.js":
            return httpx.Response(200, headers={"etag": '"demo-cna"'})
        if url == "https://acs.youku.com/h5/mtop.com.youku.aplatform.weakget/1.0/?jsv=2.5.1&appKey=24679788":
            return httpx.Response(
                200,
                headers={
                    "set-cookie": (
                        "_m_h5_tk=abcdefghijklmnopqrstuvwxyz123456_123;Path=/;Domain=youku.com;Max-Age=86400, "
                        "_m_h5_tk_enc=enc-cookie;Path=/;Domain=youku.com;Max-Age=86400"
                    )
                },
            )
        raise AssertionError(url)

    def fake_post(
        url: str,
        params: dict | None = None,
        data: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert url == "https://acs.youku.com/h5/mopen.youku.danmu.list/1.0/"
        assert params is not None
        assert params["api"] == "mopen.youku.danmu.list"
        assert params["appKey"] == "24679788"
        assert data is not None and "data" in data
        payload = __import__("json").loads(data["data"])
        assert payload["vid"] == "demo123"
        assert payload["guid"] == "demo-cna"
        mat = payload["mat"]
        if mat == 0:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "result": __import__("json").dumps(
                            {
                                "code": 1,
                                "data": {
                                    "result": [
                                        {"playat": 1500, "propertis": '{"pos":1,"color":16777215}', "content": "优酷第一条"}
                                    ]
                                },
                            }
                        )
                    }
                },
            )
        if mat == 1:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "result": __import__("json").dumps(
                            {
                                "code": 1,
                                "data": {
                                    "result": [
                                        {"playat": 3200, "propertis": '{"pos":4,"color":255}', "content": "优酷第二条"}
                                    ]
                                },
                            }
                        )
                    }
                },
            )
        raise AssertionError(mat)

    provider = YoukuDanmakuProvider(get=fake_get, post=fake_post)

    records = provider.resolve("https://v.youku.com/v_show/id_demo123.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (1.5, 1, "16777215", "优酷第一条"),
        (3.2, 4, "255", "优酷第二条"),
    ]
    assert calls[0] == "https://v.youku.com/v_show/id_demo123.html"


def test_youku_provider_resolve_tolerates_segment_timeout_when_other_segments_succeed() -> None:
    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        if url == "https://v.youku.com/v_show/id_demo123.html":
            return httpx.Response(200, text="<html><title>demo</title></html>")
        if url == (
            "https://openapi.youku.com/v2/videos/show.json"
            "?client_id=53e6cc67237fc59a&video_id=demo123&package=com.huawei.hwvplayer.youku&ext=show"
        ):
            return httpx.Response(200, json={"duration": "61.0"})
        if url == "https://log.mmstat.com/eg.js":
            return httpx.Response(200, headers={"etag": '"demo-cna"'})
        if url == "https://acs.youku.com/h5/mtop.com.youku.aplatform.weakget/1.0/?jsv=2.5.1&appKey=24679788":
            return httpx.Response(
                200,
                headers={
                    "set-cookie": (
                        "_m_h5_tk=abcdefghijklmnopqrstuvwxyz123456_123;Path=/;Domain=youku.com;Max-Age=86400, "
                        "_m_h5_tk_enc=enc-cookie;Path=/;Domain=youku.com;Max-Age=86400"
                    )
                },
            )
        raise AssertionError(url)

    def fake_post(
        url: str,
        params: dict | None = None,
        data: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        payload = __import__("json").loads((data or {})["data"])
        if payload["mat"] == 0:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "result": __import__("json").dumps(
                            {
                                "code": 1,
                                "data": {"result": [{"playat": 1500, "propertis": '{"pos":1}', "content": "优酷第一条"}]},
                            }
                        )
                    }
                },
            )
        raise httpx.ReadTimeout("timed out")

    provider = YoukuDanmakuProvider(get=fake_get, post=fake_post)

    records = provider.resolve("https://v.youku.com/v_show/id_demo123.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (1.5, 1, "16777215", "优酷第一条"),
    ]


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
        provider.resolve("https://v.youku.com/v_show/demo123.html")
    except Exception as exc:
        assert "vid" in str(exc)
    else:
        raise AssertionError("Expected Youku provider to reject pages without vid")
