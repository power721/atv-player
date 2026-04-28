import pytest

from atv_player.danmaku.errors import DanmakuResolveError, ProviderNotSupportedError
from atv_player.danmaku.models import DanmakuRecord, DanmakuSearchItem
from atv_player.danmaku.service import DanmakuService, create_default_danmaku_service


class FakeProvider:
    def __init__(self, key: str, items: list[DanmakuSearchItem], records: list[DanmakuRecord]) -> None:
        self.key = key
        self.items = items
        self.records = records
        self.search_calls: list[str] = []
        self.original_name_calls: list[str | None] = []
        self.resolve_calls: list[str] = []

    def search(self, name: str, original_name: str | None = None) -> list[DanmakuSearchItem]:
        self.search_calls.append(name)
        self.original_name_calls.append(original_name)
        return list(self.items)

    def resolve(self, page_url: str) -> list[DanmakuRecord]:
        self.resolve_calls.append(page_url)
        return list(self.records)

    def supports(self, page_url: str) -> bool:
        return self.key in page_url


class FailingSearchProvider(FakeProvider):
    def search(self, name: str, original_name: str | None = None) -> list[DanmakuSearchItem]:
        self.search_calls.append(name)
        self.original_name_calls.append(original_name)
        raise RuntimeError(f"{self.key} search boom")


def test_search_danmu_prefers_provider_from_reg_src() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第1集", url="https://tencent/item", ratio=0.9, simi=0.8)],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第1集", url="https://youku/item", ratio=0.8, simi=0.8)],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("剑来 第1集", "https://v.qq.com/x/cover/demo.html")

    assert [item.provider for item in results] == ["tencent"]
    assert tencent.search_calls == ["剑来"]
    assert youku.search_calls == []


def test_search_danmu_sources_groups_results_by_provider_and_marks_default() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第12集", url="https://v.qq.com/12", ratio=0.9, simi=0.9)],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第12集", url="https://v.youku.com/12", ratio=0.8, simi=0.8)],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    result = service.search_danmu_sources("剑来 第12集")

    assert [group.provider for group in result.groups] == ["tencent", "youku"]
    assert result.default_provider == "tencent"
    assert result.default_option_url == "https://v.qq.com/12"


def test_search_danmu_sources_prefers_exact_historical_page_url() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(provider="tencent", name="剑来 第12集", url="https://v.qq.com/11", ratio=0.95, simi=0.95),
            DanmakuSearchItem(provider="tencent", name="剑来 第12集", url="https://v.qq.com/12", ratio=0.80, simi=0.80),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    result = service.search_danmu_sources(
        "剑来 第12集",
        preferred_provider="tencent",
        preferred_page_url="https://v.qq.com/12",
    )

    assert result.default_option_url == "https://v.qq.com/12"
    assert result.groups[0].options[1].preferred_by_history is True


def test_service_resolve_danmu_uses_mgtv_provider_for_mgtv_urls() -> None:
    mgtv = FakeProvider(
        "mgtv",
        [DanmakuSearchItem(provider="mgtv", name="歌手2026 第1期", url="https://www.mgtv.com/b/555/1001.html")],
        [DanmakuRecord(time_offset=1.5, pos=1, color="16777215", content="芒果弹幕")],
    )
    service = DanmakuService({"mgtv": mgtv}, provider_order=["mgtv"])

    xml_text = service.resolve_danmu("https://www.mgtv.com/b/555/1001.html")

    assert '<d p="1.5,1,25,16777215">芒果弹幕</d>' in xml_text
    assert mgtv.resolve_calls == ["https://www.mgtv.com/b/555/1001.html"]


def test_search_danmu_falls_through_to_other_providers_when_reg_src_provider_misses_requested_episode() -> None:
    bilibili = FakeProvider(
        "bilibili",
        [DanmakuSearchItem(provider="bilibili", name="蜜语纪 24集", url="https://www.bilibili.com/video/BV24")],
        [],
    )
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="蜜语纪 15集", url="https://v.qq.com/x/cover/demo15.html")],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="蜜语纪 特别篇", url="https://v.youku.com/v_show/id_demo.html")],
        [],
    )
    service = DanmakuService(
        {"bilibili": bilibili, "tencent": tencent, "youku": youku},
        provider_order=["tencent", "youku", "bilibili"],
    )

    results = service.search_danmu("蜜语纪 15集", "https://www.bilibili.com/video/BV1xx411c7mD")

    assert [item.url for item in results] == [
        "https://v.qq.com/x/cover/demo15.html",
        "https://v.youku.com/v_show/id_demo.html",
    ]
    assert bilibili.search_calls == ["蜜语纪"]
    assert tencent.search_calls == ["蜜语纪"]
    assert youku.search_calls == ["蜜语纪"]


def test_search_danmu_aggregates_and_sorts_results_without_reg_src() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第1集", url="https://tencent/item", ratio=0.82, simi=0.82)],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第1集", url="https://youku/item", ratio=0.91, simi=0.91)],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("剑来 第1集")

    assert [item.provider for item in results] == ["youku", "tencent"]


def test_search_danmu_falls_back_to_default_order_for_unknown_reg_src() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第1集", url="https://tencent/item", ratio=0.82, simi=0.82)],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第1集", url="https://youku/item", ratio=0.91, simi=0.91)],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("剑来 第1集", "https://unknown.example/video/1")

    assert [item.provider for item in results] == ["youku", "tencent"]


def test_search_danmu_ignores_single_provider_search_failures() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第二季 10集", url="https://tencent/item", ratio=0.82, simi=0.82)],
        [],
    )
    youku = FailingSearchProvider("youku", [], [])
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("剑来 第二季 10集", "/play/10")

    assert [(item.provider, item.url) for item in results] == [("tencent", "https://tencent/item")]
    assert tencent.search_calls == ["剑来 第二季"]
    assert youku.search_calls == ["剑来 第二季"]


def test_search_danmu_strips_episode_suffix_before_calling_providers() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第二季 第10集", url="https://tencent/item")],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第二季 第10集", url="https://youku/item")],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    service.search_danmu("剑来 第二季 10集")

    assert tencent.search_calls == ["剑来 第二季"]
    assert youku.search_calls == ["剑来 第二季"]


def test_search_danmu_passes_original_name_to_providers() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="白日提灯 20集", url="https://tencent/20")],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    service.search_danmu("白日提灯 20集", "https://v.qq.com/x/cover/demo.html")

    assert tencent.search_calls == ["白日提灯"]
    assert tencent.original_name_calls == ["白日提灯 20集"]


def test_search_danmu_filters_to_candidates_with_matching_episode_number() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(provider="tencent", name="剑来 第二季 第9集", url="https://tencent/9"),
            DanmakuSearchItem(provider="tencent", name="剑来 第二季 第10集", url="https://tencent/10"),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    results = service.search_danmu("剑来 第二季 10集")

    assert [item.url for item in results] == ["https://tencent/10"]


def test_search_danmu_matches_youku_titles_with_trailing_numeric_episode_suffix() -> None:
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="黑夜告白 01", url="https://v.youku.com/v_show/id_demo01.html")],
        [],
    )
    service = DanmakuService({"youku": youku}, provider_order=["youku"])

    results = service.search_danmu("黑夜告白 第1集")

    assert [item.url for item in results] == ["https://v.youku.com/v_show/id_demo01.html"]


def test_search_danmu_rejects_matching_episode_when_title_only_matches_by_containment() -> None:
    iqiyi = FakeProvider(
        "iqiyi",
        [
            DanmakuSearchItem(
                provider="iqiyi",
                name="隆行天下之重走八千里路云和月 第16集",
                url="https://www.iqiyi.com/v_wrong16.html",
                ratio=0.66,
                simi=0.66,
            )
        ],
        [],
    )
    service = DanmakuService({"iqiyi": iqiyi}, provider_order=["iqiyi"])

    results = service.search_danmu("八千里路云和月 16集")

    assert results == []


def test_search_danmu_keeps_fallback_ranking_when_no_candidate_matches_requested_episode() -> None:
    tencent = FakeProvider(
        "tencent",
        [DanmakuSearchItem(provider="tencent", name="剑来 第二季 第9集", url="https://tencent/9", ratio=0.86, simi=0.86)],
        [],
    )
    youku = FakeProvider(
        "youku",
        [DanmakuSearchItem(provider="youku", name="剑来 第二季 特别篇", url="https://youku/special", ratio=0.92, simi=0.92)],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("剑来 第二季 10集")

    assert results == []


def test_search_danmu_rejects_no_episode_candidates_for_episode_requests() -> None:
    bilibili = FakeProvider(
        "bilibili",
        [
            DanmakuSearchItem(
                provider="bilibili",
                name="《八千里路云和月》史东山编导经典",
                url="https://www.bilibili.com/bangumi/play/ep338215",
                ratio=0.66,
                simi=0.66,
            )
        ],
        [],
    )
    iqiyi = FakeProvider(
        "iqiyi",
        [
            DanmakuSearchItem(
                provider="iqiyi",
                name="八千里路云和月",
                url="http://www.iqiyi.com/v_19rrn6svcw.html",
                ratio=1.0,
                simi=1.0,
            )
        ],
        [],
    )
    service = DanmakuService(
        {"bilibili": bilibili, "iqiyi": iqiyi},
        provider_order=["bilibili", "iqiyi"],
    )

    results = service.search_danmu("八千里路云和月 1集")

    assert results == []


def test_search_danmu_keeps_movie_candidates_for_implicit_trailing_index_requests() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="熊猫计划之部落奇遇记",
                url="https://v.qq.com/x/cover/movie.html",
                ratio=0.98,
                simi=0.98,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="熊猫计划",
                url="https://v.qq.com/x/cover/other.html",
                ratio=0.80,
                simi=0.80,
            ),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    results = service.search_danmu("熊猫计划之部落奇遇记 1")

    assert [item.url for item in results] == ["https://v.qq.com/x/cover/movie.html", "https://v.qq.com/x/cover/other.html"]


def test_search_danmu_keeps_movie_candidates_for_implicit_trailing_year_requests() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2（原声版）",
                url="https://v.qq.com/x/cover/original.html",
                ratio=0.99,
                simi=0.99,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2 1集",
                url="https://v.qq.com/x/cover/ep1.html",
                ratio=0.90,
                simi=0.90,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2",
                url="https://v.qq.com/x/cover/movie.html",
                ratio=0.98,
                simi=0.98,
            ),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    results = service.search_danmu("疯狂动物城2 2025")

    assert [item.url for item in results] == [
        "https://v.qq.com/x/cover/movie.html",
        "https://v.qq.com/x/cover/original.html",
    ]


def test_search_danmu_prefers_no_episode_candidates_for_implicit_trailing_index_requests() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2 1集",
                url="https://v.qq.com/x/cover/ep1.html",
                ratio=1.0,
                simi=1.0,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2（原声版）",
                url="https://v.qq.com/x/cover/original.html",
                ratio=0.8,
                simi=0.8,
            ),
        ],
        [],
    )
    youku = FakeProvider(
        "youku",
        [
            DanmakuSearchItem(
                provider="youku",
                name="疯狂动物城2",
                url="https://v.youku.com/v_show/id_movie.html",
                ratio=1.0,
                simi=1.0,
            )
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("疯狂动物城2 1")

    assert [item.url for item in results] == [
        "https://v.youku.com/v_show/id_movie.html",
        "https://v.qq.com/x/cover/original.html",
        "https://v.qq.com/x/cover/ep1.html",
    ]


def test_search_danmu_prefers_movie_variants_over_promotional_items_for_implicit_trailing_index_requests() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2 1集",
                url="https://v.qq.com/x/cover/ep1.html",
                ratio=1.0,
                simi=1.0,
                duration_seconds=1826,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2（原声版）",
                url="https://v.qq.com/x/cover/original.html",
                ratio=0.7,
                simi=0.7,
                duration_seconds=5940,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2·独家采访",
                url="https://v.qq.com/x/cover/interview.html",
                ratio=0.95,
                simi=0.95,
                duration_seconds=480,
            ),
        ],
        [],
    )
    youku = FakeProvider(
        "youku",
        [
            DanmakuSearchItem(
                provider="youku",
                name="疯狂动物城2",
                url="https://v.youku.com/v_show/id_movie.html",
                ratio=1.0,
                simi=1.0,
                duration_seconds=5935,
            ),
            DanmakuSearchItem(
                provider="youku",
                name="疯狂动物城2（普通话版）",
                url="https://v.youku.com/v_show/id_mandarin.html",
                ratio=0.68,
                simi=0.68,
                duration_seconds=5935,
            ),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent, "youku": youku}, provider_order=["tencent", "youku"])

    results = service.search_danmu("疯狂动物城2 1")

    assert [item.url for item in results] == [
        "https://v.youku.com/v_show/id_movie.html",
        "https://v.qq.com/x/cover/original.html",
        "https://v.youku.com/v_show/id_mandarin.html",
    ]


def test_search_danmu_filters_out_original_title_when_query_targets_sequel() -> None:
    iqiyi = FakeProvider(
        "iqiyi",
        [
            DanmakuSearchItem(
                provider="iqiyi",
                name="疯狂动物城2",
                url="https://www.iqiyi.com/v_1vf32rlbi7w.html",
                ratio=1.0,
                simi=1.0,
                duration_seconds=5935,
            ),
            DanmakuSearchItem(
                provider="iqiyi",
                name="疯狂动物城",
                url="https://www.iqiyi.com/v_19rrlpltbg.html",
                ratio=0.91,
                simi=0.91,
                duration_seconds=6480,
            ),
        ],
        [],
    )
    service = DanmakuService({"iqiyi": iqiyi}, provider_order=["iqiyi"])

    results = service.search_danmu("疯狂动物城2")

    assert [item.url for item in results] == ["https://www.iqiyi.com/v_1vf32rlbi7w.html"]


def test_search_danmu_does_not_fall_back_to_stripped_keyword_when_episode_search_misses() -> None:
    class RetryAwareProvider(FakeProvider):
        def search(self, name: str, original_name: str | None = None) -> list[DanmakuSearchItem]:
            self.search_calls.append(name)
            self.original_name_calls.append(original_name)
            if name == "蜜语纪 15集":
                return [
                    DanmakuSearchItem(provider="tencent", name="蜜语纪 10集", url="https://tencent/10"),
                    DanmakuSearchItem(provider="tencent", name="蜜语纪 11集", url="https://tencent/11"),
                ]
            return []

    tencent = RetryAwareProvider("tencent", [], [])
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    results = service.search_danmu("蜜语纪 15集")

    assert results == []
    assert tencent.search_calls == ["蜜语纪"]


def test_resolve_danmu_dispatches_by_url_and_builds_xml() -> None:
    tencent = FakeProvider(
        "tencent",
        [],
        [DanmakuRecord(time_offset=1.0, pos=1, color="16777215", content="hello")],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    xml = service.resolve_danmu("https://video.tencent/item")

    assert '<d p="1.0,1,25,16777215">hello</d>' in xml
    assert tencent.resolve_calls == ["https://video.tencent/item"]


def test_resolve_danmu_raises_for_unknown_provider_url() -> None:
    service = DanmakuService({}, provider_order=[])

    try:
        service.resolve_danmu("https://unknown.example/video/1")
    except ProviderNotSupportedError:
        pass
    else:
        raise AssertionError("Expected ProviderNotSupportedError")


def test_default_service_has_fixed_provider_order() -> None:
    service = create_default_danmaku_service()

    assert service.provider_order == ["tencent", "youku", "bilibili", "iqiyi", "mgtv"]


def test_danmaku_search_item_accepts_bilibili_metadata() -> None:
    item = DanmakuSearchItem(
        provider="bilibili",
        name="凡人修仙传 第1集",
        url="https://www.bilibili.com/bangumi/play/ep123",
        cid=987654,
        bvid="BV1xx411c7mD",
        aid=123456,
        ep_id=123,
        season_id=456,
        search_type="media_bangumi",
    )

    assert item.cid == 987654
    assert item.bvid == "BV1xx411c7mD"
    assert item.ep_id == 123
    assert item.search_type == "media_bangumi"


def test_match_provider_maps_bilibili_domains() -> None:
    from atv_player.danmaku.utils import match_provider

    assert match_provider("https://www.bilibili.com/video/BV1xx411c7mD") == "bilibili"
    assert match_provider("https://www.bilibili.com/bangumi/play/ep123") == "bilibili"
    assert match_provider("https://b23.tv/demo") == "bilibili"


def test_default_service_includes_bilibili_provider_in_fixed_order() -> None:
    service = create_default_danmaku_service()

    assert service.provider_order == ["tencent", "youku", "bilibili", "iqiyi", "mgtv"]


def test_default_service_includes_iqiyi_provider_in_fixed_order() -> None:
    service = create_default_danmaku_service()

    assert "iqiyi" in service.provider_order


def test_default_service_raises_clear_error_for_invalid_mgtv_resolution_url() -> None:
    service = create_default_danmaku_service()

    with pytest.raises(DanmakuResolveError, match="MGTV.*invalid play url"):
        service.resolve_danmu("https://www.mgtv.com/b/demo/1.html")


def test_default_service_still_rejects_unknown_urls() -> None:
    service = create_default_danmaku_service()

    with pytest.raises(ProviderNotSupportedError):
        service.resolve_danmu("https://unknown.example/video/1")
