# ruff: noqa: E501
import threading
import time
from dataclasses import replace
from pathlib import Path

from atv_player.following_models import (
    FollowingDetailSnapshot,
    FollowingEpisode,
    FollowingRecord,
    FollowingSeason,
)
from atv_player.following_repository import FollowingRepository
from atv_player.following_update_service import FollowingUpdateService


class FakeMetadataGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.failures: set[str] = set()
        self.responses: dict[str, tuple[FollowingRecord, FollowingDetailSnapshot]] = {}

    def refresh(self, record: FollowingRecord, provider: str):
        self.calls.append((record.title, provider))
        if provider in self.failures:
            raise RuntimeError(f"{provider} failed")
        if provider in self.responses:
            return self.responses[provider]
        return (
            replace(
                record,
                provider_id="subject:1:detail",
                poster="poster",
                backdrop="backdrop",
                rating="8.0",
                latest_episode=2,
                total_episodes=2,
            ),
            FollowingDetailSnapshot(
                following_id=record.id,
                overview="简介",
                episodes=[
                    FollowingEpisode(episode_number=1, title="第一集"),
                    FollowingEpisode(episode_number=2, title="第二集"),
                ],
                refreshed_at=200,
            ),
        )


def _record(**overrides):
    values = dict(
        id=0,
        title="凡人修仙传",
        media_kind="anime",
        provider="bangumi",
        provider_id="subject:1",
        provider_priority=["bangumi", "tmdb", "douban"],
        external_ids={"bangumi": "1"},
        latest_episode=1,
        previous_latest_episode=1,
        total_episodes=1,
        current_season_number=1,
        current_episode=1,
        watched_latest_episode=True,
        next_check_after=0,
        created_at=1,
        updated_at=1,
    )
    values.update(overrides)
    return FollowingRecord(**values)


def test_update_service_sets_homepage_prompt_when_caught_up(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(_record())
    gateway = FakeMetadataGateway()
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 200)

    results = service.check_due_records(limit=10)

    record = repo.get(record_id)
    assert results[0].has_update is True
    assert record is not None
    assert record.latest_episode == 2
    assert record.has_update is True
    assert record.homepage_prompt_pending is True
    assert record.poster == "poster"
    assert record.provider_id == "subject:1:detail"
    assert repo.get_detail_snapshot(record_id).overview == "简介"


def test_update_service_does_not_reprompt_after_homepage_prompt_is_cleared(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(_record())
    now = 200
    service = FollowingUpdateService(repo, metadata_gateway=FakeMetadataGateway(), now=lambda: now)

    service.check_record(record_id)
    repo.clear_homepage_prompt(record_id)
    now = 300
    service.check_record(record_id)

    record = repo.get(record_id)
    assert record is not None
    assert record.has_update is True
    assert record.homepage_prompt_pending is False


def test_update_service_does_not_prompt_after_dismiss_until_new_episode(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(_record(prompt_dismissed_latest_episode=2))
    service = FollowingUpdateService(repo, metadata_gateway=FakeMetadataGateway(), now=lambda: 200)

    service.check_record(record_id)

    record = repo.get(record_id)
    assert record is not None
    assert record.latest_episode == 2
    assert record.has_update is True
    assert record.homepage_prompt_pending is False


def test_update_service_prompts_again_after_dismissed_episode_advances(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(_record(prompt_dismissed_latest_episode=2))
    gateway = FakeMetadataGateway()
    gateway.responses["bangumi"] = (
        replace(_record(id=record_id), latest_episode=3, total_episodes=3),
        FollowingDetailSnapshot(
            following_id=record_id,
            overview="简介",
            episodes=[
                FollowingEpisode(episode_number=1, title="第一集"),
                FollowingEpisode(episode_number=2, title="第二集"),
                FollowingEpisode(episode_number=3, title="第三集"),
            ],
            refreshed_at=200,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 200)

    service.check_record(record_id)

    record = repo.get(record_id)
    assert record is not None
    assert record.latest_episode == 3
    assert record.homepage_prompt_pending is True


def test_update_service_prompts_after_dismissed_episode_resets_in_new_season(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            season_number=1,
            latest_episode=12,
            total_episodes=12,
            current_season_number=1,
            current_episode=12,
            prompt_dismissed_latest_episode=12,
        )
    )
    gateway = FakeMetadataGateway()
    gateway.responses["bangumi"] = (
        replace(_record(id=record_id), season_number=2, latest_episode=1, total_episodes=1),
        FollowingDetailSnapshot(
            following_id=record_id,
            overview="简介",
            seasons=[
                FollowingSeason(season_number=1, title="第一季", episode_count=12),
                FollowingSeason(season_number=2, title="第二季", episode_count=1),
            ],
            episodes=[FollowingEpisode(episode_number=1, title="第二季第一集", season_number=2)],
            refreshed_at=200,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 200)

    service.check_record(record_id)

    record = repo.get(record_id)
    assert record is not None
    assert record.latest_episode == 1
    assert record.season_number == 2
    assert record.homepage_prompt_pending is True


def test_update_service_does_not_prompt_when_user_is_behind(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(_record(current_episode=0, watched_latest_episode=False))
    service = FollowingUpdateService(repo, metadata_gateway=FakeMetadataGateway(), now=lambda: 200)

    service.check_due_records(limit=10)

    record = repo.get(record_id)
    assert record is not None
    assert record.has_update is True
    assert record.homepage_prompt_pending is False


def test_update_service_falls_back_to_next_provider_and_keeps_errors(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(_record())
    gateway = FakeMetadataGateway()
    gateway.failures.add("bangumi")
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 200)

    service.check_due_records(limit=10)

    record = repo.get(record_id)
    assert gateway.calls[:2] == [("凡人修仙传", "bangumi"), ("凡人修仙传", "tmdb")]
    assert record is not None
    assert record.last_error == ""
    assert record.latest_episode == 2


def test_update_service_preserves_tmdb_identity_when_tmdb_fails_and_bangumi_falls_back(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            title="盗妖行",
            media_kind="live_action",
            provider="tmdb",
            provider_id="tv:315088:season:1",
            provider_priority=["tmdb", "douban", "bangumi"],
            external_ids={"tmdb": "315088"},
            poster="tmdb-poster",
            backdrop="tmdb-backdrop",
            current_episode=29,
            latest_episode=60,
            previous_latest_episode=29,
            total_episodes=60,
            has_update=True,
            new_episode_count=31,
            watched_latest_episode=True,
        )
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            overview="tmdb-overview",
            episodes=[FollowingEpisode(episode_number=1, title="tmdb-episode-1")],
        ),
    )
    gateway = FakeMetadataGateway()
    gateway.failures.add("tmdb")
    gateway.failures.add("douban")
    gateway.responses["bangumi"] = (
        _record(
            id=record_id,
            title="盗妖行",
            media_kind="anime",
            provider="bangumi",
            provider_id="subject:315088",
            provider_priority=["bangumi", "tmdb", "douban"],
            external_ids={"bangumi": "315088"},
            poster="bangumi-poster",
            backdrop="bangumi-backdrop",
            current_episode=29,
            latest_episode=29,
            total_episodes=60,
        ),
        FollowingDetailSnapshot(
            following_id=record_id,
            overview="bangumi-overview",
            episodes=[
                FollowingEpisode(
                    episode_number=episode_number,
                    title=f"bangumi-episode-{episode_number}",
                    air_date="2026-05-21" if episode_number <= 29 else "2026-05-26",
                )
                for episode_number in range(1, 61)
            ],
            refreshed_at=1779638400,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 1779638400)

    service.check_due_records(limit=10)

    record = repo.get(record_id)
    snapshot = repo.get_detail_snapshot(record_id)
    assert gateway.calls[:3] == [("盗妖行", "tmdb"), ("盗妖行", "douban"), ("盗妖行", "bangumi")]
    assert record is not None
    assert record.provider == "tmdb"
    assert record.provider_id == "tv:315088"
    assert record.external_ids == {"tmdb": "315088"}
    assert record.media_kind == "live_action"
    assert record.poster == "tmdb-poster"
    assert record.backdrop == "tmdb-backdrop"
    assert record.latest_episode == 60
    assert record.total_episodes == 60
    assert snapshot is not None
    assert snapshot.overview == "tmdb-overview"
    assert snapshot.episodes[0].title == "tmdb-episode-1"


def test_update_service_counts_absolute_episode_updates_across_seasons(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            title="航海王",
            provider="tmdb",
            provider_id="tv:37854",
            provider_priority=["tmdb"],
            external_ids={"tmdb": "37854"},
            season_number=1,
            current_season_number=15,
            current_episode=581,
            latest_episode=581,
            total_episodes=0,
            watched_latest_episode=True,
        )
    )
    gateway = FakeMetadataGateway()
    gateway.responses["tmdb"] = (
        _record(
            id=record_id,
            title="航海王",
            provider="tmdb",
            provider_id="tv:37854:season:23",
            provider_priority=["tmdb"],
            external_ids={"tmdb": "37854"},
            season_number=23,
            latest_episode=1163,
            total_episodes=0,
        ),
        FollowingDetailSnapshot(
            following_id=record_id,
            episodes=[
                FollowingEpisode(
                    season_number=23,
                    episode_number=1163,
                    air_date="2026-05-24",
                )
            ],
            refreshed_at=200,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 200)

    service.check_record(record_id)

    record = repo.get(record_id)
    assert record is not None
    assert record.has_update is True
    assert record.new_episode_count == 582


def test_update_service_uses_tmdb_global_latest_and_series_total_without_specials(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            title="航海王",
            provider="tmdb",
            provider_id="tv:37854",
            provider_priority=["tmdb"],
            external_ids={"tmdb": "37854"},
            season_number=1,
            current_season_number=15,
            current_episode=581,
            latest_episode=581,
            total_episodes=0,
            watched_latest_episode=True,
        )
    )
    gateway = FakeMetadataGateway()
    gateway.responses["tmdb"] = (
        _record(
            id=record_id,
            title="航海王",
            provider="tmdb",
            provider_id="tv:37854:season:23",
            provider_priority=["tmdb"],
            external_ids={"tmdb": "37854"},
            season_number=23,
            latest_episode=1163,
            total_episodes=1181,
        ),
        FollowingDetailSnapshot(
            following_id=record_id,
            seasons=[
                FollowingSeason(season_number=0, episode_count=35, is_special=True),
                FollowingSeason(season_number=15, episode_count=62),
                FollowingSeason(season_number=23, episode_count=61),
            ],
            episodes=[
                FollowingEpisode(season_number=23, episode_number=61, air_date="2026-05-24"),
            ],
            next_episode=FollowingEpisode(season_number=23, episode_number=1164, air_date="2026-05-31"),
            refreshed_at=200,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 200)

    service.check_record(record_id)

    record = repo.get(record_id)
    assert record is not None
    assert record.latest_episode == 1163
    assert record.total_episodes == 1181
    assert record.has_update is True
    assert record.new_episode_count == 582


def test_update_service_counts_unwatched_local_episode_updates_across_seasons(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            title="五十公里桃花坞",
            media_kind="variety",
            provider="tmdb",
            provider_id="tv:12345",
            provider_priority=["tmdb"],
            external_ids={"tmdb": "12345"},
            season_number=1,
            current_season_number=0,
            current_episode=0,
            latest_episode=0,
            total_episodes=0,
            watched_latest_episode=False,
        )
    )
    gateway = FakeMetadataGateway()
    gateway.responses["tmdb"] = (
        _record(
            id=record_id,
            title="五十公里桃花坞",
            media_kind="variety",
            provider="tmdb",
            provider_id="tv:12345:season:6",
            provider_priority=["tmdb"],
            external_ids={"tmdb": "12345"},
            season_number=6,
            latest_episode=10,
            total_episodes=0,
        ),
        FollowingDetailSnapshot(
            following_id=record_id,
            seasons=[
                FollowingSeason(season_number=1, episode_count=60),
                FollowingSeason(season_number=2, episode_count=60),
                FollowingSeason(season_number=3, episode_count=60),
                FollowingSeason(season_number=4, episode_count=60),
                FollowingSeason(season_number=5, episode_count=60),
                FollowingSeason(season_number=6, episode_count=10),
            ],
            episodes=[
                FollowingEpisode(
                    season_number=6,
                    episode_number=10,
                    air_date="2026-05-24",
                )
            ],
            refreshed_at=200,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 200)

    service.check_record(record_id)

    record = repo.get(record_id)
    assert record is not None
    assert record.has_update is True
    assert record.new_episode_count == 310


def test_update_service_counts_unwatched_tmdb_latest_as_updates_with_incomplete_seasons(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            title="流言终结者",
            media_kind="documentary",
            provider="tmdb",
            provider_id="tv:1428",
            provider_priority=["tmdb"],
            external_ids={"tmdb": "1428"},
            season_number=1,
            current_season_number=0,
            current_episode=0,
            latest_episode=0,
            total_episodes=0,
            watched_latest_episode=False,
        )
    )
    gateway = FakeMetadataGateway()
    gateway.responses["tmdb"] = (
        _record(
            id=record_id,
            title="流言终结者",
            media_kind="documentary",
            provider="tmdb",
            provider_id="tv:1428:season:16",
            provider_priority=["tmdb"],
            external_ids={"tmdb": "1428"},
            season_number=16,
            latest_episode=8,
            total_episodes=272,
        ),
        FollowingDetailSnapshot(
            following_id=record_id,
            seasons=[FollowingSeason(season_number=16, episode_count=11)],
            episodes=[
                FollowingEpisode(
                    season_number=16,
                    episode_number=8,
                    air_date="2018-02-07",
                )
            ],
            refreshed_at=200,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 200)

    service.check_record(record_id)

    record = repo.get(record_id)
    assert record is not None
    assert record.total_episodes == 272
    assert record.has_update is True
    assert record.new_episode_count == 8


def test_update_service_prefers_tmdb_first_when_tmdb_id_exists(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            title="名侦探柯南",
            media_kind="anime",
            provider="bangumi",
            provider_id="subject:1",
            provider_priority=["bangumi", "tmdb", "douban"],
            external_ids={"bangumi": "1", "tmdb": "30983"},
            current_episode=1200,
            latest_episode=1200,
            previous_latest_episode=1200,
            total_episodes=1200,
            watched_latest_episode=True,
        )
    )
    gateway = FakeMetadataGateway()
    gateway.responses["tmdb"] = (
        _record(
            id=record_id,
            title="名侦探柯南",
            media_kind="anime",
            provider="tmdb",
            provider_id="tv:30983:season:1",
            provider_priority=["tmdb", "bangumi", "douban"],
            external_ids={"tmdb": "30983"},
            latest_episode=1201,
            total_episodes=1201,
        ),
        FollowingDetailSnapshot(
            following_id=record_id,
            episodes=[FollowingEpisode(episode_number=1201, air_date="2026-05-09")],
            refreshed_at=1779638400,
        ),
    )
    gateway.responses["bangumi"] = (
        _record(
            id=record_id,
            title="名侦探柯南",
            media_kind="anime",
            provider="bangumi",
            provider_id="subject:1",
            provider_priority=["bangumi", "tmdb", "douban"],
            external_ids={"bangumi": "1"},
            latest_episode=1200,
            total_episodes=1200,
        ),
        FollowingDetailSnapshot(
            following_id=record_id,
            episodes=[FollowingEpisode(episode_number=1200, air_date="2026-05-02")],
            refreshed_at=1779638400,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 1779638400)

    results = service.check_due_records(limit=10)

    record = repo.get(record_id)
    assert gateway.calls[0] == ("名侦探柯南", "tmdb")
    assert results[0].latest_episode == 1201
    assert record is not None
    assert record.latest_episode == 1201


def test_update_service_prefers_tmdb_record_latest_over_season_local_episode_numbers(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            title="名侦探柯南",
            media_kind="anime",
            provider="tmdb",
            provider_id="tv:30983:season:1",
            provider_priority=["tmdb", "bangumi", "douban"],
            external_ids={"tmdb": "30983"},
            current_episode=1200,
            latest_episode=1200,
            previous_latest_episode=1200,
            total_episodes=1200,
            watched_latest_episode=True,
        )
    )
    gateway = FakeMetadataGateway()
    gateway.responses["tmdb"] = (
        _record(
            id=record_id,
            title="名侦探柯南",
            media_kind="anime",
            provider="tmdb",
            provider_id="tv:30983:season:1",
            provider_priority=["tmdb", "bangumi", "douban"],
            external_ids={"tmdb": "30983"},
            latest_episode=1201,
            total_episodes=1201,
        ),
        FollowingDetailSnapshot(
            following_id=record_id,
            episodes=[
                FollowingEpisode(episode_number=1, air_date="2026-05-02"),
                FollowingEpisode(episode_number=2, air_date="2026-05-09"),
            ],
            refreshed_at=1779638400,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 1779638400)

    results = service.check_due_records(limit=10)

    record = repo.get(record_id)
    assert results[0].latest_episode == 1201
    assert results[0].has_update is True
    assert record is not None
    assert record.latest_episode == 1201
    assert record.new_episode_count == 1
    assert record.homepage_prompt_pending is True


def test_update_service_detects_cross_season_tmdb_updates(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            title="黑袍纠察队",
            media_kind="live_action",
            provider="tmdb",
            provider_id="tv:76479",
            provider_priority=["tmdb", "douban", "bangumi"],
            external_ids={"tmdb": "76479"},
            season_number=4,
            current_season_number=4,
            current_episode=8,
            latest_episode=8,
            previous_latest_episode=8,
            total_episodes=8,
            watched_latest_episode=True,
        )
    )
    gateway = FakeMetadataGateway()
    gateway.responses["tmdb"] = (
        _record(
            id=record_id,
            title="黑袍纠察队",
            media_kind="live_action",
            provider="tmdb",
            provider_id="tv:76479",
            provider_priority=["tmdb", "douban", "bangumi"],
            external_ids={"tmdb": "76479"},
            season_number=5,
            latest_episode=1,
            total_episodes=8,
        ),
        FollowingDetailSnapshot(
            following_id=record_id,
            episodes=[FollowingEpisode(episode_number=1, season_number=5, air_date="2026-05-09")],
            refreshed_at=1779638400,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 1779638400)

    results = service.check_due_records(limit=10)

    record = repo.get(record_id)
    assert results[0].latest_episode == 1
    assert results[0].has_update is True
    assert record is not None
    assert record.season_number == 5
    assert record.has_update is True
    assert record.new_episode_count == 1
    assert record.homepage_prompt_pending is True


def test_update_service_tmdb_check_keeps_existing_metadata_identity(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            title="名侦探柯南",
            media_kind="anime",
            provider="tmdb",
            provider_id="tv:30983:season:1",
            provider_priority=["tmdb", "bangumi", "douban"],
            external_ids={"tmdb": "30983"},
            poster="old-poster",
            backdrop="old-backdrop",
            rating="9.0",
            current_episode=1200,
            latest_episode=1200,
            previous_latest_episode=1200,
            total_episodes=1200,
            watched_latest_episode=True,
        )
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            overview="旧简介",
            episodes=[FollowingEpisode(episode_number=1, title="旧分集")],
        ),
    )
    gateway = FakeMetadataGateway()
    gateway.responses["tmdb"] = (
        FollowingRecord(
            id=0,
            title="",
            latest_episode=1201,
            previous_latest_episode=1201,
            total_episodes=1201,
        ),
        FollowingDetailSnapshot(),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 1779638400)

    service.check_record(record_id)

    record = repo.get(record_id)
    snapshot = repo.get_detail_snapshot(record_id)
    assert record is not None
    assert record.provider == "tmdb"
    assert record.provider_id == "tv:30983"
    assert record.external_ids == {"tmdb": "30983"}
    assert record.poster == "old-poster"
    assert record.backdrop == "old-backdrop"
    assert record.rating == "9.0"
    assert record.latest_episode == 1201
    assert record.total_episodes == 1201
    assert snapshot is not None
    assert snapshot.overview == "旧简介"
    assert snapshot.episodes[0].title == "旧分集"


def test_update_service_does_not_demote_long_running_tmdb_anime_to_bangumi_season(tmp_path: Path) -> None:
    """Conan: 1200 TMDB episodes must not be overwritten by a 100-episode Bangumi season."""
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            title="名侦探柯南",
            media_kind="anime",
            provider="tmdb",
            provider_id="tv:30983:season:1",
            provider_priority=["tmdb", "bangumi", "douban"],
            external_ids={"tmdb": "30983"},
            poster="tmdb-poster",
            current_episode=1100,
            latest_episode=1200,
            previous_latest_episode=1100,
            total_episodes=1200,
            watched_latest_episode=False,
        )
    )
    gateway = FakeMetadataGateway()
    gateway.failures.add("tmdb")
    gateway.responses["bangumi"] = (
        _record(
            id=record_id,
            title="名侦探柯南 某一季",
            media_kind="anime",
            provider="bangumi",
            provider_id="subject:999",
            provider_priority=["bangumi", "tmdb", "douban"],
            external_ids={"bangumi": "999"},
            poster="bangumi-poster",
            latest_episode=100,
            total_episodes=100,
        ),
        FollowingDetailSnapshot(
            following_id=record_id,
            episodes=[
                FollowingEpisode(episode_number=n, air_date="2026-05-01")
                for n in range(1, 101)
            ],
            refreshed_at=1779638400,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 1779638400)

    service.check_due_records(limit=10)

    record = repo.get(record_id)
    assert record is not None
    assert record.provider == "tmdb"
    assert record.provider_id == "tv:30983"
    assert record.external_ids == {"tmdb": "30983"}
    assert record.poster == "tmdb-poster"
    assert record.title == "名侦探柯南"
    assert record.latest_episode == 1200
    assert record.total_episodes == 1200


def test_update_service_keeps_live_action_kind_when_bangumi_falls_back(tmp_path: Path) -> None:
    """The Boys: live_action TV must not be reclassified as anime by Bangumi fallback."""
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            title="黑袍纠察队",
            media_kind="live_action",
            provider="tmdb",
            provider_id="tv:76479",
            provider_priority=["tmdb", "douban", "bangumi"],
            external_ids={"tmdb": "76479"},
            season_number=4,
            current_season_number=4,
            current_episode=8,
            latest_episode=8,
            previous_latest_episode=8,
            total_episodes=8,
            watched_latest_episode=True,
        )
    )
    gateway = FakeMetadataGateway()
    gateway.failures.add("tmdb")
    gateway.failures.add("douban")
    gateway.responses["bangumi"] = (
        _record(
            id=record_id,
            title="黑袍纠察队",
            media_kind="anime",
            provider="bangumi",
            provider_id="subject:42",
            provider_priority=["bangumi", "tmdb", "douban"],
            external_ids={"bangumi": "42"},
            latest_episode=8,
            total_episodes=8,
        ),
        FollowingDetailSnapshot(
            following_id=record_id,
            episodes=[FollowingEpisode(episode_number=n, air_date="2026-05-01") for n in range(1, 9)],
            refreshed_at=1779638400,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 1779638400)

    service.check_due_records(limit=10)

    record = repo.get(record_id)
    assert record is not None
    assert record.provider == "tmdb"
    assert record.provider_id == "tv:76479"
    assert record.external_ids == {"tmdb": "76479"}
    assert record.media_kind == "live_action"


def test_update_service_reports_completion_state_from_snapshot(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(_record())
    gateway = FakeMetadataGateway()
    gateway.responses["bangumi"] = (
        _record(id=record_id),
        FollowingDetailSnapshot(
            following_id=record_id,
            episodes=[FollowingEpisode(episode_number=1, air_date="2026-05-19")],
            refreshed_at=1779638400,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 1779638400)

    results = service.check_due_records(limit=10)

    assert results[0].completion_state == "completed"


def test_update_service_does_not_prompt_completed_watched_series_for_future_unreleased_season(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        _record(
            title="剑来",
            provider="tmdb",
            provider_id="tv:240411",
            provider_priority=["tmdb"],
            external_ids={"tmdb": "240411"},
            season_number=1,
            current_season_number=1,
            current_episode=26,
            latest_episode=26,
            previous_latest_episode=26,
            total_episodes=26,
            watched_latest_episode=True,
        )
    )
    gateway = FakeMetadataGateway()
    gateway.responses["tmdb"] = (
        _record(
            id=record_id,
            title="剑来",
            provider="tmdb",
            provider_id="tv:240411",
            provider_priority=["tmdb"],
            external_ids={"tmdb": "240411"},
            season_number=1,
            latest_episode=26,
            total_episodes=26,
        ),
        FollowingDetailSnapshot(
            following_id=record_id,
            seasons=[
                FollowingSeason(season_number=1, episode_count=26),
                FollowingSeason(season_number=2, episode_count=26),
            ],
            episodes=[
                FollowingEpisode(season_number=1, episode_number=26, air_date="2025-02-15"),
                FollowingEpisode(season_number=2, episode_number=1, air_date="2026-12-01"),
            ],
            refreshed_at=1779638400,
        ),
    )
    service = FollowingUpdateService(repo, metadata_gateway=gateway, now=lambda: 1779638400)

    results = service.check_record(record_id)

    record = repo.get(record_id)
    assert results is not None
    assert results.has_update is False
    assert results.homepage_prompt_pending is False
    assert record is not None
    assert record.has_update is False
    assert record.homepage_prompt_pending is False


def test_update_service_timer_check_runs_gateway_off_main_thread(qtbot, tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    repo.upsert(_record())
    refresh_threads: list[int] = []
    release = threading.Event()

    class SlowGateway(FakeMetadataGateway):
        def refresh(self, record: FollowingRecord, provider: str):
            refresh_threads.append(threading.get_ident())
            release.wait(10)
            return super().refresh(record, provider)

    service = FollowingUpdateService(repo, metadata_gateway=SlowGateway(), now=lambda: 200)

    finished: list[object] = []
    service.update_finished.connect(finished.append)

    service._on_check_timer()
    qtbot.waitUntil(lambda: bool(refresh_threads), timeout=5000)
    assert threading.get_ident() not in refresh_threads

    release.set()
    qtbot.waitUntil(lambda: not service._async_check_running, timeout=5000)
    qtbot.waitUntil(lambda: bool(finished), timeout=5000)

    record = repo.get(1)
    assert record is not None
    assert record.last_error == ""
    assert finished


def test_update_service_skips_async_check_while_previous_round_in_flight(qtbot, tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    repo.upsert(_record())
    calls = {"count": 0}
    release = threading.Event()

    class SlowGateway(FakeMetadataGateway):
        def refresh(self, record: FollowingRecord, provider: str):
            calls["count"] += 1
            release.wait(10)
            return super().refresh(record, provider)

    service = FollowingUpdateService(repo, metadata_gateway=SlowGateway(), now=lambda: 200)

    service._start_async_check()
    qtbot.waitUntil(lambda: calls["count"] == 1, timeout=5000)
    service._start_async_check()  # 上一轮未结束:不得重复发起
    time.sleep(0.1)
    assert calls["count"] == 1

    release.set()
    qtbot.waitUntil(lambda: not service._async_check_running, timeout=5000)


def test_update_service_async_failure_clears_in_flight_and_emits_nothing(qtbot, tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    repo.upsert(_record())

    class ExplodingGateway(FakeMetadataGateway):
        def refresh(self, record: FollowingRecord, provider: str):
            raise RuntimeError("network down")

    service = FollowingUpdateService(repo, metadata_gateway=ExplodingGateway(), now=lambda: 200)

    finished: list[object] = []
    service.update_finished.connect(finished.append)

    service._start_async_check()
    qtbot.waitUntil(lambda: not service._async_check_running, timeout=5000)
    # provider 异常被 _check_one 吞掉转 checked=False 结果,不外抛、不清空 in_flight 死锁
    assert len(finished) == 1
    assert finished[0][0].checked is False
    assert service._async_check_running is False
