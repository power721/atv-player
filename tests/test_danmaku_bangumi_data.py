import json

import httpx
import pytest

from atv_player.danmaku.discovery import bangumi_data as bd
from atv_player.danmaku.discovery.bangumi_data import (
    BangumiDataDiscovery,
    BangumiHit,
    _extract_season,
    _prune,
)


def _dataset() -> dict:
    return {
        "items": [
            {
                "title": "鬼滅の刃",
                "type": "tv",
                "begin": "2019-04-06",
                "titleTranslate": {"zh-Hans": ["鬼灭之刃"], "zh-Hant": ["鬼滅之刃"]},
                "sites": [
                    {"site": "bangumi", "id": "123"},
                    {"site": "bilibili", "id": "28229233", "season_id": "28229233"},
                    {"site": "gamer", "id": "999", "video_sn": "12345"},
                ],
            },
            {
                "title": "鬼滅の刃 無限列車編",
                "type": "tv",
                "titleTranslate": {"zh-Hans": ["鬼灭之刃 第二季"]},
                "sites": [{"site": "bilibili", "season_id": "28229234"}],
            },
            {
                "title": "No Target Site Anime",
                "type": "tv",
                "sites": [
                    {"site": "bangumi", "id": "999"},
                    {"site": "tmdb", "id": "1"},
                ],
            },
        ]
    }


def _discovery_with(dataset: dict) -> BangumiDataDiscovery:
    discovery = BangumiDataDiscovery(get=lambda *a, **k: None)
    discovery._items = _prune(dataset)
    return discovery


def test_extract_season_reads_explicit_markers_not_trailing_digits() -> None:
    assert _extract_season("鬼灭之刃 第二季") == 2
    assert _extract_season("Demon Slayer S2") == 2
    assert _extract_season("Foo Season 3") == 3
    # bare trailing digits are NOT a season (avoids 高达00 / 0079 confusion)
    assert _extract_season("机动战士高达00") is None
    assert _extract_season("某剧") is None


def test_prune_keeps_items_with_usable_sites_and_collects_titles() -> None:
    items = _prune(_dataset())
    assert len(items) == 2
    first = items[0]
    assert first["bilibili_season"] == "28229233"
    assert first["bahamut_video_sn"] == "12345"
    assert "鬼滅の刃" in first["titles"]
    assert "鬼灭之刃" in first["titles"]


def test_prune_drops_items_without_target_sites() -> None:
    assert (
        _prune({"items": [{"title": "X", "sites": [{"site": "bangumi", "id": "1"}]}]})
        == []
    )


def test_search_matches_and_builds_provider_urls() -> None:
    discovery = _discovery_with(_dataset())
    hits = discovery.search("鬼灭之刃")
    bilibili = next(h for h in hits if h.provider == "bilibili")
    bahamut = next(h for h in hits if h.provider == "bahamut")
    assert bilibili.page_url == "https://www.bilibili.com/bangumi/play/ss28229233"
    assert bahamut.page_url == "bahamut://series/12345"


def test_search_matches_traditional_query_via_simplified_normalization() -> None:
    discovery = _discovery_with(_dataset())
    hits = discovery.search("鬼滅之刃")
    assert any(h.provider == "bilibili" for h in hits)


def test_search_season_filter_isolates_requested_season() -> None:
    discovery = _discovery_with(_dataset())
    bilibili = [
        h for h in discovery.search("鬼灭之刃 第二季") if h.provider == "bilibili"
    ]
    assert len(bilibili) == 1
    assert bilibili[0].page_url.endswith("ss28229234")


def test_search_season_one_excludes_later_seasons() -> None:
    discovery = _discovery_with(_dataset())
    seasons = {
        h.page_url.rsplit("ss", 1)[1]
        for h in discovery.search("鬼灭之刃 第一季")
        if h.provider == "bilibili"
    }
    assert "28229233" in seasons
    assert "28229234" not in seasons


def test_search_no_match_returns_empty() -> None:
    assert _discovery_with(_dataset()).search("完全不存在的标题xyz") == []


def test_ensure_loaded_uses_fresh_disk_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bd, "app_cache_dir", lambda: tmp_path)
    (tmp_path / bd._CACHE_FILENAME).write_text(
        json.dumps({"items": _prune(_dataset())}, ensure_ascii=False),
        encoding="utf-8",
    )
    discovery = BangumiDataDiscovery(
        get=lambda *a, **k: pytest.fail("should not download")
    )
    assert discovery.ensure_loaded() is True
    assert discovery.search("鬼灭之刃")


def test_ensure_loaded_downloads_and_persists_when_no_cache(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(bd, "app_cache_dir", lambda: tmp_path)

    def fake_get(url, **kwargs):
        return httpx.Response(200, json=_dataset())

    discovery = BangumiDataDiscovery(get=fake_get)
    assert discovery.ensure_loaded() is True
    assert discovery._items is not None
    assert (tmp_path / bd._CACHE_FILENAME).exists()


def test_ensure_loaded_falls_through_failing_cdns(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bd, "app_cache_dir", lambda: tmp_path)
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "jsdelivr.net/npm/@wan0ge" in url:
            return httpx.Response(500)
        return httpx.Response(200, json=_dataset())

    discovery = BangumiDataDiscovery(get=fake_get)
    assert discovery.ensure_loaded() is True
    assert len(calls) >= 2


def test_ensure_loaded_all_cdns_fail_returns_false(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bd, "app_cache_dir", lambda: tmp_path)

    def fake_get(url, **kwargs):
        raise httpx.HTTPError("boom")

    discovery = BangumiDataDiscovery(get=fake_get)
    assert discovery.ensure_loaded() is False
    assert discovery.search("鬼灭之刃") == []


def test_service_augments_pool_when_bangumi_data_enabled() -> None:
    from atv_player.danmaku.models import DanmakuSearchItem
    from atv_player.danmaku.service import DanmakuService
    from atv_player.models import AppConfig

    class FakeDiscovery:
        def __init__(self) -> None:
            self.called = False

        def search(self, keyword: str) -> list[BangumiHit]:
            self.called = True
            return [
                BangumiHit(
                    provider="bilibili",
                    page_url="https://www.bilibili.com/bangumi/play/ss28229233",
                    title="鬼灭之刃",
                )
            ]

    class FakeBilibili:
        key = "bilibili"

        def search(self, name, original_name=None):
            return []

        def supports(self, url):
            return True

        def expand_page_url(self, page_url, query_name):
            return [
                DanmakuSearchItem(
                    provider="bilibili",
                    name=f"{query_name} 第1集",
                    url="https://www.bilibili.com/bangumi/play/ep1",
                )
            ]

    discovery = FakeDiscovery()
    service = DanmakuService(
        providers={"bilibili": FakeBilibili()},
        provider_order=["bilibili"],
        config_loader=lambda: AppConfig(bangumi_data_danmaku_enabled=True),
        bangumi_data_discovery=discovery,
    )
    results = service.search_danmu("鬼灭之刃", reg_src="", provider_filter="")
    assert discovery.called is True
    assert any(r.provider == "bilibili" for r in results)


def test_service_skips_bangumi_data_when_disabled() -> None:
    from atv_player.danmaku.service import DanmakuService
    from atv_player.models import AppConfig

    class FakeDiscovery:
        def __init__(self) -> None:
            self.called = False

        def search(self, keyword: str) -> list[BangumiHit]:
            self.called = True
            return []

    discovery = FakeDiscovery()
    service = DanmakuService(
        providers={},
        provider_order=[],
        config_loader=lambda: AppConfig(bangumi_data_danmaku_enabled=False),
        bangumi_data_discovery=discovery,
    )
    service.search_danmu("鬼灭之刃", reg_src="", provider_filter="")
    assert discovery.called is False


def test_extract_season_reads_trailing_roman_numerals() -> None:
    # Unicode 罗马数字季号;ASCII 形式无法与型号区分,不参与识别
    assert _extract_season("无职转生Ⅱ") == 2
    assert _extract_season("OVERLORD Ⅳ") == 4
    assert _extract_season("OVERLORD IV") is None
