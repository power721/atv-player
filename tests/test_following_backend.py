# ruff: noqa: E501
"""服务端追剧信号同步(FollowingBackendSyncService)单测:匹配优先级/负缓存/自动订阅/信号同步/订阅导入。"""
from pathlib import Path

import pytest

from atv_player.following_backend import (
    FollowingBackendSyncService,
    _SubscriptionIndex,
    _tmdb_series_id,
    find_missing_subscriptions,
)
from atv_player.following_models import FollowingRecord
from atv_player.following_repository import FollowingRepository
from atv_player.metadata.scrape import MetadataScrapeCandidate, MetadataScrapeGroup


def _record(**overrides):
    values = dict(
        id=0,
        title="凡人修仙传",
        original_title="",
        media_kind="anime",
        season_number=1,
        poster="",
        backdrop="",
        rating="",
        provider="tmdb",
        provider_id="tv:456:season:1",
        provider_priority=[],
        external_ids={"tmdb": "456", "douban": "789"},
        source_bindings=[],
        current_season_number=1,
        current_episode=3,
        position_seconds=0,
        watched_latest_episode=True,
        latest_episode=3,
        previous_latest_episode=3,
        total_episodes=12,
        has_update=False,
        new_episode_count=0,
        homepage_prompt_pending=False,
        prompt_snoozed_until=0,
        created_at=100,
        updated_at=100,
        last_played_at=90,
        last_checked_at=80,
        next_check_after=0,
        last_error="",
    )
    values.update(overrides)
    return FollowingRecord(**values)


def _sub(**overrides):
    payload = {
        "id": 7,
        "name": "凡人修仙传",
        "season": 1,
        "metaProvider": "tmdb",
        "metaId": "456",
        "doubanId": 789,
        "currentEpisodes": 5,
        "officialEpisodes": 4,
        "maxEpisode": 5,
    }
    payload.update(overrides)
    return payload


class FakeApiClient:
    base_url = "http://192.168.50.60:4567"
    username = "harold"

    def __init__(self, subscriptions=None, create_result=None, create_error=None):
        self.subscriptions = list(subscriptions or [])
        self.create_result = create_result
        self.create_error = create_error
        self.create_calls: list[dict] = []
        self.list_calls = 0

    def list_media_subscriptions(self):
        self.list_calls += 1
        return list(self.subscriptions)

    def create_media_subscription(self, payload):
        self.create_calls.append(dict(payload))
        if self.create_error is not None:
            raise self.create_error
        return dict(self.create_result or {})


def _service(api, repository, *, enabled=True, auto_subscribe=False, now=None):
    return FollowingBackendSyncService(
        api,
        repository,
        settings_provider=lambda: {"enabled": enabled, "auto_subscribe": auto_subscribe},
        now=now or (lambda: 1000),
    )


def test_subscription_index_matches_by_tmdb_then_douban_then_title() -> None:
    index = _SubscriptionIndex([
        _sub(id=7, metaProvider="tmdb", metaId="456", doubanId=0, name="无关剧名"),
        _sub(id=8, metaProvider="douban", metaId="", doubanId=789, name="另一个名字"),
        _sub(id=9, metaProvider="", metaId="", doubanId=0, name="凡人修仙传"),
    ])
    assert index.match(_record())["id"] == 7
    no_tmdb = _record(external_ids={"douban": "789"})
    assert index.match(no_tmdb)["id"] == 8
    no_ids = _record(external_ids={}, provider="bangumi", provider_id="subject:1")
    assert index.match(no_ids)["id"] == 9
    assert index.match(_record(title="完全不同", external_ids={})) is None


def test_subscription_index_prefers_richer_subscription_on_duplicate_key() -> None:
    index = _SubscriptionIndex([
        _sub(id=7, currentEpisodes=3),
        _sub(id=8, currentEpisodes=6),
    ])
    assert index.match(_record())["id"] == 8


def test_tmdb_series_id_variants() -> None:
    assert _tmdb_series_id("tv:456:season:1") == "456"
    assert _tmdb_series_id("tv:456") == "456"
    assert _tmdb_series_id("456") == "456"
    assert _tmdb_series_id("") == ""


def test_sync_blocking_disabled_returns_empty(tmp_path: Path) -> None:
    repository = FollowingRepository(tmp_path / "app.db")
    repository.upsert(_record())
    api = FakeApiClient(subscriptions=[_sub()])
    results = _service(api, repository, enabled=False).sync_blocking()
    assert results == []
    assert api.create_calls == []


def test_sync_blocking_applies_signal_and_skips_movies(tmp_path: Path) -> None:
    repository = FollowingRepository(tmp_path / "app.db")
    record_id = repository.upsert(_record())
    movie_id = repository.upsert(_record(title="流浪地球", media_kind="movie", provider="tmdb", provider_id="movie:1", external_ids={"tmdb": "99"}))
    api = FakeApiClient(subscriptions=[_sub()])
    results = _service(api, repository).sync_blocking()
    assert results == [(record_id, True)]
    record = repository.get(record_id)
    assert record.latest_episode == 5
    assert record.has_update is True
    assert record.new_episode_count == 2
    bindings = [binding for binding in record.source_bindings if binding.source_kind == "msub"]
    assert len(bindings) == 1 and bindings[0].vod_id == "msub:7"
    movie = repository.get(movie_id)
    assert not any(binding.source_kind == "msub" for binding in movie.source_bindings)


def test_sync_blocking_lower_signal_does_not_regress(tmp_path: Path) -> None:
    repository = FollowingRepository(tmp_path / "app.db")
    record_id = repository.upsert(_record(latest_episode=8, current_episode=8))
    api = FakeApiClient(subscriptions=[_sub(currentEpisodes=5)])
    results = _service(api, repository).sync_blocking()
    assert results == []
    assert repository.get(record_id).latest_episode == 8


def test_auto_subscribe_creates_with_tmdb_binding(tmp_path: Path) -> None:
    repository = FollowingRepository(tmp_path / "app.db")
    record_id = repository.upsert(_record())
    api = FakeApiClient(create_result={"id": 12, "currentEpisodes": 0})
    results = _service(api, repository, auto_subscribe=True).sync_blocking()
    assert api.create_calls == [
        {"name": "凡人修仙传", "keyword": "凡人修仙传", "season": 1, "metaProvider": "tmdb", "metaId": "456"}
    ]
    assert results == []
    bindings = [b for b in repository.get(record_id).source_bindings if b.source_kind == "msub"]
    assert bindings and bindings[0].vod_id == "msub:12"


def test_auto_subscribe_failure_uses_negative_cache(tmp_path: Path) -> None:
    repository = FollowingRepository(tmp_path / "app.db")
    repository.upsert(_record())
    api = FakeApiClient(create_error=RuntimeError("创建失败"))
    service = _service(api, repository, auto_subscribe=True)
    assert service.sync_blocking() == []
    assert len(api.create_calls) == 1
    service.sync_blocking()
    assert len(api.create_calls) == 1  # 负缓存期内不重复 POST
    service._negative_cache.clear()
    service.sync_blocking()
    assert len(api.create_calls) == 2


def test_auto_subscribe_douban_fallback_payload(tmp_path: Path) -> None:
    repository = FollowingRepository(tmp_path / "app.db")
    repository.upsert(_record(external_ids={"douban": "789"}, provider="douban", provider_id="789"))
    api = FakeApiClient(create_result={"id": 13})
    _service(api, repository, auto_subscribe=True).sync_blocking()
    assert api.create_calls == [
        {"name": "凡人修仙传", "keyword": "凡人修仙传", "season": 1, "doubanId": 789}
    ]


def test_sync_blocking_swallows_api_failure(tmp_path: Path) -> None:
    repository = FollowingRepository(tmp_path / "app.db")
    repository.upsert(_record())

    class BrokenApi(FakeApiClient):
        def list_media_subscriptions(self):
            raise RuntimeError("网络失败")

    results = _service(BrokenApi(), repository).sync_blocking()
    assert results == []


def test_sync_blocking_skips_when_background_sync_holds_lock(tmp_path: Path) -> None:
    repository = FollowingRepository(tmp_path / "app.db")
    repository.upsert(_record())
    api = FakeApiClient(subscriptions=[_sub()])
    service = _service(api, repository)
    service._sync_lock.acquire()

    results = service.sync_blocking()

    assert results == []
    assert service._sync_lock.locked()
    service._sync_lock.release()


def test_sync_soon_respects_interval_and_disabled(tmp_path: Path, monkeypatch) -> None:
    repository = FollowingRepository(tmp_path / "app.db")
    repository.upsert(_record())
    api = FakeApiClient(subscriptions=[_sub()])
    clock = {"monotonic": 1000.0}
    monkeypatch.setattr("atv_player.following_backend.time.monotonic", lambda: clock["monotonic"])
    service = _service(api, repository)

    service.sync_soon()  # 派发后台线程,等待完成
    service._sync_lock.acquire()
    service._sync_lock.release()
    assert api.list_calls == 1

    clock["monotonic"] = 1100.0  # 限频窗口内:被吞
    service.sync_soon()
    service._sync_lock.acquire()
    service._sync_lock.release()
    assert api.list_calls == 1

    clock["monotonic"] = 1400.0  # 越过 5 分钟窗口:放行
    service.sync_soon()
    service._sync_lock.acquire()
    service._sync_lock.release()
    assert api.list_calls == 2

    clock["monotonic"] = 2000.0
    disabled = _service(api, repository, enabled=False)
    disabled.sync_soon()
    service._sync_lock.acquire()
    service._sync_lock.release()
    assert api.list_calls == 2


# ---- find_missing_subscriptions(导入候选筛选) ----


def test_find_missing_subscriptions_filters_matched_keys() -> None:
    records = [_record()]  # tmdb 456 / douban 789 / 标题 凡人修仙传,季 1
    subs = [
        _sub(),  # tmdb 匹配
        _sub(id=8, metaProvider="douban", metaId="", doubanId=789, name="别名"),  # 豆瓣匹配
        _sub(id=9, metaProvider="", metaId="", doubanId=0, name="凡人修仙传"),  # 标题匹配
        _sub(id=10, doubanId=0, name="末日地堡", metaId="999"),  # 无任何匹配 → 待导入
        _sub(id=11, doubanId=0, metaId="456", season=3),  # 同剧不同季 → 待导入
    ]
    missing = find_missing_subscriptions(subs, records)
    assert [sub["id"] for sub in missing] == [10, 11]


def test_find_missing_subscriptions_consistent_with_index() -> None:
    records = [_record(), _record(title="仙剑三", external_ids={"tmdb": "111"}, provider="tmdb", provider_id="tv:111")]
    subs = [
        _sub(),
        _sub(id=10, doubanId=0, name="末日地堡", metaId="999"),
        _sub(id=11, metaProvider="", metaId="", doubanId=0, name="仙剑三"),
    ]
    index = _SubscriptionIndex(subs)
    matched_ids = set()
    for record in records:
        sub = index.match(record)
        if sub is not None:
            matched_ids.add(sub["id"])
    missing_ids = {sub["id"] for sub in find_missing_subscriptions(subs, records)}
    assert missing_ids == {10}
    assert matched_ids == {7, 11}
    assert not matched_ids & missing_ids


# ---- service 公开方法(list/apply/is_enabled) ----


def test_service_public_methods(tmp_path: Path) -> None:
    repository = FollowingRepository(tmp_path / "app.db")
    record_id = repository.upsert(_record())
    api = FakeApiClient(subscriptions=[_sub()])
    service = _service(api, repository)
    assert service.is_enabled() is True
    assert not _service(api, repository, enabled=False).is_enabled()
    assert service.list_subscriptions() == [_sub()]
    assert service.apply_subscription(record_id, _sub()) is True
    record = repository.get(record_id)
    bindings = [binding for binding in record.source_bindings if binding.source_kind == "msub"]
    assert bindings and bindings[0].vod_id == "msub:7" and record.latest_episode == 5


# ---- FollowingController.import_backend_subscriptions(导入编排) ----


class FakeBackendSyncService:
    def __init__(self, subscriptions) -> None:
        self.subscriptions = list(subscriptions)
        self.applied: list[tuple[int, dict]] = []

    def is_enabled(self) -> bool:
        return True

    def list_subscriptions(self):
        return list(self.subscriptions)

    def apply_subscription(self, following_id, subscription, *, now=None):
        self.applied.append((following_id, dict(subscription)))
        return True


class FakeMetadataSearchService:
    """按标题返回预置候选;无水合/详情能力,保持 add_candidate 走最短路径。"""

    def __init__(self, results_by_title: dict[str, list] | None = None) -> None:
        self.results_by_title = results_by_title or {}
        self.search_queries: list[tuple[str, str]] = []

    def search(self, query, provider_filter: str = "", *, cache_only: bool = False):
        self.search_queries.append((str(query.title), provider_filter))
        items = list(self.results_by_title.get(str(query.title), []))
        return [MetadataScrapeGroup(provider="tmdb", provider_label="TMDB", items=items)]


def _controller(tmp_path: Path, *, backend_service, metadata_service=None):
    from atv_player.controllers.following_controller import FollowingController

    return FollowingController(
        FollowingRepository(tmp_path / "app.db"),
        metadata_search_service=metadata_service if metadata_service is not None else FakeMetadataSearchService(),
        backend_sync_service=backend_service,
    )


def test_import_backend_subscriptions_creates_record_and_binds(tmp_path: Path) -> None:
    backend = FakeBackendSyncService([_sub(id=21, name="末日地堡", metaId="tv:888", season=3, currentEpisodes=7)])
    controller = _controller(tmp_path, backend_service=backend)
    events: list[tuple[int, int, str]] = []

    result = controller.import_backend_subscriptions(progress_callback=lambda *args: events.append(args))

    assert result.total == 1 and result.imported == 1 and result.skipped == 0 and not result.cancelled
    assert events[-1] == (1, 1, "导入完成")
    record = controller._repository.get_by_identity("tmdb", "tv:888")
    assert record is not None
    assert record.title == "末日地堡" and record.season_number == 3
    assert record.external_ids.get("tmdb") == "888"
    assert [item[0] for item in backend.applied] == [record.id]  # 导入即写入 msub 绑定信号
    # 导入后的订阅在第二轮导入中判为已存在
    second = controller.import_backend_subscriptions()
    assert second.total == 0 and second.imported == 0


def test_import_backend_subscriptions_dedupes_same_show_and_skips_existing(tmp_path: Path) -> None:
    controller = _controller(tmp_path, backend_service=FakeBackendSyncService([]))
    controller._repository.upsert(_record())  # tmdb 456 季 1 已存在
    backend = FakeBackendSyncService([
        _sub(),  # 已存在 → 不进入待导入
        _sub(id=30, doubanId=0, name="末日地堡S3", metaId="888", season=3),
        _sub(id=31, doubanId=0, name="末日地堡垒", metaId="888", season=3),  # 同一部剧 → 去重合并
    ])
    controller._backend_sync_service = backend

    result = controller.import_backend_subscriptions()

    assert result.total == 2 and result.imported == 1 and result.skipped == 1
    assert result.skips[0][0] == "末日地堡垒"
    assert len(backend.applied) == 1


def test_import_backend_subscriptions_title_search_fallback(tmp_path: Path) -> None:
    metadata = FakeMetadataSearchService({
        "凡人修仙传": [MetadataScrapeCandidate(
            provider="tmdb", provider_label="TMDB", provider_id="tv:999", title="凡人修仙传", year="2020",
        )],
    })
    backend = FakeBackendSyncService([_sub(id=40, metaProvider="", metaId="", doubanId=0, name="凡人修仙传", season=2)])
    controller = _controller(tmp_path, backend_service=backend, metadata_service=metadata)

    result = controller.import_backend_subscriptions()

    assert result.imported == 1
    record = controller._repository.get_by_identity("tmdb", "tv:999")
    assert record is not None and record.season_number == 2
    assert ("凡人修仙传", "tmdb") in metadata.search_queries


def test_import_backend_subscriptions_title_search_no_result_skips(tmp_path: Path) -> None:
    backend = FakeBackendSyncService([_sub(id=41, metaProvider="", metaId="", doubanId=0, name="查无此剧")])
    controller = _controller(tmp_path, backend_service=backend)

    result = controller.import_backend_subscriptions()

    assert result.imported == 0 and result.skipped == 1
    assert result.failures == [] and result.skips[0][0] == "查无此剧"


def test_import_backend_subscriptions_cancel_and_failure(tmp_path: Path) -> None:
    backend = FakeBackendSyncService([
        _sub(id=50, name="剧甲", metaId="501", currentEpisodes=0),
        _sub(id=51, name="剧乙", metaId="502", currentEpisodes=0),
    ])
    controller = _controller(tmp_path, backend_service=backend)

    cancelled = controller.import_backend_subscriptions(cancel_callback=lambda: True)
    assert cancelled.cancelled is True and cancelled.imported == 0

    def boom(_candidate, **_kwargs):
        raise RuntimeError("元数据失败")

    controller.add_candidate = boom
    failed = controller.import_backend_subscriptions()
    assert failed.imported == 0 and len(failed.failures) == 2
    assert failed.summary_text.startswith("服务端待导入 2 条，新增 0 条，失败 2 条")


def test_import_requires_backend_service(tmp_path: Path) -> None:
    from atv_player.controllers.following_controller import FollowingController

    controller = FollowingController(FollowingRepository(tmp_path / "app.db"), metadata_search_service=FakeMetadataSearchService())
    assert controller.backend_sync_available() is False
    with pytest.raises(RuntimeError):
        controller.import_backend_subscriptions()
