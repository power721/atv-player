"""豆瓣/Bangumi 评论接入:客户端端点、loader 协议适配与评论源解析。"""

from __future__ import annotations

import httpx
import pytest

from atv_player.comments.bangumi import BangumiCommentsClient, build_bangumi_comments_loader
from atv_player.comments.douban import (
    DoubanCommentsClient,
    build_douban_comments_loader,
    douban_kind_from_type_name,
)
from atv_player.comments.provider import MetadataCommentsProvider
from atv_player.danmaku.discovery.douban import DoubanSubject
from atv_player.models import PlaybackDetailField, VodItem


def _douban_client(handler) -> DoubanCommentsClient:
    return DoubanCommentsClient(transport=httpx.MockTransport(handler))


def _bangumi_client(handler) -> BangumiCommentsClient:
    return BangumiCommentsClient(transport=httpx.MockTransport(handler))


# --- 豆瓣 rexxar interests --------------------------------------------------


def test_douban_list_interests_movie_url_headers_and_mapping() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "m.douban.com"
        assert request.headers["Referer"].startswith("https://m.douban.com/")
        return httpx.Response(
            200,
            json={
                "start": 0,
                "count": 20,
                "total": 42,
                "interests": [
                    {
                        "id": "32514679",
                        "comment": "恐惧让你沦为囚犯",
                        "create_time": "2008-02-27 21:43:23",
                        "vote_count": 26541,
                        "rating": {"value": 5, "star_count": 5},
                        "user": {
                            "name": "如小果",
                            "avatar": "https://img3.doubanio.com/icon/u1.jpg",
                            "loc": {"name": "北京"},
                        },
                    }
                ],
            },
        )

    payload = _douban_client(handler).list_interests("1292052", "movie")
    assert seen[0].url.path == "/rexxar/api/v2/movie/1292052/interests"
    assert "start=0" in str(seen[0].url.query)
    assert payload["total"] == 42


def test_douban_movie_404_falls_back_to_tv_path() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "/movie/" in request.url.path:
            return httpx.Response(404, json={"code": 404, "msg": "traversal_error"})
        return httpx.Response(200, json={"total": 7, "interests": []})

    _douban_client(handler).list_interests("33404425", "movie")
    assert paths == [
        "/rexxar/api/v2/movie/33404425/interests",
        "/rexxar/api/v2/tv/33404425/interests",
    ]


def test_douban_loader_maps_protocol_fields_and_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        start = int(dict(httpx.QueryParams(request.url.params)).get("start", "0"))
        if start == 0:
            interests = [{"id": str(i), "comment": f"c{i}", "user": {"name": f"u{i}"}} for i in range(20)]
            return httpx.Response(200, json={"total": 21, "interests": interests})
        return httpx.Response(200, json={"total": 21, "interests": [{"id": "99", "comment": "last", "user": {"name": "u99"}}]})

    loader = build_douban_comments_loader(_douban_client(handler), "1292052", "movie")
    first = loader({"kind": "main", "next": ""})
    assert first["count"] == 21
    assert first["is_end"] is False
    assert first["next_offset"] == "20"
    assert first["comments"][0] == {
        "rpid": "0",
        "uname": "u0",
        "avatar": "",
        "message": "c0",
        "like": 0,
        "time_desc": "",
        "location": "",
        "badge": "",
    }

    second = loader({"kind": "main", "next": first["next_offset"]})
    assert second["is_end"] is True
    assert second["comments"][0]["rpid"] == "99"

    # 非数字游标按 0 处理
    assert loader({"kind": "main", "next": "cursor-x"})["next_offset"] == "20"


def test_douban_loader_badge_from_star_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total": 1,
                "interests": [{"id": "1", "comment": "x", "rating": {"star_count": 4}, "user": {"name": "u"}}],
            },
        )

    payload = build_douban_comments_loader(_douban_client(handler), "1", "movie")({"next": ""})
    assert payload["comments"][0]["badge"] == "★★★★"


def test_douban_kind_from_type_name() -> None:
    assert douban_kind_from_type_name("电视剧") == "tv"
    assert douban_kind_from_type_name("电影") == "movie"
    assert douban_kind_from_type_name("tv") == "tv"
    assert douban_kind_from_type_name("") == "movie"


# --- Bangumi next.bgm.tv p1 --------------------------------------------------


def test_bangumi_list_comments_url_user_agent_and_mapping() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "total": 2905,
                "data": [
                    {
                        "id": 54904687,
                        "rate": 9,
                        "comment": "荣耀尽归鲁路修",
                        "updatedAt": 1793765617,
                        "user": {
                            "nickname": "伊崎零",
                            "username": "1291801",
                            "avatar": {"medium": "https://lain.bgm.tv/r/200/pic/user/l/1.jpg"},
                        },
                    }
                ],
            },
        )

    payload = _bangumi_client(handler).list_subject_comments("8", limit=20, offset=0)
    assert seen[0].url == "https://next.bgm.tv/p1/subjects/8/comments?limit=20&offset=0"
    assert "ATVPlayer" in seen[0].headers["User-Agent"]
    assert payload["total"] == 2905


def test_bangumi_loader_pagination_and_badge() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(dict(httpx.QueryParams(request.url.params)).get("offset", "0"))
        if offset == 0:
            data = [{"id": i, "comment": f"c{i}", "user": {"nickname": f"u{i}"}} for i in range(20)]
            return httpx.Response(200, json={"total": 25, "data": data})
        return httpx.Response(
            200,
            json={
                "total": 25,
                "data": [
                    {"id": 99, "rate": 10, "comment": "last", "user": {"nickname": "u99"}},
                    *[{"id": 90 + i, "comment": f"tail{i}", "user": {"nickname": f"t{i}"}} for i in range(4)],
                ],
            },
        )

    loader = build_bangumi_comments_loader(_bangumi_client(handler), "8")
    first = loader({"kind": "main", "next": ""})
    assert first["count"] == 25
    assert first["is_end"] is False
    assert first["next_offset"] == "20"
    assert first["comments"][0]["rpid"] == "0"

    second = loader({"kind": "main", "next": "20"})
    assert second["is_end"] is True
    assert second["comments"][0]["badge"] == "10分"
    assert second["comments"][0]["ctime"] == 0 or isinstance(second["comments"][0]["ctime"], int)


def test_bangumi_search_subjects_posts_type_filter() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "api.bgm.tv"
        return httpx.Response(200, json={"data": [{"id": 8, "name_cn": "星际牛仔", "date": "1998-10-24"}]})

    entries = _bangumi_client(handler).search_subjects("星际牛仔")
    assert seen[0].url.path == "/v0/search/subjects"
    assert entries[0]["id"] == 8


def test_bangumi_client_rejects_empty_subject_id() -> None:
    with pytest.raises(ValueError):
        _bangumi_client(lambda request: httpx.Response(200, json={})).list_subject_comments("")


# --- 评论源解析 ------------------------------------------------------------


def _vod(**kwargs) -> VodItem:
    return VodItem(vod_id="test", vod_name="星际牛仔", vod_year="1998", **kwargs)


class StubDouban:
    def __init__(self, subjects: list[DoubanSubject] | None = None, error: Exception | None = None) -> None:
        self.subjects = subjects or []
        self.error = error
        self.search_calls = 0

    def search_subjects(self, keyword: str) -> list[DoubanSubject]:
        self.search_calls += 1
        if self.error is not None:
            raise self.error
        return self.subjects


class StubBangumi:
    def __init__(self, entries: list[dict] | None = None, error: Exception | None = None) -> None:
        self.entries = entries or []
        self.error = error
        self.search_calls = 0

    def search_subjects(self, keyword: str) -> list[dict]:
        self.search_calls += 1
        if self.error is not None:
            raise self.error
        return self.entries


def test_provider_uses_external_ids_without_search() -> None:
    douban = StubDouban()
    bangumi = StubBangumi()
    vod = _vod(
        dbid=1292052,
        detail_fields=[PlaybackDetailField("Bangumi ID", "8")],
    )
    sources = MetadataCommentsProvider(douban, bangumi).resolve(vod)  # type: ignore[arg-type]
    assert [(source.key, source.label) for source in sources] == [("douban", "豆瓣短评"), ("bangumi", "Bangumi 吐槽")]
    assert douban.search_calls == 0
    assert bangumi.search_calls == 0


def test_provider_falls_back_to_title_search() -> None:
    douban = StubDouban(
        subjects=[
            DoubanSubject(douban_id="1", title="别的", year="2001", type_name="电影"),
            DoubanSubject(douban_id="2", title="星际牛仔", year="1998", type_name="电影"),
        ]
    )
    bangumi = StubBangumi(entries=[{"id": 8, "name_cn": "星际牛仔", "date": "1998-10-24"}])
    sources = MetadataCommentsProvider(douban, bangumi).resolve(_vod())  # type: ignore[arg-type]
    assert [source.key for source in sources] == ["douban", "bangumi"]
    assert douban.search_calls == 1
    assert bangumi.search_calls == 1


def test_provider_search_failure_yields_no_source_without_raising() -> None:
    douban = StubDouban(error=RuntimeError("rexxar down"))
    bangumi = StubBangumi(error=RuntimeError("bgm down"))
    sources = MetadataCommentsProvider(douban, bangumi).resolve(_vod())  # type: ignore[arg-type]
    assert sources == []


def test_provider_caches_resolution_per_title_year() -> None:
    douban = StubDouban(subjects=[DoubanSubject(douban_id="2", title="星际牛仔", year="1998", type_name="电视剧")])
    bangumi = StubBangumi(entries=[{"id": 8}])
    provider = MetadataCommentsProvider(douban, bangumi)  # type: ignore[arg-type]
    provider.resolve(_vod())
    provider.resolve(_vod())
    assert douban.search_calls == 1
    assert bangumi.search_calls == 1


def test_provider_requires_title() -> None:
    provider = MetadataCommentsProvider(StubDouban(), StubBangumi())  # type: ignore[arg-type]
    assert provider.resolve(VodItem(vod_id="x", vod_name="")) == []
    assert provider.resolve(None) == []


def test_provider_unmatched_douban_search_is_skipped() -> None:
    douban = StubDouban(subjects=[DoubanSubject(douban_id="9", title="完全无关", year="2020", type_name="电影")])
    bangumi = StubBangumi(entries=[{"id": 8}])
    sources = MetadataCommentsProvider(douban, bangumi).resolve(_vod())  # type: ignore[arg-type]
    assert [source.key for source in sources] == ["bangumi"]


def test_provider_bangumi_year_match_preferred() -> None:
    from atv_player.comments.provider import _pick_bangumi_subject

    entries = [
        {"id": 111, "name_cn": "星际牛仔(旧版)", "date": "1980-01-01"},
        {"id": 8, "name_cn": "星际牛仔", "date": "1998-10-24"},
    ]
    assert _pick_bangumi_subject(entries, "1998") == "8"
    # 全部年份不符时退回第一个非空 id,不因年份过滤漏光
    assert _pick_bangumi_subject(entries, "2030") == "111"
    assert _pick_bangumi_subject([], "1998") == ""
