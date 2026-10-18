import json
import threading
import time

import httpx
import pytest

from atv_player.ai.enrichment import DanmakuQueryRefinement
from atv_player.danmaku.errors import DanmakuResolveError, ProviderNotSupportedError
from atv_player.danmaku.models import (
    DanmakuRecord,
    DanmakuSearchItem,
    DanmakuSourceGroup,
    DanmakuSourceOption,
    DanmakuSourceSearchResult,
)
from atv_player.danmaku.providers.youku import YoukuDanmakuProvider
from atv_player.danmaku.service import DanmakuService, create_default_danmaku_service
from atv_player.models import AppConfig


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


class SlowSearchProvider(FakeProvider):
    def __init__(
        self,
        key: str,
        items: list[DanmakuSearchItem],
        records: list[DanmakuRecord],
        *,
        state: dict[str, int],
        lock: threading.Lock,
    ) -> None:
        super().__init__(key, items, records)
        self._state = state
        self._lock = lock

    def search(self, name: str, original_name: str | None = None) -> list[DanmakuSearchItem]:
        self.search_calls.append(name)
        self.original_name_calls.append(original_name)
        with self._lock:
            self._state["active"] += 1
            self._state["max_active"] = max(self._state["max_active"], self._state["active"])
        time.sleep(0.05)
        with self._lock:
            self._state["active"] -= 1
        return list(self.items)


class AIRefinesDanmakuQuery:
    def __init__(self) -> None:
        self.inputs = []

    def refine_danmaku_query(self, data):
        self.inputs.append(data)
        return DanmakuQueryRefinement(queries=["黑镜 第3集"])


class AIEmptyDanmakuQuery:
    def refine_danmaku_query(self, data):
        del data
        return DanmakuQueryRefinement()


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


def test_search_danmu_keeps_subtitle_named_episode_for_explicit_request() -> None:
    # Tencent names episodes by plot subtitle without the show name, e.g.
    # "第十三集 <剧情>". Such a real episode must still match an explicit
    # "<剧名> 13集" request instead of being dropped (regression: 《百花杀》第13集
    # 搜不到，返回了其它集数却没有13集).
    ep13 = DanmakuSearchItem(
        provider="tencent",
        name="第十三集 朱字当众揭穿我藏在凤椅下，皇后一句谁先取她的命",
        url="https://v.qq.com/x/cover/mzc003wpj912rrn/l3285j9s4lh.html",
        ratio=0.9,
        simi=0.8,
    )
    tencent = FakeProvider("tencent", [ep13], [])
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    results = service.search_danmu("百花杀 13集", "https://v.qq.com/x/cover/mzc003wpj912rrn/l3285j9s4lh.html")

    assert any(item.url == ep13.url for item in results)


def test_search_danmu_finds_youku_suspense_episode_four() -> None:
    detail_payload = {
        "moduleList": [
            {
                "components": [
                    {
                        "type": 10013,
                        "itemList": [
                            {
                                "action_value": "XNjUxODE2NjYyNA==",
                                "title": "第1集 矢量",
                                "stage": 1,
                                "videoType": "正片",
                            },
                            {
                                "action_value": "XNjUxODE2NjYyOA==",
                                "title": "第4集 专线",
                                "stage": 4,
                                "videoType": "正片",
                            },
                            {
                                "action_value": "preview04",
                                "title": "第4集 专线（预告）",
                                "stage": 4,
                                "videoType": "预告片",
                            },
                        ],
                    }
                ]
            }
        ]
    }

    def fake_get(url: str, **kwargs):
        if "search.youku.com" in url:
            return httpx.Response(
                200,
                json={
                    "pageComponentList": [
                        {
                            "commonData": {
                                "isYouku": 1,
                                "hasYouku": 1,
                                "titleDTO": {"displayName": "悬案"},
                                "videoLink": (
                                    "https://v.youku.com/v_show/"
                                    "id_XNjUxODE2NjYyNA==.html"
                                ),
                            }
                        }
                    ]
                },
            )
        detail_text = json.dumps(detail_payload, ensure_ascii=False)
        return httpx.Response(
            200,
            text=f"<script>window.__INITIAL_DATA__ ={detail_text};</script>",
        )

    provider = YoukuDanmakuProvider(get=fake_get)
    service = DanmakuService({"youku": provider}, provider_order=["youku"])

    results = service.search_danmu("悬案 4集", provider_filter="youku")

    assert [(item.name, item.url) for item in results] == [
        (
            "悬案 第4集 专线",
            "https://v.youku.com/v_show/id_XNjUxODE2NjYyOA==.html",
        ),
    ]


def test_danmaku_service_uses_ai_refined_query_before_original() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="黑镜 第3集",
                url="https://v.qq.com/x/3",
                ratio=1.0,
                simi=1.0,
                duration_seconds=3600,
            )
        ],
        [],
    )
    ai = AIRefinesDanmakuQuery()
    service = DanmakuService(
        {"tencent": tencent},
        provider_order=["tencent"],
        ai_enrichment_service=ai,
    )

    results = service.search_danmu("Black.Mirror.S01E03")

    assert ai.inputs[0].title == "Black.Mirror.S01E03"
    assert tencent.search_calls[0] == "黑镜 第3集"
    assert results[0].name == "黑镜 第3集"


def test_danmaku_service_falls_back_when_ai_query_empty() -> None:
    tencent = FakeProvider("tencent", [], [])
    service = DanmakuService(
        {"tencent": tencent},
        provider_order=["tencent"],
        ai_enrichment_service=AIEmptyDanmakuQuery(),
    )

    service.search_danmu("Black.Mirror.S01E03")

    assert tencent.search_calls[0] == "Black.Mirror.S01E03"


def test_create_default_danmaku_service_excludes_disabled_providers() -> None:
    service = create_default_danmaku_service(disabled_provider_ids=["youku", "mgtv"])

    assert service.provider_order == [
        "tencent",
        "bilibili",
        "iqiyi",
        "sohu",
        "migu",
        "renren",
        "dandan",
        "bahamut",
        "animeko",
    ]


def test_default_service_can_disable_new_anime_providers() -> None:
    service = create_default_danmaku_service(
        disabled_provider_ids=["dandan", "bahamut", "animeko"]
    )

    assert "dandan" not in service.provider_order
    assert "bahamut" not in service.provider_order
    assert "animeko" not in service.provider_order


def test_create_default_danmaku_service_can_disable_renren_provider() -> None:
    service = create_default_danmaku_service(disabled_provider_ids=["renren"])

    assert "renren" not in service.provider_order


def test_search_danmu_runs_all_providers_in_one_concurrent_round() -> None:
    state = {"active": 0, "max_active": 0}
    lock = threading.Lock()
    providers = {}
    provider_order = []
    for index, key in enumerate(["tencent", "youku", "bilibili", "iqiyi", "mgtv"], start=1):
        providers[key] = SlowSearchProvider(
            key,
            [DanmakuSearchItem(provider=key, name="剑来", url=f"https://{key}/item", ratio=0.8, simi=0.8)],
            [],
            state=state,
            lock=lock,
        )
        provider_order.append(key)
    service = DanmakuService(providers, provider_order=provider_order)

    results = service.search_danmu("剑来")

    assert len(results) == 5
    # 搜索面向不同源站点,单轮并发上限=源数:批 4 会把 12 源排成 3 批,
    # 空结果轮最坏 3×10s 超时,0 结果兜底链被拖到半分钟。
    assert state["max_active"] == len(providers)


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


def test_search_danmu_sources_variety_match_beats_historical_page_url_when_duration_known() -> None:
    iqiyi = FakeProvider(
        "iqiyi",
        [
            DanmakuSearchItem(
                provider="iqiyi",
                name="哈哈哈哈哈第6季 第1期下 邓超陈赫斗舞争抢马頔 李乃文滑冰传水笑到崩溃",
                url="https://www.iqiyi.com/v_ep1b.html",
                ratio=0.99,
                simi=0.99,
                duration_seconds=5480,
                resolve_context={"variety_year": 20260405},
            ),
            DanmakuSearchItem(
                provider="iqiyi",
                name="哈哈哈哈哈第6季 第2期上 五哈团为抢圈大打出手",
                url="https://www.iqiyi.com/v_ep2a.html",
                ratio=0.96,
                simi=0.96,
                duration_seconds=5511,
                resolve_context={"variety_year": 20260411},
            ),
        ],
        [],
    )
    service = DanmakuService({"iqiyi": iqiyi}, provider_order=["iqiyi"])

    result = service.search_danmu_sources(
        "哈哈哈哈哈第六季 20260411期 第2期上：五哈团为抢圈“大打出手” 4K60",
        preferred_provider="iqiyi",
        preferred_page_url="https://www.iqiyi.com/v_ep1b.html",
        media_duration_seconds=5511,
    )

    assert [option.url for option in result.groups[0].options] == [
        "https://www.iqiyi.com/v_ep2a.html",
        "https://www.iqiyi.com/v_ep1b.html",
    ]
    assert result.default_option_url == "https://www.iqiyi.com/v_ep2a.html"


def test_search_danmu_sources_reorders_candidates_by_media_duration() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="遮天 88集",
                url="https://v.qq.com/x/cover/ep88-long.html",
                ratio=0.98,
                simi=0.98,
                duration_seconds=1490,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="遮天 88集",
                url="https://v.qq.com/x/cover/ep88-best.html",
                ratio=0.96,
                simi=0.96,
                duration_seconds=1242,
            ),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    result = service.search_danmu_sources("遮天 88集", media_duration_seconds=1240)

    assert [option.url for option in result.groups[0].options] == [
        "https://v.qq.com/x/cover/ep88-best.html",
        "https://v.qq.com/x/cover/ep88-long.html",
    ]
    assert result.default_option_url == "https://v.qq.com/x/cover/ep88-best.html"


def test_search_danmu_sources_filters_out_candidates_with_duration_gap_over_five_minutes() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="遮天 88集",
                url="https://v.qq.com/x/cover/ep88-keep.html",
                ratio=0.98,
                simi=0.98,
                duration_seconds=1242,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="遮天 88集",
                url="https://v.qq.com/x/cover/ep88-drop.html",
                ratio=0.99,
                simi=0.99,
                duration_seconds=1560,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="遮天 88集",
                url="https://v.qq.com/x/cover/ep88-unknown.html",
                ratio=0.90,
                simi=0.90,
                duration_seconds=0,
            ),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    result = service.search_danmu_sources("遮天 88集", media_duration_seconds=1240)

    assert [option.url for option in result.groups[0].options] == [
        "https://v.qq.com/x/cover/ep88-keep.html",
        "https://v.qq.com/x/cover/ep88-unknown.html",
    ]
    assert result.default_option_url == "https://v.qq.com/x/cover/ep88-keep.html"


def test_search_danmu_sources_preserves_exact_episode_match_despite_large_duration_gap() -> None:
    iqiyi = FakeProvider(
        "iqiyi",
        [
            DanmakuSearchItem(
                provider="iqiyi",
                name="成何体统 第23集",
                url="https://www.iqiyi.com/v_ep23.html",
                ratio=0.99,
                simi=0.99,
                duration_seconds=2402,
            ),
            DanmakuSearchItem(
                provider="iqiyi",
                name="成何体统 第1集",
                url="https://www.iqiyi.com/v_ep1.html",
                ratio=0.92,
                simi=0.92,
                duration_seconds=2774,
            ),
        ],
        [],
    )
    service = DanmakuService({"iqiyi": iqiyi}, provider_order=["iqiyi"])

    result = service.search_danmu_sources("成何体统 1集", media_duration_seconds=2400)

    assert [option.url for option in result.groups[0].options] == [
        "https://www.iqiyi.com/v_ep1.html",
    ]
    assert result.default_option_url == "https://www.iqiyi.com/v_ep1.html"


def test_rerank_danmaku_source_search_result_filters_cached_candidates_with_duration_gap_over_five_minutes() -> None:
    service = DanmakuService({}, provider_order=[])

    result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[
                    DanmakuSourceOption(
                        provider="tencent",
                        name="遮天 88集",
                        url="https://v.qq.com/x/cover/ep88-drop.html",
                        duration_seconds=1560,
                    ),
                    DanmakuSourceOption(
                        provider="tencent",
                        name="遮天 88集",
                        url="https://v.qq.com/x/cover/ep88-keep.html",
                        duration_seconds=1242,
                    ),
                    DanmakuSourceOption(
                        provider="tencent",
                        name="遮天 88集",
                        url="https://v.qq.com/x/cover/ep88-unknown.html",
                        duration_seconds=0,
                    ),
                ],
            )
        ]
    )

    reranked = service.rerank_danmaku_source_search_result(result, media_duration_seconds=1240)

    assert [option.url for option in reranked.groups[0].options] == [
        "https://v.qq.com/x/cover/ep88-keep.html",
        "https://v.qq.com/x/cover/ep88-unknown.html",
    ]
    assert reranked.default_option_url == "https://v.qq.com/x/cover/ep88-keep.html"


def test_rerank_danmaku_source_search_result_preserves_exact_episode_match_despite_large_duration_gap() -> None:
    service = DanmakuService({}, provider_order=[])

    result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="iqiyi",
                provider_label="爱奇艺",
                options=[
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="成何体统 第23集",
                        url="https://www.iqiyi.com/v_ep23.html",
                        duration_seconds=2402,
                    ),
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="成何体统 第1集",
                        url="https://www.iqiyi.com/v_ep1.html",
                        duration_seconds=2774,
                    ),
                ],
            )
        ]
    )

    reranked = service.rerank_danmaku_source_search_result(
        result,
        query_name="成何体统 1集",
        media_duration_seconds=2400,
    )

    assert [option.url for option in reranked.groups[0].options] == [
        "https://www.iqiyi.com/v_ep1.html",
        "https://www.iqiyi.com/v_ep23.html",
    ]
    assert reranked.default_option_url == "https://www.iqiyi.com/v_ep1.html"


def test_rerank_danmaku_source_search_result_dedupes_cached_duplicate_options_by_provider_and_url() -> None:
    service = DanmakuService({}, provider_order=[])

    result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="iqiyi",
                provider_label="爱奇艺",
                options=[
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="灵武大陆 第104集 五脉之魂",
                        url="https://www.iqiyi.com/v_104.html",
                        duration_seconds=625,
                        resolve_context={"tv_id": 8142858142151200},
                    ),
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="灵武大陆 第104集 五脉之魂",
                        url="https://www.iqiyi.com/v_104.html",
                        duration_seconds=0,
                        resolve_context={"album_id": 4812694274119401, "category_id": 4},
                    ),
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="贺新春 灵武大陆+凌天独尊联动",
                        url="https://www.iqiyi.com/v_promo.html",
                        duration_seconds=180,
                    ),
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="贺新春 灵武大陆+凌天独尊联动",
                        url="https://www.iqiyi.com/v_promo.html",
                        duration_seconds=180,
                    ),
                ],
            )
        ]
    )

    reranked = service.rerank_danmaku_source_search_result(result)

    assert [option.url for option in reranked.groups[0].options] == [
        "https://www.iqiyi.com/v_104.html",
        "https://www.iqiyi.com/v_promo.html",
    ]
    assert reranked.groups[0].options[0].duration_seconds == 625
    assert reranked.groups[0].options[0].resolve_context == {
        "tv_id": 8142858142151200,
        "album_id": 4812694274119401,
        "category_id": 4,
    }


def test_rerank_danmaku_source_search_result_prefers_exact_variety_part_over_historical_page_url() -> None:
    service = DanmakuService({}, provider_order=[])

    result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="iqiyi",
                provider_label="爱奇艺",
                options=[
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="哈哈哈哈哈第6季 第6期下 勇闯海岛",
                        url="https://www.iqiyi.com/v_ep6b.html",
                        duration_seconds=5400,
                        resolve_context={"variety_year": 20260509},
                    ),
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="哈哈哈哈哈第6季 第6期上 邓超表情包大师课2.0",
                        url="https://www.iqiyi.com/v_ep6a.html",
                        duration_seconds=5418,
                        resolve_context={"variety_year": 20260509},
                    ),
                ],
            )
        ]
    )

    reranked = service.rerank_danmaku_source_search_result(
        result,
        query_name="哈哈哈哈哈第六季 20260509期 第6期上：邓超表情包大师课2.0[4K60FPS]",
        preferred_provider="iqiyi",
        preferred_page_url="https://www.iqiyi.com/v_ep6b.html",
        media_duration_seconds=5418,
    )

    assert [option.url for option in reranked.groups[0].options] == [
        "https://www.iqiyi.com/v_ep6a.html",
        "https://www.iqiyi.com/v_ep6b.html",
    ]
    assert reranked.default_option_url == "https://www.iqiyi.com/v_ep6a.html"


def test_rerank_danmaku_source_search_result_prefers_matching_episode_over_historical_page_url_when_duration_unknown() -> None:
    service = DanmakuService({}, provider_order=[])

    result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="iqiyi",
                provider_label="爱奇艺",
                options=[
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="低智商犯罪第1集",
                        url="https://www.iqiyi.com/v_ep1.html",
                    ),
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="低智商犯罪第8集",
                        url="https://www.iqiyi.com/v_ep8.html",
                    ),
                ],
            )
        ],
        default_option_url="https://www.iqiyi.com/v_ep1.html",
        default_provider="iqiyi",
    )

    reranked = service.rerank_danmaku_source_search_result(
        result,
        query_name="低智商犯罪 8集",
        preferred_provider="iqiyi",
        preferred_page_url="https://www.iqiyi.com/v_ep1.html",
    )

    assert reranked.default_option_url == "https://www.iqiyi.com/v_ep8.html"


def test_rerank_prefers_requested_episode_over_historical_page_with_plot_subtitle_names() -> None:
    # The preferred page is the previously watched episode ("第136话 外海风云12");
    # when the query names episode 138, the default must follow the query, not the
    # preference (regression: 《凡人修仙传》138 集预下载到 136 话的弹幕).
    service = DanmakuService({}, provider_order=[])

    result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="bilibili",
                provider_label="B站",
                options=[
                    DanmakuSourceOption(
                        provider="bilibili",
                        name="《凡人修仙传》第136话 外海风云12",
                        url="https://www.bilibili.com/bangumi/play/ep136",
                    ),
                    DanmakuSourceOption(
                        provider="bilibili",
                        name="《凡人修仙传》第138话 外海风云14",
                        url="https://www.bilibili.com/bangumi/play/ep138",
                    ),
                ],
            )
        ],
        default_option_url="https://www.bilibili.com/bangumi/play/ep136",
        default_provider="bilibili",
    )

    reranked = service.rerank_danmaku_source_search_result(
        result,
        query_name="凡人修仙传 138集",
        preferred_provider="bilibili",
        preferred_page_url="https://www.bilibili.com/bangumi/play/ep136",
    )

    assert reranked.default_option_url == "https://www.bilibili.com/bangumi/play/ep138"


def test_rerank_prefers_reg_src_cover_among_same_named_shows() -> None:
    # Two same-named shows both have an ep13 candidate. reg_src identifies the
    # user's show by Tencent cover id; its candidate must rank first even when the
    # other show's candidate is listed first (multi-show disambiguation).
    service = DanmakuService({}, provider_order=[])
    result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[
                    DanmakuSourceOption(
                        provider="tencent",
                        name="百花杀 13集",
                        url="https://v.qq.com/x/cover/mzc003wpj912rrn/ep13.html",
                        duration_seconds=2800,
                        episode_match=True,
                    ),
                    DanmakuSourceOption(
                        provider="tencent",
                        name="百花杀 13集",
                        url="https://v.qq.com/x/cover/mzc00200nfe7al6/ep13.html",
                        duration_seconds=2800,
                        episode_match=True,
                    ),
                ],
            )
        ]
    )

    reranked = service.rerank_danmaku_source_search_result(
        result,
        query_name="百花杀 13集",
        reg_src="https://v.qq.com/x/cover/mzc00200nfe7al6/y4102qxof84.html",
        media_duration_seconds=2800,
    )

    assert "mzc00200nfe7al6" in reranked.default_option_url


def test_rerank_danmaku_source_search_result_drops_promotional_no_episode_items_for_explicit_episode_query() -> None:
    service = DanmakuService({}, provider_order=[])

    result = DanmakuSourceSearchResult(
        groups=[
            DanmakuSourceGroup(
                provider="iqiyi",
                provider_label="爱奇艺",
                options=[
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="灵武大陆 第104集 五脉之魂",
                        url="https://www.iqiyi.com/v_fiiwpasp3c.html",
                        episode_match=True,
                        duration_seconds=625,
                    ),
                    DanmakuSourceOption(
                        provider="iqiyi",
                        name="贺新春 灵武大陆+凌天独尊联动",
                        url="https://www.iqiyi.com/v_24bi1r7wnms.html",
                        duration_seconds=0,
                    ),
                ],
            )
        ],
        default_option_url="https://www.iqiyi.com/v_fiiwpasp3c.html",
        default_provider="iqiyi",
    )

    reranked = service.rerank_danmaku_source_search_result(result, query_name="灵武大陆 104集")

    assert [option.url for option in reranked.groups[0].options] == [
        "https://www.iqiyi.com/v_fiiwpasp3c.html",
    ]


def test_search_danmu_sources_preserves_existing_order_when_media_duration_unknown() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="遮天 88集",
                url="https://v.qq.com/x/cover/first.html",
                ratio=0.98,
                simi=0.98,
                duration_seconds=1560,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="遮天 88集",
                url="https://v.qq.com/x/cover/second.html",
                ratio=0.96,
                simi=0.96,
                duration_seconds=1242,
            ),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    result = service.search_danmu_sources("遮天 88集", media_duration_seconds=0)

    assert [option.url for option in result.groups[0].options] == [
        "https://v.qq.com/x/cover/first.html",
        "https://v.qq.com/x/cover/second.html",
    ]


def test_search_danmu_filters_out_known_candidates_shorter_than_five_minutes() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2",
                url="https://v.qq.com/short",
                ratio=0.99,
                simi=0.99,
                duration_seconds=299,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2",
                url="https://v.qq.com/keep",
                ratio=0.95,
                simi=0.95,
                duration_seconds=5935,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2",
                url="https://v.qq.com/unknown",
                ratio=0.90,
                simi=0.90,
                duration_seconds=0,
            ),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    results = service.search_danmu("疯狂动物城2")

    assert [item.url for item in results] == ["https://v.qq.com/keep", "https://v.qq.com/unknown"]


def test_search_danmu_keeps_candidates_at_five_minutes_or_longer() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2",
                url="https://v.qq.com/exact",
                ratio=0.99,
                simi=0.99,
                duration_seconds=300,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="疯狂动物城2",
                url="https://v.qq.com/long",
                ratio=0.95,
                simi=0.95,
                duration_seconds=600,
            ),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    results = service.search_danmu("疯狂动物城2")

    assert [item.url for item in results] == ["https://v.qq.com/long", "https://v.qq.com/exact"]


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


def test_search_danmu_strips_variety_date_suffix_before_calling_providers() -> None:
    mgtv = FakeProvider(
        "mgtv",
        [DanmakuSearchItem(provider="mgtv", name="你好星期六 20250104期", url="https://mgtv/date")],
        [],
    )
    service = DanmakuService({"mgtv": mgtv}, provider_order=["mgtv"])

    service.search_danmu("你好星期六 20250104期")

    assert mgtv.search_calls == ["你好星期六"]


def test_search_danmu_strips_noisy_variety_issue_title_before_calling_providers() -> None:
    mgtv = FakeProvider(
        "mgtv",
        [DanmakuSearchItem(provider="mgtv", name="哈哈哈哈哈第六季 20260404期", url="https://mgtv/date")],
        [],
    )
    service = DanmakuService({"mgtv": mgtv}, provider_order=["mgtv"])

    service.search_danmu("哈哈哈哈哈第六季 20260404期 第1期上：最狠开局！五哈团命悬一线好刺激 4K60")

    assert mgtv.search_calls == ["哈哈哈哈哈第六季"]


def test_search_danmu_prefers_variety_candidate_with_matching_date() -> None:
    mgtv = FakeProvider(
        "mgtv",
        [
            DanmakuSearchItem(provider="mgtv", name="你好星期六 20241228期", url="https://mgtv/20241228"),
            DanmakuSearchItem(provider="mgtv", name="你好星期六 20250104期", url="https://mgtv/20250104"),
            DanmakuSearchItem(provider="mgtv", name="你好星期六 纯享版", url="https://mgtv/extra"),
        ],
        [],
    )
    service = DanmakuService({"mgtv": mgtv}, provider_order=["mgtv"])

    results = service.search_danmu("你好星期六 20250104期")

    assert [item.url for item in results] == [
        "https://mgtv/20250104",
        "https://mgtv/20241228",
        "https://mgtv/extra",
    ]


def test_search_danmu_prefers_variety_candidate_with_matching_date_from_resolve_context() -> None:
    bilibili = FakeProvider(
        "bilibili",
        [
            DanmakuSearchItem(provider="bilibili", name="《哈哈哈哈哈第六季》 第七期 完整版", url="https://bili/7"),
            DanmakuSearchItem(provider="bilibili", name="《哈哈哈哈哈第六季》 第一期", url="https://bili/1"),
        ],
        [],
    )
    iqiyi = FakeProvider(
        "iqiyi",
        [
            DanmakuSearchItem(
                provider="iqiyi",
                name="哈哈哈哈哈第6季 第1期上 邓超陈赫癫狂式唱山歌",
                url="https://iqiyi/20260404",
                resolve_context={"variety_year": 20260404},
            ),
        ],
        [],
    )
    service = DanmakuService({"bilibili": bilibili, "iqiyi": iqiyi}, provider_order=["bilibili", "iqiyi"])

    results = service.search_danmu("哈哈哈哈哈第六季 20260404期 第1期上：最狠开局！五哈团命悬一线好刺激 4K60")

    assert [item.url for item in results] == [
        "https://iqiyi/20260404",
        "https://bili/1",
        "https://bili/7",
    ]


def test_search_danmu_variety_without_matching_issue_falls_back_to_title_similarity() -> None:
    mgtv = FakeProvider(
        "mgtv",
        [
            DanmakuSearchItem(provider="mgtv", name="你好星期六 纯享版", url="https://mgtv/highlight"),
            DanmakuSearchItem(provider="mgtv", name="你好星期六", url="https://mgtv/main"),
        ],
        [],
    )
    service = DanmakuService({"mgtv": mgtv}, provider_order=["mgtv"])

    results = service.search_danmu("你好星期六 20250111期")

    assert [item.url for item in results] == [
        "https://mgtv/main",
        "https://mgtv/highlight",
    ]


def test_search_danmu_prefers_variety_candidate_matching_date_and_part() -> None:
    # Variety episodes split a single issue into 上/中/下 halves that may share
    # the same air date; the date+part key must select the exact half. Candidate
    # names are date-led (no show name), so this also covers the date-led
    # should_filter_name exemption. (Regression: 《心动的信号 第9季》0 候选.)
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent", name="2026-08-10 第2期中：首次约会 你侬我侬", url="https://tencent/2zhong"
            ),
            DanmakuSearchItem(
                provider="tencent", name="2026-08-10 第2期上：偶像剧现场 情歌对唱", url="https://tencent/2shang"
            ),
            DanmakuSearchItem(
                provider="tencent", name="2026-08-11 第2期下：勇敢者约会", url="https://tencent/2xia"
            ),
            DanmakuSearchItem(
                provider="tencent", name="2026-08-03 第1期上：又争又抢", url="https://tencent/1shang"
            ),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    results = service.search_danmu("心动的信号 第9季 20260810 第2期上")

    assert results[0].url == "https://tencent/2shang"
    assert {item.url for item in results} == {
        "https://tencent/2shang",
        "https://tencent/2zhong",
        "https://tencent/2xia",
        "https://tencent/1shang",
    }


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


def test_search_danmu_prefers_exact_episode_over_title_only_candidate_for_explicit_episode_request() -> None:
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="遮天",
                url="https://v.qq.com/x/cover/series.html",
                ratio=1.0,
                simi=1.0,
                duration_seconds=1560,
            ),
            DanmakuSearchItem(
                provider="tencent",
                name="遮天 88集",
                url="https://v.qq.com/x/cover/ep88.html",
                ratio=0.96,
                simi=0.96,
                duration_seconds=1240,
            ),
        ],
        [],
    )
    service = DanmakuService({"tencent": tencent}, provider_order=["tencent"])

    results = service.search_danmu("遮天 88集")

    assert [item.url for item in results] == [
        "https://v.qq.com/x/cover/ep88.html",
        "https://v.qq.com/x/cover/series.html",
    ]


def test_search_danmu_drops_no_episode_promotional_items_for_explicit_episode_request() -> None:
    iqiyi = FakeProvider(
        "iqiyi",
        [
            DanmakuSearchItem(
                provider="iqiyi",
                name="灵武大陆 第104集 五脉之魂",
                url="https://www.iqiyi.com/v_fiiwpasp3c.html",
                ratio=0.98,
                simi=0.98,
                duration_seconds=625,
            ),
            DanmakuSearchItem(
                provider="iqiyi",
                name="贺新春 灵武大陆+凌天独尊联动",
                url="https://www.iqiyi.com/v_24bi1r7wnms.html",
                ratio=0.80,
                simi=0.80,
                duration_seconds=0,
            ),
        ],
        [],
    )
    service = DanmakuService({"iqiyi": iqiyi}, provider_order=["iqiyi"])

    results = service.search_danmu("灵武大陆 104集")

    assert [item.url for item in results] == ["https://www.iqiyi.com/v_fiiwpasp3c.html"]


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


def test_search_danmu_prefers_movie_candidates_over_commentary_for_title_only_queries() -> None:
    bilibili = FakeProvider(
        "bilibili",
        [
            DanmakuSearchItem(
                provider="bilibili",
                name="《蜂蜜的针》：只有针，没有蜜！邀请《三炮台》激情开炮",
                url="https://www.bilibili.com/video/BV-commentary",
                ratio=0.99,
                simi=0.99,
            ),
            DanmakuSearchItem(
                provider="bilibili",
                name="《蜂蜜的针》首映礼小燕子导演观影支持",
                url="https://www.bilibili.com/video/BV-premiere",
                ratio=0.98,
                simi=0.98,
            ),
            DanmakuSearchItem(
                provider="bilibili",
                name="全!片·《蜂蜜的针》完结",
                url="https://www.bilibili.com/video/BV-full",
                ratio=0.90,
                simi=0.90,
            ),
        ],
        [],
    )
    tencent = FakeProvider(
        "tencent",
        [
            DanmakuSearchItem(
                provider="tencent",
                name="蜂蜜的针",
                url="https://v.qq.com/x/cover/movie.html",
                ratio=0.92,
                simi=0.92,
            )
        ],
        [],
    )
    service = DanmakuService({"bilibili": bilibili, "tencent": tencent}, provider_order=["bilibili", "tencent"])

    results = service.search_danmu("蜂蜜的针")

    assert [item.url for item in results] == [
        "https://v.qq.com/x/cover/movie.html",
        "https://www.bilibili.com/video/BV-full",
    ]


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


def test_provider_key_for_url_returns_first_enabled_supporting_provider() -> None:
    tencent = FakeProvider("tencent", [], [])
    youku = FakeProvider("youku", [], [])
    service = DanmakuService(
        {"tencent": tencent, "youku": youku},
        provider_order=["tencent", "youku"],
    )

    assert service.provider_key_for_url("https://video.youku/item") == "youku"


def test_provider_key_for_url_rejects_dynamically_disabled_provider() -> None:
    disabled = ["youku"]
    service = DanmakuService(
        {"youku": FakeProvider("youku", [], [])},
        provider_order=["youku"],
        disabled_provider_ids_loader=lambda: disabled,
    )

    with pytest.raises(ProviderNotSupportedError, match="不支持的弹幕来源"):
        service.provider_key_for_url("https://video.youku/item")


def test_provider_key_for_url_rejects_unknown_url() -> None:
    service = DanmakuService({}, provider_order=[])

    with pytest.raises(ProviderNotSupportedError, match="不支持的弹幕来源"):
        service.provider_key_for_url("https://unknown.example/video/1")


def test_resolve_danmu_prefers_selected_option_provider_when_url_is_ambiguous() -> None:
    class AmbiguousProvider(FakeProvider):
        def supports(self, page_url: str) -> bool:
            return "shared.example" in page_url

    iqiyi = AmbiguousProvider(
        "iqiyi",
        [],
        [DanmakuRecord(time_offset=1.0, pos=1, color="16777215", content="wrong")],
    )
    sohu = AmbiguousProvider(
        "sohu",
        [],
        [DanmakuRecord(time_offset=2.0, pos=1, color="16777215", content="right")],
    )
    service = DanmakuService({"iqiyi": iqiyi, "sohu": sohu}, provider_order=["iqiyi", "sohu"])
    option = DanmakuSourceOption(provider="sohu", name="不识君 第2集", url="https://shared.example/video")

    xml = service.resolve_danmu("https://shared.example/video", option=option)

    assert "right" in xml
    assert sohu.resolve_calls == ["https://shared.example/video"]
    assert iqiyi.resolve_calls == []


def test_resolve_danmu_uses_selected_option_provider_context_even_when_url_matches_another_site() -> None:
    class ContextOnlyProvider(FakeProvider):
        def __init__(self, key: str, items: list[DanmakuSearchItem], records: list[DanmakuRecord]) -> None:
            super().__init__(key, items, records)
            self.primed_contexts: list[dict[str, str | int | None]] = []

        def supports(self, page_url: str) -> bool:
            return self.key in page_url

        def prime_resolve_context(self, page_url: str, resolve_context: dict[str, str | int | None]) -> None:
            self.primed_contexts.append(dict(resolve_context))

    iqiyi = ContextOnlyProvider(
        "iqiyi",
        [],
        [DanmakuRecord(time_offset=1.0, pos=1, color="16777215", content="wrong")],
    )
    sohu = ContextOnlyProvider(
        "sohu",
        [],
        [DanmakuRecord(time_offset=2.0, pos=1, color="16777215", content="right")],
    )
    service = DanmakuService({"iqiyi": iqiyi, "sohu": sohu}, provider_order=["iqiyi", "sohu"])
    option = DanmakuSourceOption(
        provider="sohu",
        name="不识君 第2集",
        url="https://www.iqiyi.com/v_demo.html?vfm=m_312_shsp",
        resolve_context={"aid": "1001352658", "vid": "458728253"},
    )

    xml = service.resolve_danmu(option.url, option=option)

    assert "right" in xml
    assert sohu.primed_contexts == [{"aid": "1001352658", "vid": "458728253"}]
    assert sohu.resolve_calls == [option.url]
    assert iqiyi.resolve_calls == []


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

    assert service.provider_order == [
        "tencent",
        "youku",
        "bilibili",
        "iqiyi",
        "mgtv",
        "sohu",
        "migu",
        "renren",
        "dandan",
        "bahamut",
        "animeko",
    ]


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

    assert service.provider_order == [
        "tencent",
        "youku",
        "bilibili",
        "iqiyi",
        "mgtv",
        "sohu",
        "migu",
        "renren",
        "dandan",
        "bahamut",
        "animeko",
    ]


def test_default_service_includes_iqiyi_provider_in_fixed_order() -> None:
    service = create_default_danmaku_service()

    assert "iqiyi" in service.provider_order


def test_new_anime_provider_labels_are_user_facing() -> None:
    providers = {
        key: FakeProvider(
            key,
            [
                DanmakuSearchItem(
                    provider=key,
                    name="迷宫饭 第1集",
                    url=f"{key}://episode/1",
                )
            ],
            [],
        )
        for key in ("dandan", "bahamut", "animeko")
    }
    service = DanmakuService(
        providers,
        provider_order=["dandan", "bahamut", "animeko"],
    )

    result = service.search_danmu_sources("迷宫饭 第1集")

    assert [(group.provider, group.provider_label) for group in result.groups] == [
        ("dandan", "弹弹Play"),
        ("bahamut", "巴哈姆特"),
        ("animeko", "Animeko"),
    ]


def test_resolve_routes_cached_internal_url_without_context() -> None:
    animeko = FakeProvider(
        "animeko",
        [],
        [DanmakuRecord(1.0, 1, "16777215", "缓存")],
    )
    service = DanmakuService(
        {"animeko": animeko},
        provider_order=["animeko"],
    )

    xml = service.resolve_danmu("animeko://episode/4201")

    assert "缓存" in xml
    assert animeko.resolve_calls == ["animeko://episode/4201"]


def test_search_danmu_sources_uses_sohu_provider_label() -> None:
    sohu = FakeProvider(
        "sohu",
        [DanmakuSearchItem(provider="sohu", name="剑来 第1集", url="https://tv.sohu.com/v/demo.html", ratio=0.8, simi=0.8)],
        [],
    )
    service = DanmakuService({"sohu": sohu}, provider_order=["sohu"])

    result = service.search_danmu_sources("剑来 第1集")

    assert result.groups[0].provider == "sohu"
    assert result.groups[0].provider_label == "搜狐"
    assert result.default_option_url == "https://tv.sohu.com/v/demo.html"


def test_default_service_raises_clear_error_for_invalid_mgtv_resolution_url() -> None:
    service = create_default_danmaku_service()

    with pytest.raises(DanmakuResolveError, match="MGTV.*invalid play url"):
        service.resolve_danmu("https://www.mgtv.com/b/demo/1.html")


def test_default_service_still_rejects_unknown_urls() -> None:
    service = create_default_danmaku_service()

    with pytest.raises(ProviderNotSupportedError):
        service.resolve_danmu("https://unknown.example/video/1")


class FakeDoubanDiscovery:
    def __init__(self, subjects, vendors_by_id) -> None:
        self._subjects = subjects
        self._vendors_by_id = vendors_by_id
        self.search_calls: list[str] = []
        self.vendor_calls: list[str] = []

    def search_subjects(self, keyword: str):
        self.search_calls.append(keyword)
        return list(self._subjects)

    def fetch_vendors(self, douban_id: str):
        self.vendor_calls.append(douban_id)
        return list(self._vendors_by_id.get(douban_id, []))


class ExpandableFakeProvider(FakeProvider):
    """A provider whose MbSearch returns nothing, but can expand a douban page URL."""

    def __init__(self, key, expand_items):
        super().__init__(key, [], [])
        self._expand_items = expand_items
        self.expand_calls: list[str] = []

    def search(self, name, original_name=None):
        self.search_calls.append(name)
        self.original_name_calls.append(original_name)
        return []

    def expand_page_url(self, page_url, query_name):
        self.expand_calls.append(page_url)
        return list(self._expand_items)


def test_search_danmu_falls_back_to_douban_discovery_when_builtin_search_empty() -> None:
    from atv_player.danmaku.discovery.douban import DoubanSubject, DoubanVendor

    ep13 = DanmakuSearchItem(
        provider="tencent",
        name="百花杀 13集",
        url="https://v.qq.com/x/cover/mzc00200nfe7al6/ep13.html",
        duration_seconds=2700,
    )
    tencent = ExpandableFakeProvider("tencent", [ep13])
    discovery = FakeDoubanDiscovery(
        subjects=[DoubanSubject(douban_id="35876897", title="百花杀", year="2024", type_name="电视剧")],
        vendors_by_id={"35876897": [DoubanVendor(provider="tencent", media_id="mzc00200nfe7al6")]},
    )
    service = DanmakuService(
        {"tencent": tencent},
        provider_order=["tencent"],
        douban_discovery=discovery,
    )

    results = service.search_danmu("百花杀 13集")

    assert any(item.url == ep13.url for item in results)
    assert discovery.search_calls == ["百花杀"]
    assert tencent.expand_calls == ["https://v.qq.com/x/cover/mzc00200nfe7al6/"]


class _EmptySearchTencent:
    key = "tencent"

    def search(self, name, original_name=None):
        return []

    def resolve(self, page_url):
        return []

    def supports(self, page_url):
        return "qq.com" in page_url


class _EmptyDiscovery:
    def search_subjects(self, keyword):
        return []

    def fetch_vendors(self, douban_id):
        return []


def test_search_danmu_falls_back_to_other_when_builtin_and_douban_miss() -> None:
    from atv_player.danmaku.providers.other import OtherDanmakuProvider

    other = OtherDanmakuProvider(get=lambda *a, **k: None, server="https://dmku.hls.one/")
    service = DanmakuService(
        {"tencent": _EmptySearchTencent()},
        provider_order=["tencent"],
        douban_discovery=_EmptyDiscovery(),
        other_provider=other,
    )

    results = service.search_danmu("冷门剧 5集", "https://v.qq.com/x/cover/xyz/abc.html")

    assert any(it.provider == "other" for it in results)
    assert results[0].url == "https://v.qq.com/x/cover/xyz/abc.html"


def test_search_danmu_skips_other_fallback_for_drive_share_links() -> None:
    from atv_player.danmaku.providers.other import OtherDanmakuProvider

    other = OtherDanmakuProvider(get=lambda *a, **k: None, server="https://dmku.hls.one/")
    service = DanmakuService(
        {"tencent": _EmptySearchTencent()},
        provider_order=["tencent"],
        douban_discovery=_EmptyDiscovery(),
        other_provider=other,
    )

    # 回归：网盘分享链接不是播放页，弹幕库没有按它索引的弹幕；合成 other 候选只会
    # 产生必败下载并把结果污染进源搜索缓存（"爱是愤怒 9794集" 循环失败）。
    assert service.search_danmu("爱是愤怒 9794集", "https://pan.baidu.com/s/1ZuAV-E16fxFHXPpExkkHpw?pwd=9527") == []
    assert service.search_danmu("冷门剧", "https://pan.quark.cn/s/abc123") == []


def test_resolve_danmu_routes_to_other_when_selected() -> None:
    from atv_player.danmaku.models import DanmakuSourceOption
    from atv_player.danmaku.providers.other import OtherDanmakuProvider

    class FakeOther(OtherDanmakuProvider):
        def __init__(self):
            super().__init__(get=lambda *a, **k: None, server="x")
            self.resolved = []

        def resolve(self, page_url):
            self.resolved.append(page_url)
            from atv_player.danmaku.models import DanmakuRecord

            return [DanmakuRecord(time_offset=1.0, pos=1, color="16777215", content="x")]

    other = FakeOther()
    service = DanmakuService(
        {"tencent": _EmptySearchTencent()},
        provider_order=["tencent"],
        other_provider=other,
    )
    option = DanmakuSourceOption(provider="other", name="冷门剧 5集", url="https://v.qq.com/x/cover/xyz/abc.html")

    xml = service.resolve_danmu("https://v.qq.com/x/cover/xyz/abc.html", option)

    assert other.resolved == ["https://v.qq.com/x/cover/xyz/abc.html"]
    assert "<d " in xml


def test_service_ignores_removed_danmaku_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("ATV_DANMU_BLOCKED_WORDS", "/normal/")
    monkeypatch.setenv("ATV_DANMU_GROUP_MINUTE", "5")
    monkeypatch.setenv("ATV_DANMU_CONVERT_TOP_BOTTOM", "1")
    monkeypatch.setenv("ATV_DANMU_OFFSET", "demo:99")
    provider = FakeProvider(
        "fake",
        [],
        [DanmakuRecord(time_offset=10.0, pos=5, color="16777215", content="normal")],
    )
    service = DanmakuService(
        {"fake": provider},
        provider_order=["fake"],
        config_loader=lambda: AppConfig(),
    )

    xml = service.resolve_danmu("https://fake.example/video")

    assert "normal" in xml
    assert 'p="10.0,5,' in xml


def test_service_applies_persisted_danmaku_cleaning_in_order() -> None:
    provider = FakeProvider(
        "fake",
        [],
        [
            DanmakuRecord(time_offset=1.0, pos=5, color="16777215", content="SPAM duplicate"),
            DanmakuRecord(time_offset=2.0, pos=5, color="16777215", content="duplicate"),
            DanmakuRecord(time_offset=3.0, pos=4, color="16777215", content="duplicate"),
            DanmakuRecord(time_offset=70.0, pos=4, color="16777215", content="duplicate"),
        ],
    )
    service = DanmakuService(
        {"fake": provider},
        provider_order=["fake"],
        config_loader=lambda: AppConfig(
            danmaku_blocked_words=["spam"],
            danmaku_duplicate_window_minutes=1,
            danmaku_convert_top_bottom_to_scroll=True,
        ),
    )

    xml = service.resolve_danmu("https://fake.example/video")

    assert "SPAM" not in xml
    assert xml.count(">duplicate</d>") == 2
    assert 'p="2.0,1,' in xml
    assert 'p="70.0,1,' in xml


def test_search_danmu_queries_all_providers_in_one_concurrent_round(monkeypatch) -> None:
    # 12 源在批并发 4 下要排 3 批,空结果轮次最坏 3×10s 超时;搜索轮的并发
    # 上限应等于源数量,让单轮耗时只由最慢单源决定。
    import atv_player.danmaku.service as service_module

    providers = {
        key: FakeProvider(key, [], [])
        for key in ("tencent", "youku", "iqiyi", "mgtv", "bilibili", "sohu")
    }
    service = DanmakuService(providers, provider_order=list(providers))

    observed_max_workers: list[int] = []
    real_iter_bounded_settled = service_module.iter_bounded_settled

    def recording_iter_bounded_settled(items, worker, **kwargs):
        observed_max_workers.append(int(kwargs.get("max_workers", 0)))
        yield from real_iter_bounded_settled(items, worker, **kwargs)

    monkeypatch.setattr(service_module, "iter_bounded_settled", recording_iter_bounded_settled)

    results = service.search_danmu("冷门剧 1集")

    assert results == []
    assert observed_max_workers == [len(providers)]


def _mushenji_bilibili_group() -> DanmakuSourceGroup:
    # B站式集级候选:整组同剧不同话,API 原始序非数字序
    return DanmakuSourceGroup(
        provider="bilibili",
        provider_label="B站",
        options=[
            DanmakuSourceOption(provider="bilibili", name="《牧神记》第18话 冰潮", url="https://www.bilibili.com/bangumi/play/ep18"),
            DanmakuSourceOption(provider="bilibili", name="《牧神记》第2话 我是霸体", url="https://www.bilibili.com/bangumi/play/ep2"),
            DanmakuSourceOption(provider="bilibili", name="《牧神记》第100话 鬼上身", url="https://www.bilibili.com/bangumi/play/ep100", episode_match=True),
            DanmakuSourceOption(provider="bilibili", name="《牧神记》第58话 虫军", url="https://www.bilibili.com/bangumi/play/ep58"),
        ],
    )


def test_rerank_default_pick_is_episode_aware_for_per_episode_candidates() -> None:
    # 查询带集数锚点但 exact 匹配被季名后缀挡住("牧神记 S01 100集")时,
    # provider 兜底不能按 options[0] 随机抽集——线上第100集自动下到了第2话
    service = DanmakuService({}, provider_order=[])
    result = DanmakuSourceSearchResult(groups=[_mushenji_bilibili_group()])

    reranked = service.rerank_danmaku_source_search_result(
        result,
        query_name="牧神记 S01 100集",
        reg_src="/牧神记/S01 100/S01E100.mp4",
        preferred_provider="bilibili",
        # 系列偏好被此前某集的自动下载写成了第2话地址
        preferred_page_url="https://www.bilibili.com/bangumi/play/ep2",
        media_duration_seconds=0,
    )

    assert reranked.default_option_url == "https://www.bilibili.com/bangumi/play/ep100"


def test_rerank_episode_specific_preferred_page_does_not_outrank_episode_match() -> None:
    # 时长已知、排序生效时,"别的集"的偏好 URL 不得压过集数锚点命中的候选
    service = DanmakuService({}, provider_order=[])
    result = DanmakuSourceSearchResult(groups=[_mushenji_bilibili_group()])

    reranked = service.rerank_danmaku_source_search_result(
        result,
        query_name="牧神记 S01 100集",
        reg_src="/牧神记/S01 100/S01E100.mp4",
        preferred_provider="bilibili",
        preferred_page_url="https://www.bilibili.com/bangumi/play/ep2",
        media_duration_seconds=1500,
    )

    assert reranked.groups[0].options[0].url == "https://www.bilibili.com/bangumi/play/ep100"
    assert reranked.default_option_url == "https://www.bilibili.com/bangumi/play/ep100"
