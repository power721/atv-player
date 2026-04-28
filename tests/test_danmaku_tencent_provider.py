import httpx

from atv_player.danmaku.providers.tencent import TencentDanmakuProvider


def test_tencent_provider_search_maps_candidates_from_search_payload() -> None:
    def fake_post(
        url: str,
        content: str | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert (
            url
            == "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch?vplatform=2"
        )
        assert content is not None
        assert '"query":"剑来"' in content
        assert '"pagenum":0' in content
        assert '"pagesize":30' in content
        assert headers == {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; M2104K10AC Build/TP1A.220624.014) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/131.0.6778.200 "
                "Mobile Safari/537.36"
            ),
            "Content-Type": "application/json",
            "Origin": "https://v.qq.com",
            "Referer": "https://v.qq.com/",
        }
        return httpx.Response(
            200,
            json={
                "data": {
                    "normalList": {
                        "itemList": [
                            {
                                "videoInfo": {
                                    "title": "剑来 第1集",
                                    "url": "https://v.qq.com/x/cover/demo/vid123.html",
                                }
                            }
                        ]
                    }
                }
            },
        )

    provider = TencentDanmakuProvider(post=fake_post)

    items = provider.search("剑来")

    assert len(items) == 1
    assert items[0].provider == "tencent"
    assert items[0].name == "剑来 第1集"
    assert items[0].url == "https://v.qq.com/x/cover/demo/vid123.html"


def test_tencent_provider_search_filters_txvideo_links_and_expands_numeric_episode_titles() -> None:
    def fake_post(
        url: str,
        content: str | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        return httpx.Response(
            200,
            json={
                "data": {
                    "normalList": {
                        "itemList": [
                            {
                                "videoInfo": {
                                    "title": "10",
                                    "url": "https://v.qq.com/x/cover/mzc00200xxpsogl/t4101te90vx.html",
                                }
                            },
                            {
                                "videoInfo": {
                                    "title": "剑来 第二季",
                                    "url": "txvideo://v.qq.com/HomeActivity?tabIndex=17&searchType=1",
                                }
                            },
                            {
                                "videoInfo": {
                                    "title": "11",
                                    "url": "https://v.qq.com/x/cover/mzc00200xxpsogl/n4101ho78vs.html",
                                }
                            },
                        ]
                    }
                }
            },
        )

    provider = TencentDanmakuProvider(post=fake_post)

    items = provider.search("剑来 第二季 10集")

    assert [(item.name, item.url) for item in items] == [
        ("剑来 第二季 10集", "https://v.qq.com/x/cover/mzc00200xxpsogl/t4101te90vx.html"),
        ("剑来 第二季 11集", "https://v.qq.com/x/cover/mzc00200xxpsogl/n4101ho78vs.html"),
    ]


def test_tencent_provider_search_filters_preview_episode_candidates_from_episode_info_list() -> None:
    def fake_post(
        url: str,
        content: str | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        return httpx.Response(
            200,
            json={
                "data": {
                    "normalList": {
                        "itemList": [
                            {
                                "videoInfo": {
                                    "title": "剑来 第二季",
                                    "episodeInfoList": [
                                        {
                                            "title": "10",
                                            "url": "https://v.qq.com/x/cover/mzc00200xxpsogl/t4101te90vx.html",
                                            "markLabel": '{"2":{"info":{"text":"预告"}}}',
                                            "rawTags": '{"tag_2":{"text":"预"}}',
                                            "duration": "56",
                                        },
                                        {
                                            "title": "10",
                                            "url": "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html",
                                            "markLabel": '{"2":{"info":{"text":"VIP"}}}',
                                            "rawTags": '{"tag_2":{"text":"VIP-包月only"}}',
                                            "duration": "1826",
                                        },
                                    ]
                                }
                            }
                        ]
                    }
                }
            },
        )

    provider = TencentDanmakuProvider(post=fake_post)

    items = provider.search("剑来 第二季 10集")

    assert [(item.name, item.url) for item in items] == [
        ("剑来 第二季 10集", "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html"),
    ]


def test_tencent_provider_search_falls_back_to_web_when_mbsearch_returns_business_error() -> None:
    def fake_post(
        url: str,
        content: str | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        return httpx.Response(200, json={"ret": 1001, "msg": "rate limited"})

    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert url == "https://v.qq.com/x/search/?q=%E5%89%91%E6%9D%A5"
        return httpx.Response(
            200,
            text=(
                '<html><body>'
                '<a href="https://v.qq.com/x/cover/demo/vid123.html" title="剑来 第1集"></a>'
                "</body></html>"
            ),
        )

    provider = TencentDanmakuProvider(get=fake_get, post=fake_post)

    items = provider.search("剑来")

    assert len(items) == 1
    assert items[0].name == "剑来 第1集"
    assert items[0].url == "https://v.qq.com/x/cover/demo/vid123.html"


def test_tencent_provider_search_falls_back_to_web_when_mbsearch_http_fails() -> None:
    def fake_post(
        url: str,
        content: str | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        raise httpx.HTTPError("boom")

    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert url == "https://v.qq.com/x/search/?q=%E5%89%91%E6%9D%A5"
        return httpx.Response(
            200,
            text=(
                '<html><body>'
                '<a href="https://v.qq.com/x/cover/demo/vid123.html" title="剑来 第1集"></a>'
                "</body></html>"
            ),
        )

    provider = TencentDanmakuProvider(get=fake_get, post=fake_post)

    items = provider.search("剑来")

    assert len(items) == 1
    assert items[0].name == "剑来 第1集"
    assert items[0].url == "https://v.qq.com/x/cover/demo/vid123.html"


def test_tencent_provider_resolve_extracts_video_id_and_merges_segments() -> None:
    calls: list[str] = []

    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        calls.append(url)
        if url == "https://v.qq.com/x/cover/demo/vid123.html":
            return httpx.Response(200, text='<script>var DATA={"videoId":"vid123","duration":61};</script>')
        if url == "https://dm.video.qq.com/barrage/segment/vid123/t/v1/0/30000":
            return httpx.Response(
                200,
                json={
                        "barrage_list": [
                            {
                                "time_offset": 1500,
                                "content": "第一条",
                                "content_style": {"position": 1, "color": 16777215},
                            },
                            {
                                "time_offset": 1500,
                                "content": "第一条",
                                "content_style": {"position": 1, "color": 16777215},
                            },
                    ]
                },
            )
        if url == "https://dm.video.qq.com/barrage/segment/vid123/t/v1/30000/60000":
            return httpx.Response(
                200,
                json={
                        "barrage_list": [
                            {
                                "time_offset": 2000,
                                "content": "第二条",
                                "content_style": {"position": 4, "color": 255},
                            }
                    ]
                },
            )
        return httpx.Response(200, json={"barrage_list": []})

    provider = TencentDanmakuProvider(get=fake_get)

    records = provider.resolve("https://v.qq.com/x/cover/demo/vid123.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (1.5, 1, "16777215", "第一条"),
        (2.0, 4, "255", "第二条"),
    ]
    assert calls[0] == "https://v.qq.com/x/cover/demo/vid123.html"
    assert "https://dm.video.qq.com/barrage/segment/vid123/t/v1/0/30000" in calls
    assert "https://dm.video.qq.com/barrage/segment/vid123/t/v1/30000/60000" in calls


def test_tencent_provider_resolve_uses_url_vid_and_millisecond_ranges_when_page_has_no_embedded_vid() -> None:
    calls: list[str] = []

    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        calls.append(url)
        if url == "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html":
            return httpx.Response(
                200,
                text=(
                    '<html><head><title>剑来2_10</title>'
                    '<meta property="video:duration" content="1826">'
                    "</head><body></body></html>"
                ),
            )
        if url == "https://dm.video.qq.com/barrage/segment/h4101bl5ftq/t/v1/0/30000":
            return httpx.Response(
                200,
                json={
                    "barrage_list": [
                        {
                            "time_offset": "0",
                            "content": "第一条",
                            "content_style": "",
                        },
                        {
                            "time_offset": "27000",
                            "content": "第二条",
                            "content_style": "",
                        },
                    ]
                },
            )
        if url == "https://dm.video.qq.com/barrage/segment/h4101bl5ftq/t/v1/30000/60000":
            return httpx.Response(200, json={"barrage_list": []})
        return httpx.Response(404, json={})

    provider = TencentDanmakuProvider(get=fake_get)

    records = provider.resolve("https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html")

    assert [(record.time_offset, record.pos, record.color, record.content) for record in records] == [
        (0.0, 1, "16777215", "第一条"),
        (27.0, 1, "16777215", "第二条"),
    ]
    assert calls[0] == "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html"
    assert "https://dm.video.qq.com/barrage/segment/h4101bl5ftq/t/v1/0/30000" in calls
    assert "https://dm.video.qq.com/barrage/segment/h4101bl5ftq/t/v1/30000/60000" in calls


def test_tencent_provider_supports_vqq_urls() -> None:
    provider = TencentDanmakuProvider()

    assert provider.supports("https://v.qq.com/x/cover/demo/vid123.html") is True
    assert provider.supports("https://v.youku.com/v_show/id_demo.html") is False


def test_tencent_provider_raises_when_video_id_is_missing() -> None:
    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        return httpx.Response(200, text="<html>no id</html>")

    provider = TencentDanmakuProvider(get=fake_get)

    try:
        provider.resolve("https://v.qq.com/x/cover/demo.html")
    except Exception as exc:
        assert "videoId" in str(exc)
    else:
        raise AssertionError("Expected Tencent provider to reject pages without videoId")
