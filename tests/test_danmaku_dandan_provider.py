import threading
import time

import httpx
import pytest

from atv_player.danmaku.errors import DanmakuResolveError, DanmakuSearchError
from atv_player.danmaku.providers.dandan import DandanDanmakuProvider, probe_dandan_server

BASE = "http://dd-test:9321"


def _provider(get) -> DandanDanmakuProvider:
    return DandanDanmakuProvider(get=get, base_url_loader=lambda: BASE)


def test_dandan_search_expands_and_filters_requested_episode() -> None:
    calls: list[tuple[str, object]] = []

    def fake_get(url: str, **kwargs):
        if url.endswith("/api/v2/search/anime"):
            calls.append((url, kwargs.get("params")))
            return httpx.Response(
                200,
                json={"animes": [{"animeId": 100, "animeTitle": "葬送的芙莉莲"}]},
            )
        if url.endswith("/api/v2/bangumi/100"):
            return httpx.Response(
                200,
                json={
                    "bangumi": {
                        "episodes": [
                            {"episodeId": 10001, "episodeNumber": "1", "episodeTitle": "冒险的终点"},
                            {"episodeId": 10002, "episodeNumber": "2", "episodeTitle": "无需魔法"},
                        ]
                    }
                },
            )
        raise AssertionError(url)

    items = _provider(fake_get).search("葬送的芙莉莲", original_name="葬送的芙莉莲 第2集")

    assert [(item.provider, item.name, item.url) for item in items] == [
        ("dandan", "葬送的芙莉莲 第2集 无需魔法", "dandan://episode/10002")
    ]
    # standard path-segment routing against the configured base, keyword as a query param
    assert calls == [("http://dd-test:9321/api/v2/search/anime", {"keyword": "葬送的芙莉莲"})]


def test_dandan_resolve_maps_comments_and_skips_invalid_rows() -> None:
    def fake_get(url: str, **kwargs):
        assert url == "http://dd-test:9321/api/v2/comment/10002"
        return httpx.Response(
            200,
            json={
                "comments": [
                    {"cid": 1, "p": "1.25,1,16711680,[dandan]", "m": "滚动"},
                    {"cid": 2, "p": "2.5,5,65280,[dandan]", "m": "顶部"},
                    {"cid": 3, "p": "bad,1,255,[dandan]", "m": "坏时间"},
                    {"cid": 4, "p": "4,4,255,[dandan]", "m": ""},
                ]
            },
        )

    records = _provider(fake_get).resolve("dandan://episode/10002")

    assert [(r.time_offset, r.pos, r.color, r.content) for r in records] == [
        (1.25, 1, "16711680", "滚动"),
        (2.5, 5, "65280", "顶部"),
    ]


def test_dandan_supports_only_valid_internal_episode_urls() -> None:
    provider = _provider(lambda *a, **k: httpx.Response(200, json={}))

    assert provider.supports("dandan://episode/10002") is True
    assert provider.supports("dandan://episode/") is False
    assert provider.supports("animeko://episode/10002") is False


def test_dandan_unconfigured_source_is_off() -> None:
    provider = DandanDanmakuProvider(get=lambda *a, **k: pytest.fail("no request expected"))

    assert provider.supports("dandan://episode/10002") is False
    assert provider.search("葬送的芙莉莲") == []
    with pytest.raises(DanmakuResolveError, match="未配置"):
        provider.resolve("dandan://episode/10002")


def test_dandan_search_failure_raises_search_error() -> None:
    provider = DandanDanmakuProvider(
        get=lambda *a, **k: (_ for _ in ()).throw(httpx.HTTPError("down")),
        base_url_loader=lambda: BASE,
    )

    with pytest.raises(DanmakuSearchError, match="弹弹Play服务器连接失败"):
        provider.search("葬送的芙莉莲")


def test_dandan_search_keeps_results_when_another_series_detail_fails() -> None:
    def fake_get(url: str, **kwargs):
        if url.endswith("/api/v2/search/anime"):
            return httpx.Response(
                200,
                json={
                    "animes": [
                        {"animeId": 100, "animeTitle": "迷宫饭"},
                        {"animeId": 200, "animeTitle": "迷宫饭"},
                    ]
                },
            )
        if url.endswith("/api/v2/bangumi/100"):
            return httpx.Response(
                200,
                json={"bangumi": {"episodes": [{"episodeId": 10001, "episodeNumber": "1"}]}},
            )
        raise httpx.HTTPError("second detail failed")

    items = _provider(fake_get).search("迷宫饭")

    assert [item.url for item in items] == ["dandan://episode/10001"]


def test_dandan_search_expands_series_details_concurrently() -> None:
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_get(url: str, **kwargs):
        nonlocal active, max_active
        if url.endswith("/api/v2/search/anime"):
            return httpx.Response(
                200,
                json={
                    "animes": [
                        {"animeId": anime_id, "animeTitle": "迷宫饭"}
                        for anime_id in range(1, 5)
                    ]
                },
            )
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        anime_id = url.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={"bangumi": {"episodes": [{"episodeId": f"{anime_id}01", "episodeNumber": "1"}]}},
        )

    _provider(fake_get).search("迷宫饭")

    assert max_active > 1


def test_dandan_resolve_skips_non_finite_timestamps() -> None:
    provider = DandanDanmakuProvider(
        get=lambda *a, **k: httpx.Response(
            200,
            json={
                "comments": [
                    {"p": "nan,1,16777215", "m": "NaN"},
                    {"p": "inf,1,16777215", "m": "Infinity"},
                    {"p": "1,1,16777215", "m": "valid"},
                ]
            },
        ),
        base_url_loader=lambda: BASE,
    )

    assert [record.content for record in provider.resolve("dandan://episode/1")] == ["valid"]


def test_dandan_resolve_failure_names_the_source() -> None:
    provider = DandanDanmakuProvider(
        get=lambda *a, **k: (_ for _ in ()).throw(httpx.HTTPError("down")),
        base_url_loader=lambda: BASE,
    )

    with pytest.raises(DanmakuResolveError, match="弹弹Play服务器连接失败"):
        provider.resolve("dandan://episode/10002")


# --- probe_dandan_server (used by the settings "测试连接" button) ---


def test_probe_dandan_server_success() -> None:
    ok, message = probe_dandan_server(
        get=lambda *a, **k: httpx.Response(200, json={"animes": []}),
        base_url=BASE,
    )
    assert ok is True
    assert "连接正常" in message


def test_probe_dandan_server_empty_base() -> None:
    ok, _ = probe_dandan_server(get=lambda *a, **k: pytest.fail("no request"), base_url="")
    assert ok is False


def test_probe_dandan_server_http_error() -> None:
    ok, message = probe_dandan_server(
        get=lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")),
        base_url=BASE,
    )
    assert ok is False
    assert "连接失败" in message


def test_probe_dandan_server_bad_status() -> None:
    ok, message = probe_dandan_server(
        get=lambda *a, **k: httpx.Response(401, text="unauthorized"),
        base_url=BASE,
    )
    assert ok is False
    assert "401" in message


def test_probe_dandan_server_bad_shape() -> None:
    ok, _ = probe_dandan_server(
        get=lambda *a, **k: httpx.Response(200, json={"unexpected": True}),
        base_url=BASE,
    )
    assert ok is False


def test_dandan_search_keeps_foreign_keyword_hits_without_title_filter() -> None:
    # 外文检索词与中文标题零字符交集:无标题直击时全量放行,交由请求侧按集数
    # 兜底(对齐 danmu_api #493)
    def fake_get(url: str, **kwargs):
        if url.endswith("/api/v2/search/anime"):
            return httpx.Response(
                200,
                json={
                    "animes": [
                        {"animeId": 200, "animeTitle": "再见，拉拉"},
                        {"animeId": 201, "animeTitle": "MyGO!!!!! 剧场版"},
                    ]
                },
            )
        if url.endswith("/api/v2/bangumi/200"):
            return httpx.Response(
                200,
                json={"bangumi": {"episodes": [
                    {"episodeId": 20001, "episodeNumber": "1", "episodeTitle": "初见"},
                ]}},
            )
        if url.endswith("/api/v2/bangumi/201"):
            return httpx.Response(
                200,
                json={"bangumi": {"episodes": [
                    {"episodeId": 20101, "episodeNumber": "1", "episodeTitle": "前夜"},
                ]}},
            )
        raise AssertionError(url)

    items = _provider(fake_get).search("Sayonara Lara")

    assert {item.url for item in items} == {
        "dandan://episode/20001",
        "dandan://episode/20101",
    }


def test_dandan_search_foreign_keyword_prefers_direct_title_hits() -> None:
    # 标题直击存在时只保留直击条目,别名子串混入的无关系列不再展开
    def fake_get(url: str, **kwargs):
        if url.endswith("/api/v2/search/anime"):
            return httpx.Response(
                200,
                json={
                    "animes": [
                        {"animeId": 300, "animeTitle": "BanG Dream! It's MyGO!!!!!"},
                        {"animeId": 301, "animeTitle": "我女神系列 藤宫香织"},
                    ]
                },
            )
        if url.endswith("/api/v2/bangumi/300"):
            return httpx.Response(
                200,
                json={"bangumi": {"episodes": [
                    {"episodeId": 30001, "episodeNumber": "1"},
                ]}},
            )
        raise AssertionError(url)

    items = _provider(fake_get).search("mygo")

    assert [item.url for item in items] == ["dandan://episode/30001"]
