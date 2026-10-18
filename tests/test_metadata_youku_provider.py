import httpx

from atv_player.metadata.models import MetadataQuery
from atv_player.metadata.providers.youku import YoukuMetadataProvider


def test_youku_metadata_provider_search_maps_page_component_to_series_match() -> None:
    def fake_get(url: str, **kwargs):
        assert url == "https://search.youku.com/api/search"
        assert kwargs["params"]["keyword"] == "名侦探柯南"
        return httpx.Response(
            200,
            json={
                "pageComponentList": [
                    {
                        "commonData": {
                            "isYouku": 1,
                            "titleDTO": {"displayName": "名侦探柯南"},
                            "summary": "高中生侦探工藤新一被黑衣组织灌下毒药。",
                            "posterDTO": {"url": "https://img.youku.com/conan.jpg"},
                            "year": "1996",
                            "area": "日本",
                            "category": "动漫 / 推理",
                            "director": "儿玉兼嗣",
                            "actor": "高山南 / 山崎和佳奈",
                            "updateNotice": "更新至第1234集",
                            "cornerMark": "独播",
                        },
                        "componentMap": {
                            "1035": {
                                "data": [
                                    {
                                        "videoId": "XNjUzNDA5NDEwNA==",
                                        "title": "名侦探柯南 01",
                                    }
                                ]
                            }
                        },
                    }
                ]
            },
        )

    provider = YoukuMetadataProvider(get=fake_get)

    matches = provider.search(MetadataQuery(title="名侦探柯南", year="1996", category_name="动漫"))

    assert len(matches) == 1
    assert matches[0].provider == "youku"
    assert matches[0].title == "名侦探柯南"
    assert matches[0].provider_id == "https://v.youku.com/v_show/id_XNjUzNDA5NDEwNA==.html"

    record = provider.get_detail(matches[0])

    assert record.provider == "youku"
    assert record.title == "名侦探柯南"
    assert record.year == "1996"
    assert record.poster == "https://img.youku.com/conan.jpg"
    assert record.overview == "高中生侦探工藤新一被黑衣组织灌下毒药。"
    assert record.country == "日本"
    assert record.directors == ["儿玉兼嗣"]
    assert record.actors == ["高山南", "山崎和佳奈"]
    assert record.genres == ["动漫", "推理"]
    assert record.detail_fields == [
        {"label": "更新状态", "value": "更新至第1234集"},
        {"label": "优酷标签", "value": "独播"},
        {"label": "播放链接", "value": "https://v.youku.com/v_show/id_XNjUzNDA5NDEwNA==.html"},
    ]


def test_youku_metadata_provider_detail_exposes_site_metrics() -> None:
    provider = YoukuMetadataProvider(
        get=lambda url, **kwargs: httpx.Response(
            200,
            json={
                "pageComponentList": [
                    {
                        "commonData": {
                            "isYouku": 1,
                            "titleDTO": {"displayName": "名侦探柯南"},
                            "summary": "高中生侦探工藤新一被黑衣组织灌下毒药。",
                            "year": "1996",
                            "category": "动漫 / 推理",
                            "score": 9.5,
                            "heat": {"displayName": "8500"},
                            "commentCount": "3.4万",
                            "leftButtonDTO": {
                                "action": {
                                    "value": "https://v.youku.com/v_show/id_XNjUzNDA5NDEwNA==.html"
                                }
                            },
                        },
                    }
                ]
            },
        )
    )

    match = provider.search(MetadataQuery(title="名侦探柯南", year="1996", category_name="动漫"))[0]
    record = provider.get_detail(match)

    assert {"label": "站内评分", "value": "9.5"} in record.detail_fields
    assert {"label": "热度", "value": "8500"} in record.detail_fields
    assert {"label": "评论", "value": "3.4万"} in record.detail_fields


def test_youku_metadata_provider_search_maps_series_payload_to_series_match() -> None:
    provider = YoukuMetadataProvider(
        get=lambda url, **kwargs: httpx.Response(
            200,
            json={
                "serisesList": [
                    {
                        "videoId": "XMjQ4MTc0ODMyOA==",
                        "title": "月鳞绮纪 01",
                        "img": "https://img.youku.com/yuelin.jpg",
                        "desc": "一段东方幻想冒险。",
                        "year": 2025,
                        "area": "中国大陆",
                        "type": "动漫",
                    }
                ]
            },
        )
    )

    matches = provider.search(MetadataQuery(title="月鳞绮纪", category_name="动漫"))

    assert [(match.title, match.provider_id) for match in matches] == [
        ("月鳞绮纪", "https://v.youku.com/v_show/id_XMjQ4MTc0ODMyOA==.html"),
    ]
    record = provider.get_detail(matches[0])
    assert record.poster == "https://img.youku.com/yuelin.jpg"
    assert record.overview == "一段东方幻想冒险。"
    assert record.year == "2025"
    assert record.country == "中国大陆"
    assert record.genres == ["动漫"]


def test_youku_metadata_provider_search_maps_nested_action_and_common_metadata() -> None:
    provider = YoukuMetadataProvider(
        get=lambda url, **kwargs: httpx.Response(
            200,
            json={
                "pageComponentList": [
                    {
                        "commonData": {
                            "titleDTO": {"displayName": "黑夜告白"},
                            "descDTO": {"displayName": "一场跨越多年的刑侦谜案。"},
                            "coverDTO": {"url": "https://img.youku.com/heiye.jpg"},
                            "showYear": "2026",
                            "areaDTO": {"displayName": "中国大陆"},
                            "typeDTO": {"displayName": "剧集 / 悬疑"},
                            "directorDTO": {"displayName": "导演甲"},
                            "actorDTO": {"displayName": "潘粤明 / 王鹤棣"},
                            "updateStatus": "更新至第4集",
                            "leftButtonDTO": {
                                "action": {
                                    "value": "youku://play?source=search&vid=XNjUxODQ1MzE1Ng==&showid=dccc1a382ea3456eaa77"
                                }
                            },
                        },
                    }
                ]
            },
        )
    )

    matches = provider.search(MetadataQuery(title="黑夜告白", year="2026", category_name="剧集"))

    assert len(matches) == 1
    assert matches[0].title == "黑夜告白"
    assert matches[0].provider_id == "https://v.youku.com/v_show/id_XNjUxODQ1MzE1Ng==.html"
    record = provider.get_detail(matches[0])
    assert record.year == "2026"
    assert record.poster == "https://img.youku.com/heiye.jpg"
    assert record.overview == "一场跨越多年的刑侦谜案。"
    assert record.country == "中国大陆"
    assert record.directors == ["导演甲"]
    assert record.actors == ["潘粤明", "王鹤棣"]
    assert record.genres == ["剧集", "悬疑"]
    assert {"label": "更新状态", "value": "更新至第4集"} in record.detail_fields


def test_youku_metadata_provider_search_maps_real_program_card_metadata() -> None:
    provider = YoukuMetadataProvider(
        get=lambda url, **kwargs: httpx.Response(
            200,
            json={
                "pageComponentList": [
                    {
                        "commonData": {
                            "showId": "dccc1a382ea3456eaa77",
                            "feature": "2026 · 电视剧 · 中国 · 28集全(完结)",
                            "director": "导演：王之 ",
                            "notice": "演员：潘粤明 王鹤棣 任敏 姜珮瑶 ",
                            "stripeBottom": "28集全",
                            "sourceName": "优酷",
                            "isYouku": 1,
                            "hasYouku": 1,
                            "posterDTO": {
                                "iconCorner": {"tagText": "独播"},
                                "vThumbUrl": "http://r1.ykimg.com/0526000069ECA155C54E4913B6BBA904",
                            },
                            "leftButtonDTO": {
                                "action": {"value": "https://v.youku.com/v_show/id_XNjUxODQ1MzE1Ng==.html"}
                            },
                            "titleDTO": {"displayName": "黑夜告白"},
                        },
                        "componentMap": {
                            "1035": {
                                "data": [
                                    {
                                        "videoId": "XNjUxODQ1MzE1Ng==",
                                        "title": "黑夜告白 01",
                                    }
                                ]
                            }
                        },
                    }
                ]
            },
        )
    )

    matches = provider.search(MetadataQuery(title="黑夜告白", year="2026", category_name="剧集"))

    assert len(matches) == 1
    record = provider.get_detail(matches[0])
    assert record.year == "2026"
    assert record.poster == "http://r1.ykimg.com/0526000069ECA155C54E4913B6BBA904"
    assert record.country == "中国"
    assert record.directors == ["王之"]
    assert record.actors == ["潘粤明", "王鹤棣", "任敏", "姜珮瑶"]
    assert record.genres == []
    assert {"label": "更新状态", "value": "28集全"} in record.detail_fields
    assert {"label": "优酷标签", "value": "独播"} in record.detail_fields


def test_youku_metadata_provider_versions_search_and_detail_cache_keys() -> None:
    provider = YoukuMetadataProvider(get=lambda url, **kwargs: httpx.Response(200, json={}))

    assert provider.search_cache_key(MetadataQuery(title="黑夜告白", year="2026")) == (
        "黑夜告白",
        "2026#metadata-v3",
    )
    assert (
        provider.detail_cache_key("https://v.youku.com/v_show/id_XNjUxODQ1MzE1Ng==.html")
        == "https://v.youku.com/v_show/id_XNjUxODQ1MzE1Ng==.html:metadata-v3"
    )


def test_youku_metadata_provider_strips_alias_annotation_from_titles() -> None:
    # 优酷展示名自带的 "(别名:Garfield S01)" 注记里的外文名会作为子串参与
    # 标题匹配,把退化查询("S01")顶过置信线;搜索与详情两侧都要剥掉
    provider = YoukuMetadataProvider(
        get=lambda url, **kwargs: httpx.Response(
            200,
            json={
                "pageComponentList": [
                    {
                        "commonData": {
                            "titleDTO": {"displayName": "加菲猫的幸福生活 第一季(别名:Garfield S01)"},
                            "descDTO": {"displayName": "加菲猫一家的日常。"},
                            "coverDTO": {"url": "https://img.youku.com/garfield.jpg"},
                            "showYear": "2009",
                            "leftButtonDTO": {
                                "action": {
                                    "value": "youku://play?source=search&vid=XNjUzNDA5NDEwNA==&showid=garfield"
                                }
                            },
                        },
                    }
                ]
            },
        )
    )

    matches = provider.search(MetadataQuery(title="加菲猫的幸福生活"))

    assert [match.title for match in matches] == ["加菲猫的幸福生活 第一季"]
    assert provider.get_detail(matches[0]).title == "加菲猫的幸福生活 第一季"
