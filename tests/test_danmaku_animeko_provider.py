import threading
import time

import httpx
import pytest

from atv_player.danmaku.errors import DanmakuResolveError
from atv_player.danmaku.providers.animeko import AnimekoDanmakuProvider


def test_animeko_search_expands_main_episode_from_fallback_subject_node() -> None:
    get_calls: list[str] = []

    def fake_post(url: str, **kwargs):
        assert url == "https://api.bangumi.lol/v0/search/subjects"
        assert kwargs["json"] == {
            "keyword": "迷宫饭",
            "filter": {"type": [2]},
        }
        return httpx.Response(
            200,
            json={"data": [{"id": 42, "name": "ダンジョン飯", "name_cn": "迷宫饭"}]},
        )

    def fake_get(url: str, **kwargs):
        get_calls.append(url)
        if url == "https://node-a.example/v2/subjects/42":
            raise httpx.HTTPError("node a down")
        if url == "https://node-b.example/v2/subjects/42":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "episodes": [
                        {
                            "episodeId": 4201,
                            "sort": 1,
                            "type": "MAIN",
                            "nameCn": "水炖史莱姆",
                        },
                        {
                            "episodeId": 4299,
                            "sort": 1,
                            "type": "SP",
                            "nameCn": "特典",
                        },
                    ],
                },
            )
        raise AssertionError(url)

    provider = AnimekoDanmakuProvider(
        get=fake_get,
        post=fake_post,
        nodes=("https://node-a.example", "https://node-b.example"),
        search_nodes=("https://api.bangumi.lol",),
    )

    items = provider.search("迷宫饭", original_name="迷宫饭 第1集")

    assert [(item.provider, item.name, item.url) for item in items] == [
        ("animeko", "迷宫饭 第1集 水炖史莱姆", "animeko://episode/4201")
    ]
    assert get_calls == [
        "https://node-a.example/v2/subjects/42",
        "https://node-b.example/v2/subjects/42",
    ]


def test_animeko_resolve_falls_back_and_remembers_healthy_node() -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs):
        calls.append(url)
        if url.startswith("https://node-a.example"):
            raise httpx.HTTPError("node a down")
        return httpx.Response(
            200,
            json={
                "danmakuList": [
                    {
                        "id": 1,
                        "danmakuInfo": {
                            "playTime": 1250,
                            "location": "TOP",
                            "color": -1,
                            "text": "顶部",
                        },
                    }
                ]
            },
        )

    provider = AnimekoDanmakuProvider(
        get=fake_get,
        post=lambda *args, **kwargs: httpx.Response(200, json={"data": []}),
        nodes=("https://node-a.example", "https://node-b.example"),
    )

    first = provider.resolve("animeko://episode/4201")
    second = provider.resolve("animeko://episode/4202")

    assert [(r.time_offset, r.pos, r.color, r.content) for r in first] == [
        (1.25, 5, "16777215", "顶部")
    ]
    assert len(second) == 1
    assert calls == [
        "https://node-a.example/v1/danmaku/4201",
        "https://node-b.example/v1/danmaku/4201",
        "https://node-b.example/v1/danmaku/4202",
    ]


def test_animeko_valid_empty_comments_do_not_try_other_nodes() -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={"danmakuList": []})

    provider = AnimekoDanmakuProvider(
        get=fake_get,
        post=lambda *args, **kwargs: httpx.Response(200, json={"data": []}),
        nodes=("https://node-a.example", "https://node-b.example"),
    )

    assert provider.resolve("animeko://episode/4201") == []
    assert calls == ["https://node-a.example/v1/danmaku/4201"]


def test_animeko_search_expands_subject_details_concurrently() -> None:
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_post(url: str, **kwargs):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": subject_id, "name_cn": "迷宫饭"}
                    for subject_id in range(1, 5)
                ]
            },
        )

    def fake_get(url: str, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        subject_id = url.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "id": subject_id,
                "episodes": [
                    {
                        "episodeId": f"{subject_id}01",
                        "sort": 1,
                        "type": "MAIN",
                    }
                ],
            },
        )

    AnimekoDanmakuProvider(
        get=fake_get,
        post=fake_post,
        nodes=("https://node.example",),
    ).search("迷宫饭")

    assert max_active > 1


def test_animeko_resolve_skips_non_finite_timestamps() -> None:
    provider = AnimekoDanmakuProvider(
        get=lambda *args, **kwargs: httpx.Response(
            200,
            json={
                "danmakuList": [
                    {
                        "danmakuInfo": {
                            "playTime": "nan",
                            "color": 16777215,
                            "text": "NaN",
                        }
                    },
                    {
                        "danmakuInfo": {
                            "playTime": "inf",
                            "color": 16777215,
                            "text": "Infinity",
                        }
                    },
                    {
                        "danmakuInfo": {
                            "playTime": 1000,
                            "color": 16777215,
                            "text": "valid",
                        }
                    },
                ]
            },
        ),
        nodes=("https://node.example",),
    )

    assert [record.content for record in provider.resolve("animeko://episode/1")] == [
        "valid"
    ]


def test_animeko_supports_only_valid_internal_episode_urls() -> None:
    provider = AnimekoDanmakuProvider()

    assert provider.supports("animeko://episode/4201") is True
    assert provider.supports("animeko://episode/") is False
    assert provider.supports("bahamut://episode/4201") is False


def test_animeko_all_nodes_fail_with_named_error() -> None:
    provider = AnimekoDanmakuProvider(
        get=lambda *args, **kwargs: (_ for _ in ()).throw(httpx.HTTPError("down")),
        nodes=("https://node-a.example", "https://node-b.example"),
    )

    with pytest.raises(DanmakuResolveError, match="Animeko弹幕获取失败"):
        provider.resolve("animeko://episode/4201")


def test_animeko_default_search_nodes_include_vip_mirror() -> None:
    # danmu_api f56b34f 迁移后的 bangumi API 镜像作为兜底节点
    provider = AnimekoDanmakuProvider()
    assert provider._search_nodes == (
        "https://api.bangumi.lol",
        "https://api.bgm.tv",
        "https://api.bangumi.vip",
    )
