import httpx

from atv_player.danmaku.providers.tencent import TencentDanmakuProvider


def test_tencent_provider_search_maps_candidates_from_search_payload() -> None:
    def fake_get(
        url: str,
        content: str | None = None,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert url == "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch"
        assert params == {
            "q": "剑来",
            "query": "剑来",
            "vversion_platform": "2",
            "page_num": "1",
            "page_size": "20",
            "req_from": "web",
        }
        assert headers == {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Origin": "https://v.qq.com",
            "Referer": "https://v.qq.com/",
            "trpc-trans-info": '{"trpc-env":""}',
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

    provider = TencentDanmakuProvider(get=fake_get)

    items = provider.search("剑来")

    assert len(items) == 1
    assert items[0].provider == "tencent"
    assert items[0].name == "剑来 第1集"
    assert items[0].url == "https://v.qq.com/x/cover/demo/vid123.html"


def test_tencent_provider_search_uses_stripped_keyword_for_episode_queries() -> None:
    calls: list[dict[str, str]] = []

    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert params is not None
        calls.append(params)
        if params.get("query") == "蜜语纪":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "normalList": {
                            "itemList": [
                                {
                                    "videoInfo": {
                                        "title": "16",
                                        "url": "https://v.qq.com/x/cover/mzc002006dzzunf/u4102abc016.html",
                                    }
                                }
                            ]
                        }
                    }
                },
            )
        raise AssertionError(str(params))

    provider = TencentDanmakuProvider(get=fake_get)

    items = provider.search("蜜语纪 16集")

    assert [(item.name, item.url) for item in items] == [
        ("蜜语纪 16集", "https://v.qq.com/x/cover/mzc002006dzzunf/u4102abc016.html"),
    ]
    assert len(calls) == 1
    assert calls[0]["query"] == "蜜语纪"


def test_tencent_provider_search_filters_txvideo_links_and_expands_numeric_episode_titles() -> None:
    def fake_get(
        url: str,
        params: dict | None = None,
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

    provider = TencentDanmakuProvider(get=fake_get)

    items = provider.search("剑来 第二季 10集")

    assert [(item.name, item.url) for item in items] == [
        ("剑来 第二季 10集", "https://v.qq.com/x/cover/mzc00200xxpsogl/t4101te90vx.html"),
        ("剑来 第二季 11集", "https://v.qq.com/x/cover/mzc00200xxpsogl/n4101ho78vs.html"),
    ]


def test_tencent_provider_search_filters_preview_episode_candidates_from_episode_info_list() -> None:
    def fake_get(
        url: str,
        params: dict | None = None,
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

    provider = TencentDanmakuProvider(get=fake_get)

    items = provider.search("剑来 第二季 10集")

    assert [(item.name, item.url) for item in items] == [
        ("剑来 第二季 10集", "https://v.qq.com/x/cover/mzc00200xxpsogl/h4101bl5ftq.html"),
    ]


def test_tencent_provider_search_expands_episode_list_from_candidate_detail_page() -> None:
    def fake_get(
        url: str,
        headers: dict | None = None,
        params: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        if url == "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "normalList": {
                            "itemList": [
                                {
                                    "videoInfo": {
                                        "title": "蜜语纪",
                                        "url": "https://v.qq.com/x/cover/mzc002006dzzunf/k410266zdm1.html",
                                    }
                                }
                            ]
                        }
                    }
                },
            )
        assert url == "https://v.qq.com/x/cover/mzc002006dzzunf/k410266zdm1.html"
        return httpx.Response(
            200,
            text=(
                '<script>window.__STATE__={"vsite_episode_list":[{"title":"15","url":"https://v.qq.com/x/cover/'
                'mzc002006dzzunf/u4102abc015.html","duration":"1826"},{"title":"16","url":"https://v.qq.com/x/cover/'
                'mzc002006dzzunf/u4102abc016.html","duration":"1826"}]};</script>'
            ),
        )

    def fake_post(
        url: str,
        content: str | None = None,
        json: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        raise httpx.HTTPError("page data unavailable")

    provider = TencentDanmakuProvider(get=fake_get, post=fake_post)

    items = provider.search("蜜语纪 15集")

    assert ("蜜语纪 15集", "https://v.qq.com/x/cover/mzc002006dzzunf/u4102abc015.html") in [
        (item.name, item.url) for item in items
    ]


def test_tencent_provider_search_extracts_episode_links_from_detail_html_items() -> None:
    def fake_get(
        url: str,
        headers: dict | None = None,
        params: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        if url == "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch":
            assert params is not None
            assert params["query"] == "蜜语纪"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "normalList": {
                            "itemList": [
                                {
                                    "videoInfo": {
                                        "title": "蜜语纪 1集",
                                        "url": "https://v.qq.com/x/cover/mzc002006dzzunf/h4102lz1osw.html",
                                    }
                                }
                            ]
                        }
                    }
                },
            )
        assert url == "https://v.qq.com/x/cover/mzc002006dzzunf/h4102lz1osw.html"
        return httpx.Response(
            200,
            text=(
                '<div id="video-pc_web_episode_list_mzc002006dzzunf_v41021ycs9o_33_main" '
                'data-video-idx="33" class="episode-item normal-font" '
                'title="明星即将入住！纪总拿捏小段总，让出总统套房" '
                'dt-params="cid=mzc002006dzzunf&amp;item_idx=33&amp;vid=v41021ycs9o">'
                '<span class="episode-item-text">16</span>'
                '<div class="corner-wrap"><span class="text">VIP</span></div>'
                "</div>"
            ),
        )

    provider = TencentDanmakuProvider(get=fake_get)

    items = provider.search("蜜语纪 16集")

    assert ("蜜语纪 16集", "https://v.qq.com/x/cover/mzc002006dzzunf/v41021ycs9o.html") in [
        (item.name, item.url) for item in items
    ]


def test_tencent_provider_search_prefers_full_episode_over_preview_from_union_detail_data() -> None:
    def fake_get(
        url: str,
        headers: dict | None = None,
        params: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        if url == "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch":
            assert params is not None
            assert params["query"] == "蜜语纪"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "normalList": {
                            "itemList": [
                                {
                                    "videoInfo": {
                                        "title": "蜜语纪",
                                        "url": "https://v.qq.com/x/cover/mzc002006dzzunf/h4102lz1osw.html",
                                    }
                                }
                            ]
                        }
                    }
                },
            )
        if url == "https://v.qq.com/x/cover/mzc002006dzzunf/h4102lz1osw.html":
            return httpx.Response(
                200,
                text=(
                    '<script>window.__vikor__context__.ssrPayloads=(function(a,b){return {_piniaState:{union:{'
                    'coverInfoMap:{mzc002006dzzunf:{video_ids:["z4102434xj2","v41021ycs9o"],cid:"mzc002006dzzunf"}}'
                    "}}}}({},{}));</script>"
                ),
            )
        if url == "https://union.video.qq.com/fcgi-bin/data":
            assert params == {
                "otype": "json",
                "tid": "1804",
                "appid": "20001238",
                "appkey": "6c03bbe9658448a4",
                "union_platform": "1",
                "idlist": "z4102434xj2,v41021ycs9o",
            }
            return httpx.Response(
                200,
                text=(
                    'QZOutputJson={"errorno":0,"results":['
                    '{"id":"z4102434xj2","retcode":0,"fields":{"vid":"z4102434xj2","c_covers":"mzc002006dzzunf",'
                    '"c_title_output":"16集预告","title":"《蜜语纪》预告片_16","category_map":[10479,"预告片"]}},'
                    '{"id":"v41021ycs9o","retcode":0,"fields":{"vid":"v41021ycs9o","c_covers":"mzc002006dzzunf",'
                    '"c_title_output":"16","title":"蜜语纪_16","category_map":[10470,"正片"]}}]};'
                ),
            )
        raise AssertionError(url)

    provider = TencentDanmakuProvider(get=fake_get)

    items = provider.search("蜜语纪 16集")

    matching = [(item.name, item.url) for item in items if item.name == "蜜语纪 16集"]
    assert matching == [("蜜语纪 16集", "https://v.qq.com/x/cover/mzc002006dzzunf/v41021ycs9o.html")]


def test_tencent_provider_search_expands_episode_list_from_page_data_api() -> None:
    page_contexts: list[str] = []

    def fake_get(
        url: str,
        headers: dict | None = None,
        params: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        if url == "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch":
            assert params is not None
            assert params["query"] == "蜜语纪"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "normalList": {
                            "itemList": [
                                {
                                    "videoInfo": {
                                        "title": "蜜语纪",
                                        "url": "https://v.qq.com/x/cover/mzc002006dzzunf/h4102lz1osw.html",
                                    }
                                }
                            ]
                        }
                    }
                },
            )
        raise AssertionError(url)

    def fake_post(
        url: str,
        content: str | None = None,
        json: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert url == (
            "https://pbaccess.video.qq.com/trpc.universal_backend_service.page_server_rpc.PageServer/GetPageData"
            "?video_appid=3000010&vversion_name=8.2.96&vversion_platform=2"
        )
        payload = json
        assert payload is not None
        page_context = payload["page_params"]["page_context"]
        page_contexts.append(page_context)
        if page_context == "cid=mzc002006dzzunf&detail_page_type=1&req_from=web_vsite&req_from_second_type=&req_type=0":
            return httpx.Response(
                200,
                json={
                    "ret": 0,
                    "data": {
                        "module_list_datas": [
                            {
                                "module_datas": [
                                    {
                                        "module_params": {
                                            "tabs": (
                                                '[{"begin":1,"end":30,"selected":true,"page_context":"cid=mzc002006dzzunf'
                                                '&detail_page_type=1&episode_begin=1&episode_end=30"},'
                                                '{"begin":31,"end":33,"selected":false,"page_context":"cid=mzc002006dzzunf'
                                                '&detail_page_type=1&episode_begin=31&episode_end=33"}]'
                                            )
                                        },
                                        "item_data_lists": {
                                            "item_datas": [
                                                {
                                                    "item_params": {
                                                        "vid": "u4102abc029",
                                                        "title": "29",
                                                        "play_title": "蜜语纪 第29集",
                                                        "union_title": "蜜语纪_29",
                                                        "is_trailer": "0",
                                                    }
                                                },
                                                {
                                                    "item_params": {
                                                        "vid": "g41024s47bo",
                                                        "title": "30",
                                                        "play_title": "蜜语纪 30集预告",
                                                        "union_title": "《蜜语纪》预告片_30",
                                                        "is_trailer": "1",
                                                    }
                                                },
                                            ]
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                },
            )
        if page_context == "cid=mzc002006dzzunf&detail_page_type=1&episode_begin=31&episode_end=33":
            return httpx.Response(
                200,
                json={
                    "ret": 0,
                    "data": {
                        "module_list_datas": [
                            {
                                "module_datas": [
                                    {
                                        "item_data_lists": {
                                            "item_datas": [
                                                {
                                                    "item_params": {
                                                        "vid": "u4102abc031",
                                                        "title": "31",
                                                        "play_title": "蜜语纪 第31集",
                                                        "union_title": "蜜语纪_31",
                                                        "is_trailer": "0",
                                                    }
                                                },
                                                {
                                                    "item_params": {
                                                        "vid": "x3198drama1",
                                                        "title": "小剧场1",
                                                        "play_title": "小剧场1",
                                                        "union_title": "小剧场1",
                                                        "is_trailer": "0",
                                                    }
                                                },
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                },
            )
        raise AssertionError(page_context)

    provider = TencentDanmakuProvider(get=fake_get, post=fake_post)

    items = provider.search("蜜语纪 31集")

    assert ("蜜语纪 31集", "https://v.qq.com/x/cover/mzc002006dzzunf/u4102abc031.html") in [
        (item.name, item.url) for item in items
    ]
    assert not any("小剧场" in item.name or "预告" in item.name for item in items)
    assert page_contexts == [
        "cid=mzc002006dzzunf&detail_page_type=1&req_from=web_vsite&req_from_second_type=&req_type=0",
        "cid=mzc002006dzzunf&detail_page_type=1&episode_begin=31&episode_end=33",
    ]


def test_tencent_provider_search_falls_back_to_detail_html_when_page_data_is_unusable() -> None:
    detail_page_requested = False

    def fake_get(
        url: str,
        headers: dict | None = None,
        params: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        nonlocal detail_page_requested
        if url == "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch":
            assert params is not None
            assert params["query"] == "蜜语纪"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "normalList": {
                            "itemList": [
                                {
                                    "videoInfo": {
                                        "title": "蜜语纪",
                                        "url": "https://v.qq.com/x/cover/mzc002006dzzunf/h4102lz1osw.html",
                                    }
                                }
                            ]
                        }
                    }
                },
            )
        if url == "https://v.qq.com/x/cover/mzc002006dzzunf/h4102lz1osw.html":
            detail_page_requested = True
            return httpx.Response(
                200,
                text=(
                    '<script>window.__STATE__={"vsite_episode_list":[{"title":"15","url":"https://v.qq.com/x/cover/'
                    'mzc002006dzzunf/u4102abc015.html","duration":"1826"}]};</script>'
                ),
            )
        raise AssertionError(url)

    def fake_post(
        url: str,
        content: str | None = None,
        json: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert url == (
            "https://pbaccess.video.qq.com/trpc.universal_backend_service.page_server_rpc.PageServer/GetPageData"
            "?video_appid=3000010&vversion_name=8.2.96&vversion_platform=2"
        )
        return httpx.Response(
            200,
            json={
                "ret": 0,
                "data": {
                    "module_list_datas": [
                        {
                            "module_datas": [
                                {
                                    "item_data_lists": {
                                        "item_datas": [
                                            {
                                                "item_params": {
                                                    "vid": "x3198drama1",
                                                    "title": "小剧场1",
                                                    "play_title": "小剧场1",
                                                    "union_title": "小剧场1",
                                                    "is_trailer": "0",
                                                }
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    ]
                },
            },
        )

    provider = TencentDanmakuProvider(get=fake_get, post=fake_post)

    items = provider.search("蜜语纪 15集")

    assert detail_page_requested is True
    assert ("蜜语纪 15集", "https://v.qq.com/x/cover/mzc002006dzzunf/u4102abc015.html") in [
        (item.name, item.url) for item in items
    ]


def test_tencent_provider_search_returns_empty_when_mbsearch_returns_business_error() -> None:
    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert url == "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch"
        return httpx.Response(200, json={"ret": 1001, "msg": "rate limited"})

    provider = TencentDanmakuProvider(get=fake_get)

    items = provider.search("剑来")

    assert items == []


def test_tencent_provider_search_returns_empty_when_mbsearch_http_fails() -> None:
    def fake_get(
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        timeout: float = 10.0,
    ):
        assert url == "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch"
        raise httpx.HTTPError("boom")

    provider = TencentDanmakuProvider(get=fake_get)

    items = provider.search("剑来")

    assert items == []


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
