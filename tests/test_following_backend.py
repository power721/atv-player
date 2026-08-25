# ruff: noqa: E501
"""服务端追剧信号同步(FollowingBackendSyncService)单测:匹配优先级/负缓存/自动订阅/信号并入。"""
from pathlib import Path

from atv_player.following_backend import (
    FollowingBackendSyncService,
    _SubscriptionIndex,
    _tmdb_series_id,
)
from atv_player.following_models import FollowingRecord
from atv_player.following_repository import FollowingRepository


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

    def list_media_subscriptions(self):
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
