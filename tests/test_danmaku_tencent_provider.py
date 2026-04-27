import httpx

from atv_player.danmaku.providers.tencent import TencentDanmakuProvider


def test_tencent_provider_search_maps_candidates_from_search_payload() -> None:
    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert "pbaccess.video.qq.com" in url
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

    provider = TencentDanmakuProvider(get=fake_get)

    items = provider.search("剑来 第1集")

    assert len(items) == 1
    assert items[0].provider == "tencent"
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
            return httpx.Response(200, text='<script>var DATA={"videoId":"vid123"};</script>')
        if "dm.video.qq.com/barrage/segment/vid123" in url and url.endswith("/0"):
            return httpx.Response(
                200,
                json={
                    "barrage_list": [
                        {
                            "time_offset": 1.5,
                            "content": "第一条",
                            "content_style": {"position": 1, "color": 16777215},
                        }
                    ]
                },
            )
        if "dm.video.qq.com/barrage/segment/vid123" in url and url.endswith("/1"):
            return httpx.Response(
                200,
                json={
                    "barrage_list": [
                        {
                            "time_offset": 2.0,
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
        provider.resolve("https://v.qq.com/x/cover/demo/vid123.html")
    except Exception as exc:
        assert "videoId" in str(exc)
    else:
        raise AssertionError("Expected Tencent provider to reject pages without videoId")
