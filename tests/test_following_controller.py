# ruff: noqa: E501
from datetime import datetime
from pathlib import Path

from atv_player.ai.enrichment import FollowingDetailSummary
from atv_player.controllers.following_controller import FollowingController
from atv_player.favorite_tmdb_bindings import FavoriteTMDBBindingRepository
from atv_player.following_models import (
    FollowingDetailSnapshot,
    FollowingEpisode,
    FollowingMetadataBundle,
    FollowingMetadataSourceSnapshot,
    FollowingPlaybackPlatformEntry,
    FollowingRecord,
    FollowingSeason,
    FollowingSourceBinding,
)
from atv_player.following_repository import FollowingRepository
from atv_player.metadata.discovery import DiscoveryItem, DiscoveryResult
from atv_player.metadata.models import MetadataRecord
from atv_player.metadata.scrape import MetadataScrapeCandidate, MetadataScrapeGroup
from atv_player.models import PlaybackDetailField, PlayItem, VodItem


class FakeSearchService:
    def search(self, query, provider_filter=""):
        del provider_filter
        return [
            MetadataScrapeGroup(
                provider="bangumi",
                provider_label="Bangumi",
                items=[
                    MetadataScrapeCandidate(
                        provider="bangumi",
                        provider_label="Bangumi",
                        provider_id="subject:1",
                        title=query.title,
                        subtitle="动漫",
                        raw={"episodes": [{"sort": 1, "type": 0, "name": "第一话"}]},
                    )
                ],
            )
        ]


class FakeDetailSearchService(FakeSearchService):
    def __init__(self) -> None:
        self.detail_provider_ids: list[str] = []

    def detail_record(self, candidate):
        assert candidate.provider == "tmdb"
        self.detail_provider_ids.append(candidate.provider_id)
        return MetadataRecord(
            provider="tmdb",
            provider_id=candidate.provider_id,
            title="庆余年",
            poster="poster",
            backdrop="backdrop",
            overview="详情简介",
            rating="8.0",
            actors=["张若昀"],
            directors=["孙皓"],
            detail_fields=[
                {
                    "label": "episodes",
                    "value": [
                        {"episode_number": 1, "name": "第一集", "overview": "剧情", "still_url": "still"},
                        {"episode_number": 2, "name": "第二集"},
                    ],
                }
            ],
        )


class FakeFollowingMetadataRefreshService:
    def __init__(self) -> None:
        self.detail_provider_ids: list[tuple[str, str]] = []

    def search(self, query, provider_filter=""):
        assert query.title == "仙逆"
        if provider_filter == "tmdb":
            return [
                MetadataScrapeGroup(
                    provider="tmdb",
                    provider_label="TMDB",
                    items=[
                        MetadataScrapeCandidate(
                            provider="tmdb",
                            provider_label="TMDB",
                            provider_id="tv:236534:season:1",
                            title="仙逆",
                            subtitle="剧集",
                        )
                    ],
                )
            ]
        if provider_filter == "bangumi":
            return [
                MetadataScrapeGroup(
                    provider="bangumi",
                    provider_label="Bangumi",
                    items=[
                        MetadataScrapeCandidate(
                            provider="bangumi",
                            provider_label="Bangumi",
                            provider_id="subject:1",
                            title="仙逆",
                            subtitle="动漫",
                        )
                    ],
                )
            ]
        return []

    def detail_record(self, candidate):
        self.detail_provider_ids.append((candidate.provider, candidate.provider_id))
        if candidate.provider == "bangumi":
            return MetadataRecord(
                provider="bangumi",
                provider_id="subject:1",
                title="仙逆",
                overview="Bangumi简介",
                rating="8.4",
                aliases=["仙逆动画"],
            )
        return MetadataRecord(
            provider="tmdb",
            provider_id="tv:236534:season:1",
            title="仙逆",
            poster="tmdb-poster",
            backdrop="tmdb-backdrop",
            overview="TMDB简介",
            rating="7.6",
            tmdb_id="236534",
            actors=["史泽鲲"],
            directors=["导演"],
            genres=["动画"],
            detail_fields=[
                {
                    "label": "episodes",
                    "value": [
                        {
                            "episode_number": 1,
                            "name": "TMDB第1集",
                            "overview": "TMDB剧情",
                            "still_url": "tmdb-still",
                        }
                    ],
                }
            ],
        )


class FakeTMDBIdRefreshService:
    def __init__(self) -> None:
        self.search_calls = 0
        self.search_provider_filters: list[str] = []
        self.full_detail_provider_ids: list[str] = []

    def search(self, query, provider_filter=""):
        del query
        self.search_calls += 1
        self.search_provider_filters.append(provider_filter)
        return []

    def detail_record_full(self, candidate):
        self.full_detail_provider_ids.append(candidate.provider_id)
        return MetadataRecord(
            provider="tmdb",
            provider_id=candidate.provider_id,
            title="低智商犯罪",
            poster="tmdb-poster",
            backdrop="tmdb-backdrop",
            overview="TMDB简介",
            tmdb_id="272432",
            actors=["王骁", "田曦薇"],
            cast_details=[
                {"name": "王骁", "role": "Zhang Yi'ang", "avatar": "/wang.jpg"},
                {"name": "田曦薇", "role": "Li Qian", "avatar": "/tian.jpg"},
            ],
        )


class FakeTMDBUrlSearchService:
    def __init__(self) -> None:
        self.detail_provider_ids: list[str] = []

    def detail_record(self, candidate):
        self.detail_provider_ids.append(candidate.provider_id)
        return MetadataRecord(
            provider="tmdb",
            provider_id=candidate.provider_id,
            title="名侦探柯南",
            year="1996",
            tmdb_id="30983",
            overview="高中生侦探化身小学生继续破案。",
        )


class FakeTMDBFollowingSearchService:
    def __init__(self) -> None:
        self.search_following_calls: list[tuple[str, str, str]] = []

    def search_following(self, query, provider_filter=""):
        self.search_following_calls.append((query.title, provider_filter, str(query.year or "")))
        return [
            MetadataScrapeGroup(
                provider="tmdb",
                provider_label="TMDB",
                items=[
                    MetadataScrapeCandidate(
                        provider="tmdb",
                        provider_label="TMDB",
                        provider_id="movie:12",
                        title="Movie First",
                        year="2024",
                        subtitle="电影",
                    ),
                    MetadataScrapeCandidate(
                        provider="tmdb",
                        provider_label="TMDB",
                        provider_id="tv:34:season:1",
                        title="TV Second",
                        year="2025",
                        subtitle="剧集",
                    ),
                ],
            )
        ]


class FakeUpdateService:
    def __init__(self) -> None:
        self.manual_checks: list[int] = []
        self.due_checks = 0

    def check_record(self, record_id: int):
        self.manual_checks.append(record_id)
        return None

    def check_due_records(self, limit: int = 3):
        del limit
        self.due_checks += 1
        return []


class SavingUpdateService:
    def __init__(self, repo: FollowingRepository) -> None:
        self.repo = repo
        self.manual_checks: list[int] = []

    def check_record(self, record_id: int):
        self.manual_checks.append(record_id)
        snapshot = FollowingDetailSnapshot(
            following_id=record_id,
            overview="刷新后的简介",
            episodes=[FollowingEpisode(episode_number=1, title="刷新分集")],
        )
        self.repo.save_detail_snapshot(record_id, snapshot)
        self.repo.update_check_state(
            record_id,
            latest_episode=1,
            total_episodes=1,
            checked_at=200,
            next_check_after=300,
            has_update=False,
            new_episode_count=0,
            homepage_prompt_pending=False,
            last_error="",
        )
        return None


def test_following_controller_searches_and_adds_candidate(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)

    groups = controller.search_media("凡人修仙传")
    record = controller.add_candidate(groups[0].items[0])

    assert groups[0].provider == "bangumi"
    assert record.title == "凡人修仙传"
    assert repo.get(record.id) is not None
    assert repo.get_detail_snapshot(record.id).episodes[0].title == "第一话"


def test_following_controller_adds_candidate_with_manual_current_episode(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)

    record = controller.add_candidate(controller.search_media("凡人修仙传")[0].items[0], current_episode=1)

    loaded = repo.get(record.id)
    assert loaded is not None
    assert loaded.current_episode == 1
    assert loaded.watched_latest_episode is True
    assert loaded.has_update is False


def test_following_controller_preserves_existing_progress_when_adding_duplicate_candidate(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)

    candidate = controller.search_media("凡人修仙传")[0].items[0]
    first = controller.add_candidate(candidate)
    controller.record_playback_progress(first.id, current_episode=1, position_seconds=42)

    second = controller.add_candidate(candidate)

    loaded = repo.get(first.id)
    assert second.id == first.id
    assert loaded is not None
    assert loaded.current_episode == 1
    assert loaded.position_seconds == 42
    assert loaded.watched_latest_episode is True
    assert loaded.has_update is False


def test_following_controller_adds_candidate_with_detail_snapshot(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    service = FakeDetailSearchService()
    controller = FollowingController(repo, metadata_search_service=service, now=lambda: 100)
    candidate = MetadataScrapeCandidate(
        provider="tmdb",
        provider_label="TMDB",
        provider_id="tv:456",
        title="庆余年",
        subtitle="剧集",
    )

    record = controller.add_candidate(candidate)
    snapshot = repo.get_detail_snapshot(record.id)

    assert record.poster == "poster"
    assert record.backdrop == "backdrop"
    assert record.provider_id == "tv:456"
    assert record.season_number == 1
    assert record.latest_episode == 2
    assert record.total_episodes == 2
    assert service.detail_provider_ids == ["tv:456:season:1"]
    assert snapshot is not None
    assert snapshot.overview == "详情简介"
    assert snapshot.episodes[0].title == "第一集"
    assert snapshot.cast[0]["name"] == "张若昀"


def test_following_controller_record_playback_progress_ignores_older_episode_reports(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    following_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="凡人修仙传",
            media_kind="anime",
            provider="bangumi",
            provider_id="subject:1",
            provider_priority=["bangumi", "tmdb", "douban"],
            external_ids={"bangumi": "1"},
            current_season_number=1,
            current_episode=12,
            position_seconds=50,
            latest_episode=24,
            previous_latest_episode=24,
            total_episodes=24,
            created_at=1,
            updated_at=1,
        )
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), now=lambda: 100)

    controller.record_playback_progress(
        following_id,
        current_season_number=1,
        current_episode=10,
        position_seconds=80,
    )

    loaded = repo.get(following_id)
    assert loaded is not None
    assert loaded.current_episode == 12
    assert loaded.position_seconds == 50


def test_following_controller_manual_progress_allows_older_episode(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    following_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="成何体统 第二季",
            media_kind="anime",
            provider="tmdb",
            provider_id="tv:256783",
            season_number=2,
            current_season_number=2,
            current_episode=112,
            position_seconds=50,
            latest_episode=24,
            total_episodes=24,
            created_at=1,
            updated_at=1,
        )
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), now=lambda: 100)

    controller.record_playback_progress(
        following_id,
        current_season_number=2,
        current_episode=12,
        position_seconds=0,
        allow_regression=True,
    )

    loaded = repo.get(following_id)
    assert loaded is not None
    assert loaded.current_season_number == 2
    assert loaded.current_episode == 12
    assert loaded.position_seconds == 0


def test_following_controller_hydrates_tmdb_url_candidate_for_search_results(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    service = FakeTMDBUrlSearchService()
    controller = FollowingController(
        repo,
        metadata_search_service=service,
        now=lambda: 100,
    )

    groups = controller.search_media("https://www.themoviedb.org/tv/30983-case-closed")

    assert len(groups) == 1
    assert groups[0].provider == "tmdb"
    assert service.detail_provider_ids == ["tv:30983:season:1"]
    candidate = groups[0].items[0]
    assert candidate.provider_id == "tv:30983:season:1"
    assert candidate.title == "名侦探柯南"
    assert candidate.year == "1996"
    assert candidate.subtitle == "剧集"


def test_following_controller_load_page_uses_completed_text_for_idle_record(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    following_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="凡人修仙传",
            media_kind="anime",
            provider="bangumi",
            provider_id="subject:1",
            provider_priority=["bangumi", "tmdb", "douban"],
            external_ids={"bangumi": "1"},
            current_season_number=1,
            current_episode=24,
            latest_episode=24,
            previous_latest_episode=24,
            total_episodes=24,
            watched_latest_episode=True,
            created_at=1,
            updated_at=1,
        )
    )
    repo.save_detail_snapshot(
        following_id,
        FollowingDetailSnapshot(
            following_id=following_id,
            episodes=[FollowingEpisode(episode_number=24, air_date="2026-05-19")],
            refreshed_at=1779638400,
        ),
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), now=lambda: 1779638400)

    cards, _total = controller.load_page(page=1, size=20, keyword="", only_updates=False)

    assert cards[0].update_text == "已完结"


def test_following_controller_load_page_sorts_by_next_update_and_keeps_completed_last(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    completed_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="已完结",
            media_kind="anime",
            provider="bangumi",
            provider_id="subject:completed",
            provider_priority=["bangumi"],
            latest_episode=12,
            total_episodes=12,
            created_at=1,
            updated_at=400,
        )
    )
    later_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="稍后更新",
            media_kind="anime",
            provider="bangumi",
            provider_id="subject:later",
            provider_priority=["bangumi"],
            latest_episode=8,
            total_episodes=0,
            created_at=2,
            updated_at=300,
        )
    )
    sooner_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="更早更新",
            media_kind="anime",
            provider="bangumi",
            provider_id="subject:sooner",
            provider_priority=["bangumi"],
            latest_episode=8,
            total_episodes=0,
            created_at=3,
            updated_at=200,
        )
    )
    repo.save_detail_snapshot(
        completed_id,
        FollowingDetailSnapshot(
            following_id=completed_id,
            episodes=[FollowingEpisode(episode_number=12, air_date="2026-05-20")],
        ),
    )
    repo.save_detail_snapshot(
        later_id,
        FollowingDetailSnapshot(
            following_id=later_id,
            episodes=[FollowingEpisode(episode_number=9, air_date="2026-06-03")],
        ),
    )
    repo.save_detail_snapshot(
        sooner_id,
        FollowingDetailSnapshot(
            following_id=sooner_id,
            episodes=[FollowingEpisode(episode_number=9, air_date="2026-05-31")],
        ),
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), now=lambda: 1779638400)

    cards, total = controller.load_page(page=1, size=20, keyword="", only_updates=False)

    assert total == 3
    assert [card.display_title for card in cards] == ["更早更新", "稍后更新", "已完结"]


def test_following_controller_load_page_prefers_platform_weekly_update_time(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    tmdb_only_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="TMDB日期",
            media_kind="anime",
            provider="tmdb",
            provider_id="tv:tmdb",
            provider_priority=["tmdb"],
            created_at=1,
            updated_at=2,
        )
    )
    platform_time_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="平台具体时间",
            media_kind="anime",
            provider="bilibili",
            provider_id="ss1",
            provider_priority=["bilibili"],
            created_at=2,
            updated_at=1,
        )
    )
    repo.save_detail_snapshot(
        tmdb_only_id,
        FollowingDetailSnapshot(
            following_id=tmdb_only_id,
            episodes=[FollowingEpisode(episode_number=9, air_date="2026-06-01")],
        ),
    )
    repo.save_detail_snapshot(
        platform_time_id,
        FollowingDetailSnapshot(
            following_id=platform_time_id,
            episodes=[FollowingEpisode(episode_number=9, air_date="2026-06-01")],
            metadata_bundle=FollowingMetadataBundle(
                merged_snapshot=FollowingMetadataSourceSnapshot(
                    source_key="merged",
                    provider="merged",
                    provider_label="合并",
                    playback_platforms=[
                        FollowingPlaybackPlatformEntry(
                            provider="bilibili",
                            label="哔哩哔哩",
                            update_time_text="连载中, 每周日 11:00更新",
                        )
                    ],
                )
            ),
        ),
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), now=lambda: 1780070400)

    cards, total = controller.load_page(page=1, size=20, keyword="", only_updates=False)

    assert total == 2
    assert [card.display_title for card in cards] == ["平台具体时间", "TMDB日期"]


def test_following_controller_parses_multiple_platform_weekdays() -> None:
    parsed = FollowingController._parse_update_time_text(
        "每周三周四 20:00更新",
        now=datetime(2026, 5, 30, 0, 0),
    )

    assert parsed == (datetime(2026, 6, 3).date().toordinal(), 20 * 60)


def test_following_controller_keyword_search_uses_tmdb_only_and_sorts_tv_before_movie(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    service = FakeTMDBFollowingSearchService()
    controller = FollowingController(repo, metadata_search_service=service)

    groups = controller.search_media("星际")

    assert groups[0].provider == "tmdb"
    assert [item.provider_id for item in groups[0].items] == ["tv:34:season:1", "movie:12"]
    assert service.search_following_calls == [("星际", "tmdb", "")]


def test_following_controller_keyword_search_forwards_year_to_tmdb_search(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    service = FakeTMDBFollowingSearchService()
    controller = FollowingController(repo, metadata_search_service=service)

    controller.search_media("星际", year="2024")

    assert service.search_following_calls == [("星际", "tmdb", "2024")]


def test_following_controller_load_discovery_search_forwards_year_filter(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    service = FakeTMDBFollowingSearchService()
    controller = FollowingController(
        repo,
        metadata_search_service=service,
        discovery_service=type("DiscoveryService", (), {})(),
    )

    controller.load_discovery_tab("search", query="星际", filters={"year": "2024"})

    assert service.search_following_calls == [("星际", "tmdb", "2024")]


def test_following_controller_tmdb_url_search_ignores_year_parameter(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    service = FakeTMDBUrlSearchService()
    controller = FollowingController(repo, metadata_search_service=service, now=lambda: 100)

    groups = controller.search_media("https://www.themoviedb.org/tv/30983-case-closed", year="2024")

    assert len(groups) == 1
    assert service.detail_provider_ids == ["tv:30983:season:1"]


def test_following_controller_non_tmdb_url_passthrough_still_works(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    controller = FollowingController(repo, metadata_search_service=FakeSearchService())

    groups = controller.search_media("https://bgm.tv/subject/123")

    assert len(groups) == 1
    assert groups[0].provider == "bangumi"
    assert groups[0].items[0].provider_id == "subject:123"


def test_following_controller_douban_url_passthrough_still_works(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    controller = FollowingController(repo, metadata_search_service=FakeSearchService())

    groups = controller.search_media("https://movie.douban.com/subject/1292052/")

    assert len(groups) == 1
    assert groups[0].provider in {"official_douban", "local_douban", "douban"}
    assert groups[0].items[0].provider_id == "1292052"


def test_following_controller_builds_card_and_detail_models(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    following_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="凡人修仙传",
            season_number=5,
            provider="bangumi",
            provider_id="subject:1",
            provider_priority=["bangumi"],
            current_season_number=2,
            current_episode=3,
            latest_episode=8,
            total_episodes=8,
            has_update=True,
            new_episode_count=1,
        )
    )
    repo.save_detail_snapshot(
        following_id,
        FollowingDetailSnapshot(
            following_id=following_id,
            overview="简介",
            episodes=[FollowingEpisode(episode_number=128, title="新章")],
        ),
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)

    cards, total = controller.load_page(page=1, size=20, keyword="", only_updates=True)
    detail = controller.load_detail(following_id)

    assert total == 1
    assert cards[0].progress_text == "看到 S2E3 · 最新 S5E8 / 总 8"
    assert cards[0].updated_hint is True
    assert detail.snapshot.overview == "简介"


def test_following_controller_loads_recommendations_and_falls_back_to_trending_when_empty(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    repo.upsert(
        FollowingRecord(
            id=0,
            title="黑袍纠察队",
            media_kind="live_action",
            provider="tmdb",
            provider_id="tv:76479",
            external_ids={"tmdb": "76479"},
            has_update=True,
            updated_at=100,
        )
    )

    class DiscoveryService:
        def recommend(self, **_kwargs):
            return DiscoveryResult(items=[], total=0, source_label="推荐", fallback_reason="")

        def trending(self, query):
            assert query.kind == "trending"
            return DiscoveryResult(
                items=[
                    DiscoveryItem(
                        provider="tmdb",
                        provider_id="tv:100",
                        tmdb_id="100",
                        media_type="tv",
                        title="Gen V",
                        source_label="本周趋势",
                    )
                ],
                total=1,
                source_label="本周趋势",
            )

    controller = FollowingController(
        repo,
        metadata_search_service=FakeSearchService(),
        discovery_service=DiscoveryService(),
        favorite_tmdb_binding_repository=FavoriteTMDBBindingRepository(tmp_path / "app.db"),
    )

    result = controller.load_discovery_tab("recommendation")

    assert result.items[0].provider_id == "tv:100"
    assert result.fallback_reason == "recommendation-empty"


def test_following_controller_reuses_discovery_results_across_dialog_reopens(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")

    class DiscoveryService:
        def __init__(self) -> None:
            self.trending_calls = 0
            self.recommend_calls = 0

        def trending(self, query):
            self.trending_calls += 1
            return DiscoveryResult(
                items=[
                    DiscoveryItem(
                        provider="tmdb",
                        provider_id="tv:100",
                        tmdb_id="100",
                        media_type="tv",
                        title="Gen V",
                        source_label="本周趋势",
                    )
                ],
                total=1,
                source_label="本周趋势",
            )

        def recommend(self, **kwargs):
            self.recommend_calls += 1
            return DiscoveryResult(
                items=[
                    DiscoveryItem(
                        provider="tmdb",
                        provider_id="tv:200",
                        tmdb_id="200",
                        media_type="tv",
                        title="推荐剧集",
                        source_label="推荐",
                    )
                ],
                total=1,
                source_label="推荐",
            )

    service = DiscoveryService()
    controller = FollowingController(
        repo,
        metadata_search_service=FakeSearchService(),
        discovery_service=service,
        favorite_tmdb_binding_repository=FavoriteTMDBBindingRepository(tmp_path / "app.db"),
    )

    first_trending = controller.load_discovery_tab("trending")
    first_recommendation = controller.load_discovery_tab("recommendation")
    second_trending = controller.load_discovery_tab("trending")
    second_recommendation = controller.load_discovery_tab("recommendation")

    assert [item.provider_id for item in first_trending.items] == ["tv:100"]
    assert [item.provider_id for item in second_trending.items] == ["tv:100"]
    assert [item.provider_id for item in first_recommendation.items] == ["tv:200"]
    assert [item.provider_id for item in second_recommendation.items] == ["tv:200"]
    assert service.trending_calls == 1
    assert service.recommend_calls == 1
    assert first_trending is not second_trending
    assert first_recommendation is not second_recommendation


def test_following_controller_recommendation_seeds_include_recent_favorite_tmdb_bindings(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    favorite_bindings = FavoriteTMDBBindingRepository(tmp_path / "app.db")
    favorite_bindings.save(
        source_kind="browse",
        source_key="",
        vod_id="detail-2",
        provider_id="movie:157336",
        tmdb_id="157336",
        media_type="movie",
        title="星际穿越",
        year="2014",
        updated_at=200,
    )
    captured = {}

    class DiscoveryService:
        def recommend(self, **kwargs):
            captured.update(kwargs)
            return DiscoveryResult(items=[], total=0, source_label="推荐", fallback_reason="")

        def trending(self, query):
            return DiscoveryResult(items=[], total=0, source_label="本周趋势", fallback_reason="")

    controller = FollowingController(
        repo,
        metadata_search_service=FakeSearchService(),
        discovery_service=DiscoveryService(),
        favorite_tmdb_binding_repository=favorite_bindings,
    )

    controller.load_discovery_tab("recommendation")

    assert [seed.provider_id for seed in captured["seeds"]] == ["movie:157336"]
    assert captured["seeds"][0].seed_source == "favorite"


def test_following_controller_limits_recommendation_seed_count_for_fast_loading(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    for index in range(12):
        repo.upsert(
            FollowingRecord(
                id=0,
                title=f"剧集 {index}",
                media_kind="live_action",
                provider="tmdb",
                provider_id=f"tv:{1000 + index}",
                external_ids={"tmdb": str(1000 + index)},
                has_update=index % 2 == 0,
                updated_at=100 + index,
            )
        )
    captured = {}

    class DiscoveryService:
        def recommend(self, **kwargs):
            captured.update(kwargs)
            return DiscoveryResult(items=[], total=0, source_label="推荐", fallback_reason="")

        def trending(self, query):
            return DiscoveryResult(items=[], total=0, source_label="本周趋势", fallback_reason="")

    controller = FollowingController(
        repo,
        metadata_search_service=FakeSearchService(),
        discovery_service=DiscoveryService(),
        favorite_tmdb_binding_repository=FavoriteTMDBBindingRepository(tmp_path / "app.db"),
    )

    controller.load_discovery_tab("recommendation")

    assert len(captured["seeds"]) == 8


def test_following_controller_add_candidate_accepts_discovery_item(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")

    class SearchService:
        def detail_record(self, candidate):
            assert candidate.provider == "tmdb"
            assert candidate.provider_id == "tv:272432:season:1"
            return MetadataRecord(
                provider="tmdb",
                provider_id="tv:272432:season:1",
                title="低智商犯罪",
                year="2026",
                tmdb_id="272432",
                overview="剧集简介",
                detail_fields=[
                    {
                        "label": "episodes",
                        "value": [{"episode_number": 1, "name": "第一集"}],
                    }
                ],
            )

    controller = FollowingController(
        repo,
        metadata_search_service=SearchService(),
    )

    record = controller.add_candidate(
        DiscoveryItem(
            provider="tmdb",
            provider_id="tv:272432",
            tmdb_id="272432",
            media_type="tv",
            title="低智商犯罪",
            year="2026",
            poster="https://image.tmdb.org/t/p/original/poster.jpg",
            backdrop="https://image.tmdb.org/t/p/w1280/backdrop.jpg",
            rating="8.1",
            overview="搜索简介",
            source_label="搜索",
        )
    )

    assert record.title == "低智商犯罪"
    assert record.provider == "tmdb"
    assert record.provider_id == "tv:272432"


def test_following_controller_records_season_progress(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    following_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="黑袍纠察队",
            media_kind="live_action",
            season_number=5,
            provider="tmdb",
            provider_id="tv:76479",
            provider_priority=["tmdb"],
            current_season_number=4,
            current_episode=8,
            latest_episode=8,
            total_episodes=8,
            has_update=True,
            new_episode_count=1,
            homepage_prompt_pending=True,
        )
    )
    controller = FollowingController(
        repo,
        metadata_search_service=FakeSearchService(),
        update_service=FakeUpdateService(),
        now=lambda: 100,
    )

    controller.record_playback_progress(
        following_id,
        current_season_number=5,
        current_episode=8,
        position_seconds=15,
    )

    loaded = repo.get(following_id)
    assert loaded is not None
    assert loaded.current_season_number == 5
    assert loaded.current_episode == 8
    assert loaded.position_seconds == 15
    assert loaded.watched_latest_episode is True
    assert loaded.has_update is False
    assert loaded.new_episode_count == 0


def test_following_controller_refreshes_empty_detail_on_open(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    following_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="凡人修仙传",
            provider="bangumi",
            provider_id="subject:1",
            provider_priority=["bangumi"],
        )
    )
    update_service = SavingUpdateService(repo)
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=update_service, now=lambda: 100)

    detail = controller.load_detail(following_id)

    assert update_service.manual_checks == [following_id]
    assert detail.record.latest_episode == 1
    assert detail.snapshot.overview == "刷新后的简介"
    assert detail.snapshot.episodes[0].title == "刷新分集"


def test_following_controller_omits_unknown_episode_counts_from_card(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    repo.upsert(
        FollowingRecord(
            id=0,
            title="凡人修仙传",
            provider="player",
            provider_id="player:vod-1",
            current_episode=127,
            latest_episode=0,
            total_episodes=0,
        )
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)

    cards, _total = controller.load_page(page=1, size=20, keyword="", only_updates=False)

    assert cards[0].progress_text == "看到 S1E127"
    assert "最新 0" not in cards[0].progress_text
    assert "总 0" not in cards[0].progress_text


def test_following_controller_card_does_not_mark_cross_season_ongoing_series_completed(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    following_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="航海王",
            provider="tmdb",
            provider_id="tv:37854",
            season_number=1,
            current_season_number=15,
            current_episode=581,
            latest_episode=1163,
            total_episodes=1163,
            has_update=True,
            new_episode_count=1163,
        )
    )
    repo.save_detail_snapshot(
        following_id,
        FollowingDetailSnapshot(
            following_id=following_id,
            seasons=[FollowingSeason(season_number=23, title="第23季")],
            episodes=[FollowingEpisode(season_number=23, episode_number=1178, air_date="2026-09-06")],
        ),
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)

    cards, _total = controller.load_page(page=1, size=20, keyword="", only_updates=False)

    assert cards[0].progress_text == "看到 S15E581 · 最新 S23E1163"
    assert cards[0].update_text == "有 582 集更新"


def test_following_controller_card_uses_tmdb_global_latest_and_series_total(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    following_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="航海王",
            provider="tmdb",
            provider_id="tv:37854",
            season_number=1,
            current_season_number=15,
            current_episode=581,
            latest_episode=1163,
            total_episodes=1181,
            has_update=True,
            new_episode_count=582,
        )
    )
    repo.save_detail_snapshot(
        following_id,
        FollowingDetailSnapshot(
            following_id=following_id,
            seasons=[
                FollowingSeason(season_number=0, episode_count=35, is_special=True),
                FollowingSeason(season_number=15, episode_count=62),
                FollowingSeason(season_number=23, episode_count=61),
            ],
            episodes=[
                FollowingEpisode(season_number=23, episode_number=61, air_date="2026-05-24"),
            ],
            next_episode=FollowingEpisode(season_number=23, episode_number=1164, air_date="2026-05-31"),
        ),
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)

    cards, _total = controller.load_page(page=1, size=20, keyword="", only_updates=False)

    assert cards[0].progress_text == "看到 S15E581 · 最新 S23E1163 / 总 1181"
    assert cards[0].update_text == "有 582 集更新"


def test_following_controller_card_counts_unwatched_local_episode_updates_across_seasons(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    following_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="五十公里桃花坞",
            media_kind="variety",
            provider="tmdb",
            provider_id="tv:12345",
            season_number=1,
            current_season_number=0,
            current_episode=0,
            latest_episode=10,
            has_update=True,
            new_episode_count=10,
        )
    )
    repo.save_detail_snapshot(
        following_id,
        FollowingDetailSnapshot(
            following_id=following_id,
            seasons=[
                FollowingSeason(season_number=1, episode_count=60),
                FollowingSeason(season_number=2, episode_count=60),
                FollowingSeason(season_number=3, episode_count=60),
                FollowingSeason(season_number=4, episode_count=60),
                FollowingSeason(season_number=5, episode_count=60),
                FollowingSeason(season_number=6, episode_count=10),
            ],
            episodes=[FollowingEpisode(season_number=6, episode_number=10)],
        ),
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)

    cards, _total = controller.load_page(page=1, size=20, keyword="", only_updates=False)

    assert cards[0].progress_text == "最新 S6E10"
    assert cards[0].update_text == "有 310 集更新"


def test_following_controller_card_uses_fallback_count_for_unwatched_with_incomplete_seasons(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    following_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="流言终结者",
            media_kind="documentary",
            provider="tmdb",
            provider_id="tv:1428",
            season_number=1,
            current_season_number=0,
            current_episode=0,
            latest_episode=8,
            total_episodes=272,
            has_update=True,
            new_episode_count=11,
        )
    )
    repo.save_detail_snapshot(
        following_id,
        FollowingDetailSnapshot(
            following_id=following_id,
            seasons=[FollowingSeason(season_number=16, episode_count=11)],
            episodes=[FollowingEpisode(season_number=16, episode_number=8)],
        ),
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)

    cards, _total = controller.load_page(page=1, size=20, keyword="", only_updates=False)

    assert cards[0].progress_text == "最新 S16E8 / 总 272"
    assert cards[0].update_text == "有 8 集更新"


def test_following_controller_card_infers_latest_season_for_unwatched_unseasoned_episodes(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    following_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="流言终结者",
            media_kind="documentary",
            provider="tmdb",
            provider_id="tv:1428",
            season_number=1,
            current_season_number=0,
            current_episode=0,
            latest_episode=8,
            total_episodes=272,
            has_update=True,
            new_episode_count=272,
        )
    )
    repo.save_detail_snapshot(
        following_id,
        FollowingDetailSnapshot(
            following_id=following_id,
            seasons=[
                FollowingSeason(season_number=1, episode_count=11),
                FollowingSeason(season_number=16, episode_count=11),
            ],
            episodes=[FollowingEpisode(episode_number=index) for index in range(1, 12)],
        ),
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)

    cards, _total = controller.load_page(page=1, size=20, keyword="", only_updates=False)

    assert cards[0].progress_text == "最新 S16E8 / 总 272"
    assert cards[0].update_text == "有 8 集更新"


def test_following_controller_card_treats_out_of_range_season_current_as_unwatched(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    following_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="成何体统 第二季",
            provider="tmdb",
            provider_id="tv:256783",
            season_number=2,
            current_season_number=2,
            current_episode=112,
            latest_episode=112,
            total_episodes=24,
        )
    )
    repo.save_detail_snapshot(
        following_id,
        FollowingDetailSnapshot(
            following_id=following_id,
            seasons=[FollowingSeason(season_number=2, title="第二季", episode_count=24)],
            episodes=[
                FollowingEpisode(season_number=2, episode_number=index)
                for index in range(1, 25)
            ],
        ),
    )
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)

    cards, _total = controller.load_page(page=1, size=20, keyword="", only_updates=False)

    assert "S2E112" not in cards[0].progress_text
    assert "看到 S2E24" not in cards[0].progress_text
    assert "最新 S2E24" in cards[0].progress_text


def test_following_controller_adds_from_player_and_updates_progress(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)
    vod = VodItem(
        vod_id="vod-1",
        vod_name="凡人修仙传",
        vod_pic="poster",
        vod_content="简介",
        vod_actor="韩立, 南宫婉",
        vod_director="王裕仁",
        dbid=123,
        detail_fields=[PlaybackDetailField("TMDB ID", "456")],
    )
    playlist = [
        PlayItem(title="第127集", url="u", media_title="凡人修仙传", vod_id="vod-1", episode_display_title="风起"),
        PlayItem(title="第128集", url="u2", media_title="凡人修仙传", vod_id="vod-1", episode_display_title="新章"),
    ]
    item = playlist[0]

    record = controller.add_from_player(
        vod=vod,
        item=item,
        source_kind="browse",
        source_key="",
        position_seconds=321,
        playlist=playlist,
    )
    controller.record_playback_progress(record.id, current_episode=128, position_seconds=15)

    loaded = repo.get(record.id)
    snapshot = repo.get_detail_snapshot(record.id)
    assert loaded.current_episode == 128
    assert loaded.position_seconds == 15
    assert loaded.total_episodes == 2
    assert loaded.external_ids == {"douban": "123", "tmdb": "456"}
    assert loaded.source_bindings[0].vod_id == "vod-1"
    assert snapshot is not None
    assert snapshot.overview == "简介"
    assert snapshot.cast[0]["name"] == "韩立"
    assert snapshot.crew[0]["name"] == "王裕仁"
    assert snapshot.episodes[0].title == "风起"


def test_following_controller_add_from_player_preserves_enriched_metadata_fields(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)
    vod = VodItem(
        vod_id="vod-1",
        vod_name="低智商犯罪",
        vod_year="2025",
        type_name="喜剧 / 悬疑 / 犯罪",
        vod_area="中国大陆",
        vod_lang="汉语普通话",
        vod_director="刘海波",
        vod_actor="王骁,田曦薇",
        dbid=35517044,
        detail_fields=[
            PlaybackDetailField("TMDB ID", "272432"),
            PlaybackDetailField("IMDb ID", "tt32592348"),
        ],
    )
    item = PlayItem(title="第1集", url="u", media_title="低智商犯罪", vod_id="vod-1")

    record = controller.add_from_player(
        vod=vod,
        item=item,
        source_kind="browse",
        source_key="",
        position_seconds=0,
        playlist=[item],
    )

    snapshot = repo.get_detail_snapshot(record.id)
    assert snapshot is not None
    assert snapshot.metadata_fields == [
        {"label": "类型", "value": "喜剧 / 悬疑 / 犯罪"},
        {"label": "年代", "value": "2025"},
        {"label": "地区", "value": "中国大陆"},
        {"label": "语言", "value": "汉语普通话"},
        {"label": "导演", "value": "刘海波"},
        {"label": "演员", "value": "王骁,田曦薇"},
        {"label": "豆瓣ID", "value": "35517044"},
        {"label": "TMDB ID", "value": "272432"},
        {"label": "IMDb ID", "value": "tt32592348"},
    ]


def test_following_controller_add_from_player_can_skip_initial_watched_progress(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)
    vod = VodItem(vod_id="vod-1", vod_name="成何体统第二季")
    item = PlayItem(title="第112集", url="https://media.example/112.m3u8", media_title="成何体统第二季", vod_id="vod-1")

    record = controller.add_from_player(
        vod=vod,
        item=item,
        source_kind="browse",
        source_key="",
        position_seconds=0,
        playlist=[item],
        mark_current_episode=False,
    )

    loaded = repo.get(record.id)
    assert loaded is not None
    assert loaded.current_episode == 0
    assert loaded.position_seconds == 0
    assert loaded.latest_episode == 112
    assert loaded.watched_latest_episode is False


def test_following_controller_uses_playlist_count_for_latest_and_metadata_count_for_total(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)
    metadata_episodes = [
        {
            "episode_number": episode_number,
            "name": f"TMDB {episode_number}",
            "overview": f"剧情 {episode_number}",
        }
        for episode_number in range(1, 93)
    ]
    vod = VodItem(
        vod_id="vod-1",
        vod_name="牧神记",
        detail_fields=[
            PlaybackDetailField("TMDB ID", "236534"),
            PlaybackDetailField("episodes", repr(metadata_episodes)),
        ],
    )
    playlist = [
        PlayItem(title="第26集", url=f"u{index}", media_title="牧神记", vod_id="vod-1")
        for index in range(84)
    ]
    for index, item in enumerate(playlist):
        item.index = index

    record = controller.add_from_player(
        vod=vod,
        item=playlist[-1],
        source_kind="browse",
        source_key="",
        position_seconds=0,
        playlist=playlist,
    )

    loaded = repo.get(record.id)
    snapshot = repo.get_detail_snapshot(record.id)
    assert loaded is not None
    assert loaded.current_episode == 84
    assert loaded.latest_episode == 84
    assert loaded.total_episodes == 92
    assert loaded.external_ids["tmdb"] == "236534"
    assert snapshot is not None
    assert len(snapshot.episodes) == 92
    assert snapshot.episodes[-1].title == "TMDB 92"


def test_following_controller_refreshes_metadata_with_tmdb_details_for_bangumi_following(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="仙逆",
            media_kind="anime",
            provider="bangumi",
            provider_id="subject:1",
            provider_priority=["bangumi", "tmdb", "douban"],
            external_ids={"bangumi": "1"},
            rating="8.4",
            latest_episode=84,
            total_episodes=92,
        )
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            episodes=[FollowingEpisode(episode_number=1, title="Bangumi第1集")],
        ),
    )
    service = FakeFollowingMetadataRefreshService()
    controller = FollowingController(repo, metadata_search_service=service, now=lambda: 100)

    refreshed = controller.refresh_metadata(record_id)

    loaded = repo.get(record_id)
    snapshot = repo.get_detail_snapshot(record_id)
    assert service.detail_provider_ids == [
        ("tmdb", "tv:236534:season:1"),
        ("bangumi", "subject:1"),
    ]
    assert loaded is not None
    assert loaded.provider == "tmdb"
    assert loaded.provider_id == "tv:236534"
    assert loaded.rating == "7.6"
    assert loaded.poster == "tmdb-poster"
    assert loaded.backdrop == "tmdb-backdrop"
    assert loaded.latest_episode == 84
    assert loaded.total_episodes == 92
    assert refreshed.snapshot.overview == "TMDB简介"
    assert refreshed.snapshot.episodes[0].title == "TMDB第1集"
    assert snapshot is not None
    assert snapshot.episodes[0].overview == "TMDB剧情"
    assert snapshot.episodes[0].still == "tmdb-still"
    assert snapshot.cast[0]["name"] == "史泽鲲"


def test_following_controller_refreshes_live_action_avatars_from_existing_tmdb_id(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="低智商犯罪",
            media_kind="live_action",
            season_number=1,
            provider="player",
            provider_id="player:source:vod-1",
            external_ids={"tmdb": "272432"},
        )
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            overview="原简介",
            cast=[{"name": "王骁"}, {"name": "田曦薇"}],
            episodes=[FollowingEpisode(episode_number=1, title="第一集", still="old-still")],
        ),
    )
    service = FakeTMDBIdRefreshService()
    controller = FollowingController(repo, metadata_search_service=service, now=lambda: 100)

    refreshed = controller.refresh_metadata(record_id)

    snapshot = repo.get_detail_snapshot(record_id)
    assert service.search_provider_filters == ["official_douban", "local_douban", "douban"]
    assert service.full_detail_provider_ids == ["tv:272432:season:1"]
    assert refreshed.snapshot.cast[0]["avatar"] == "/wang.jpg"
    assert snapshot is not None
    assert snapshot.cast[1]["avatar"] == "/tian.jpg"
    assert snapshot.episodes[0].still == "old-still"


def test_following_controller_refresh_metadata_rebuilds_existing_metadata_bundle_platforms(tmp_path: Path) -> None:
    class SearchService:
        def search(self, query, provider_filter=""):
            assert query.title == "蜜语纪"
            if provider_filter != "tencent":
                return []
            return [
                MetadataScrapeGroup(
                    provider="tencent",
                    provider_label="腾讯",
                    items=[
                        MetadataScrapeCandidate(
                            provider="tencent",
                            provider_label="腾讯",
                            provider_id="https://v.qq.com/x/cover/mzc002006dzzunf/h4102lz1osw.html",
                            title="蜜语纪",
                            year="2026",
                        )
                    ],
                )
            ]

        def detail_record(self, candidate):
            if candidate.provider == "tencent":
                return MetadataRecord(
                    provider="tencent",
                    provider_id=candidate.provider_id,
                    title="蜜语纪",
                    year="2026",
                    detail_fields=[{"label": "播放链接", "value": candidate.provider_id}],
                )
            return self.detail_record_full(candidate)

        def detail_record_full(self, candidate):
            assert candidate.provider == "tmdb"
            return MetadataRecord(
                provider="tmdb",
                provider_id="tv:281231:season:1",
                title="蜜语纪",
                year="2026",
                tmdb_id="281231",
                overview="TMDB简介",
                detail_fields=[
                    {
                        "label": "watch_providers",
                        "value": [
                            {
                                "provider": "iqiyi",
                                "label": "爱奇艺",
                                "url": "https://www.iqiyi.com/a_1euk1nkfz9l.html",
                            }
                        ],
                    },
                    {"label": "episodes", "value": [{"episode_number": 38, "name": "第38集"}]},
                ],
            )

    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="蜜语纪",
            media_kind="live_action",
            season_number=1,
            provider="tmdb",
            provider_id="tv:281231",
            external_ids={"tmdb": "281231"},
        )
    )
    stale_bundle = FollowingMetadataBundle(
        merged_snapshot=FollowingMetadataSourceSnapshot(
            source_key="merged",
            provider="merged",
            provider_label="合并",
            playback_platforms=[
                FollowingPlaybackPlatformEntry(
                    provider="iqiyi",
                    label="爱奇艺",
                    url="https://www.iqiyi.com/a_1euk1nkfz9l.html",
                )
            ],
        ),
        source_snapshots={
            "merged": FollowingMetadataSourceSnapshot(
                source_key="merged",
                provider="merged",
                provider_label="合并",
            )
        },
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            overview="旧简介",
            metadata_bundle=stale_bundle,
            episodes=[FollowingEpisode(episode_number=1, title="旧第1集")],
        ),
    )
    controller = FollowingController(repo, metadata_search_service=SearchService(), now=lambda: 100)

    refreshed = controller.refresh_metadata(record_id)

    platforms = refreshed.snapshot.metadata_bundle.merged_snapshot.playback_platforms
    assert [(item.provider, item.url) for item in platforms] == [
        ("iqiyi", "https://www.iqiyi.com/a_1euk1nkfz9l.html"),
    ]


def test_following_controller_refresh_metadata_keeps_existing_episode_entries_when_refresh_returns_subset(
    tmp_path: Path,
) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="低智商犯罪",
            media_kind="live_action",
            season_number=1,
            provider="player",
            provider_id="player:source:vod-1",
            external_ids={"tmdb": "272432"},
        )
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            episodes=[
                FollowingEpisode(episode_number=1, season_number=1, title="第一集", still="old-1"),
                FollowingEpisode(episode_number=2, season_number=1, title="第二集", still="old-2"),
                FollowingEpisode(episode_number=3, season_number=1, title="第三集", still="old-3"),
            ],
        ),
    )

    class PartialEpisodeRefreshService(FakeTMDBIdRefreshService):
        def detail_record_full(self, candidate):
            self.full_detail_provider_ids.append(candidate.provider_id)
            return MetadataRecord(
                provider="tmdb",
                provider_id=candidate.provider_id,
                title="低智商犯罪",
                tmdb_id="272432",
                detail_fields=[
                    {
                        "label": "episodes",
                        "value": [
                            {"episode_number": 1, "season_number": 1, "name": "第一集", "still_url": "new-1"},
                            {"episode_number": 2, "season_number": 1, "name": "第二集", "still_url": ""},
                        ],
                    }
                ],
            )

    controller = FollowingController(repo, metadata_search_service=PartialEpisodeRefreshService(), now=lambda: 100)

    refreshed = controller.refresh_metadata(record_id)

    snapshot = repo.get_detail_snapshot(record_id)
    assert snapshot is not None
    assert [episode.episode_number for episode in snapshot.episodes] == [1, 2, 3]
    assert snapshot.episodes[0].still == "new-1"
    assert snapshot.episodes[1].still == "old-2"
    assert snapshot.episodes[2].still == "old-3"
    assert [episode.episode_number for episode in refreshed.snapshot.episodes] == [1, 2, 3]


def test_following_controller_load_detail_season_replaces_episode_list_for_requested_tmdb_season(
    tmp_path: Path,
) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="低智商犯罪",
            media_kind="live_action",
            season_number=1,
            provider="player",
            provider_id="player:source:vod-1",
            external_ids={"tmdb": "272432"},
        )
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            overview="总简介",
            episodes=[FollowingEpisode(episode_number=1, season_number=1, title="S1E1")],
        ),
    )

    class SeasonDetailService(FakeSearchService):
        def __init__(self) -> None:
            self.provider_ids: list[str] = []

        def detail_record_full(self, candidate):
            self.provider_ids.append(candidate.provider_id)
            return MetadataRecord(
                provider="tmdb",
                provider_id=candidate.provider_id,
                title="低智商犯罪",
                overview="总简介",
                tmdb_id="272432",
                detail_fields=[
                    {
                        "label": "seasons",
                        "value": [
                            {"season_number": 1, "name": "第一季", "episode_count": 24},
                            {"season_number": 2, "name": "第二季", "episode_count": 20},
                        ],
                    },
                    {
                        "label": "episodes",
                        "value": [
                            {"episode_number": 1, "season_number": 2, "name": "S2E1"},
                            {"episode_number": 2, "season_number": 2, "name": "S2E2"},
                        ],
                    },
                ],
            )

    service = SeasonDetailService()
    controller = FollowingController(repo, metadata_search_service=service, now=lambda: 100)

    detail = controller.load_detail_season(record_id, season_number=2)

    snapshot = repo.get_detail_snapshot(record_id)
    assert service.provider_ids == ["tv:272432:season:2"]
    assert [episode.title for episode in detail.snapshot.episodes] == ["S2E1", "S2E2"]
    assert snapshot is not None
    assert [season.season_number for season in snapshot.seasons] == [1, 2]
    assert [episode.season_number for episode in snapshot.episodes] == [2, 2]


def test_following_controller_load_detail_season_overrides_existing_tmdb_provider_season(
    tmp_path: Path,
) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="黑袍纠察队",
            media_kind="live_action",
            season_number=5,
            provider="tmdb",
            provider_id="tv:76479:season:1",
            external_ids={"tmdb": "76479"},
        )
    )

    class SeasonOverrideService(FakeSearchService):
        def __init__(self) -> None:
            self.provider_ids: list[str] = []

        def detail_record_full(self, candidate):
            self.provider_ids.append(candidate.provider_id)
            return MetadataRecord(
                provider="tmdb",
                provider_id=candidate.provider_id,
                title="黑袍纠察队",
                tmdb_id="76479",
                detail_fields=[
                    {
                        "label": "episodes",
                        "value": [
                            {"episode_number": 1, "season_number": 5, "name": "S5E1"},
                        ],
                    }
                ],
            )

    service = SeasonOverrideService()
    controller = FollowingController(repo, metadata_search_service=service, now=lambda: 100)

    detail = controller.load_detail_season(record_id, season_number=5)

    assert service.provider_ids == ["tv:76479:season:5"]
    assert [episode.season_number for episode in detail.snapshot.episodes] == [5]


def test_following_controller_load_detail_season_loads_tmdb_specials(
    tmp_path: Path,
) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="黑袍纠察队",
            media_kind="live_action",
            season_number=1,
            provider="tmdb",
            provider_id="tv:76479:season:1",
            external_ids={"tmdb": "76479"},
        )
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            seasons=[
                FollowingSeason(season_number=0, title="特别篇", episode_count=1),
                FollowingSeason(season_number=1, title="第一季", episode_count=8),
            ],
            episodes=[FollowingEpisode(episode_number=1, season_number=1, title="S1E1")],
        ),
    )

    class SpecialsSeasonService(FakeSearchService):
        def __init__(self) -> None:
            self.provider_ids: list[str] = []

        def detail_record_full(self, candidate):
            self.provider_ids.append(candidate.provider_id)
            return MetadataRecord(
                provider="tmdb",
                provider_id=candidate.provider_id,
                title="黑袍纠察队",
                tmdb_id="76479",
                detail_fields=[
                    {
                        "label": "seasons",
                        "value": [
                            {"season_number": 0, "name": "特别篇", "episode_count": 1},
                            {"season_number": 1, "name": "第一季", "episode_count": 8},
                        ],
                    },
                    {
                        "label": "episodes",
                        "value": [
                            {"episode_number": 1, "season_number": 0, "name": "花絮"},
                        ],
                    },
                ],
            )

    service = SpecialsSeasonService()
    controller = FollowingController(repo, metadata_search_service=service, now=lambda: 100)

    detail = controller.load_detail_season(record_id, season_number=0)

    assert service.provider_ids == ["tv:76479:season:0"]
    assert [episode.title for episode in detail.snapshot.episodes] == ["花絮"]
    assert [episode.season_number for episode in detail.snapshot.episodes] == [0]
    assert [episode.is_special for episode in detail.snapshot.episodes] == [True]


class FakeFullEpisodeRefreshService:
    def __init__(self, latest: int, total: int) -> None:
        self._latest = latest
        self._total = total
        self.search_calls = 0

    def search(self, query, provider_filter=""):
        del query, provider_filter
        self.search_calls += 1
        return []

    def detail_record_full(self, candidate):
        episodes = [
            {"episode_number": index, "name": f"第{index}集", "type": 0, "air_date": ""}
            for index in range(1, self._latest + 1)
        ]
        episodes.extend(
            {"episode_number": index, "name": f"第{index}集", "type": 0, "air_date": "2099-01-01"}
            for index in range(self._latest + 1, self._total + 1)
        )
        return MetadataRecord(
            provider="tmdb",
            provider_id=candidate.provider_id,
            title="新元数据剧集",
            poster="new-poster",
            backdrop="new-backdrop",
            overview="新简介",
            tmdb_id="999",
            detail_fields=[{"label": "episodes", "value": episodes}],
        )


def test_following_controller_refresh_metadata_corrects_wrong_episode_counts(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="错误集数剧集",
            media_kind="live_action",
            season_number=1,
            provider="player",
            provider_id="player:source:vod-1",
            external_ids={"tmdb": "999"},
            current_episode=12,
            latest_episode=12,
            previous_latest_episode=12,
            total_episodes=12,
            watched_latest_episode=True,
            has_update=False,
            new_episode_count=0,
            homepage_prompt_pending=False,
        )
    )
    service = FakeFullEpisodeRefreshService(latest=24, total=30)
    controller = FollowingController(repo, metadata_search_service=service, now=lambda: 100)

    controller.refresh_metadata(record_id)

    loaded = repo.get(record_id)
    assert loaded is not None
    assert loaded.latest_episode == 24
    assert loaded.total_episodes == 30
    assert loaded.current_episode == 12
    assert loaded.watched_latest_episode is False
    assert loaded.has_update is True
    assert loaded.new_episode_count == 12


def test_following_controller_add_from_player_captures_season_number_from_title(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), update_service=FakeUpdateService(), now=lambda: 100)
    vod = VodItem(
        vod_id="vod-1",
        vod_name="黑袍纠察队 第五季",
        detail_fields=[PlaybackDetailField("TMDB ID", "76479")],
    )
    item = PlayItem(title="第1集", url="u", media_title="黑袍纠察队 第五季", vod_id="vod-1")

    record = controller.add_from_player(
        vod=vod,
        item=item,
        source_kind="browse",
        source_key="",
        position_seconds=0,
        playlist=[item],
    )

    loaded = repo.get(record.id)
    assert loaded is not None
    assert loaded.season_number == 5


def test_following_controller_refresh_metadata_skips_tmdb_when_season_unknown(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="未知季剧集",
            media_kind="live_action",
            provider="player",
            provider_id="player:source:vod-1",
            external_ids={"tmdb": "272432"},
        )
    )
    service = FakeTMDBIdRefreshService()
    controller = FollowingController(repo, metadata_search_service=service, now=lambda: 100)

    try:
        controller.refresh_metadata(record_id)
    except RuntimeError:
        pass

    assert service.full_detail_provider_ids == []


class AISummarizesFollowingDetail:
    def __init__(self) -> None:
        self.inputs = []

    def summarize_following_detail(self, data):
        self.inputs.append(data)
        return FollowingDetailSummary(
            summary="AI 摘要",
            highlights=["看点一", "看点二"],
            next_hint="明晚更新",
        )


def test_following_controller_adds_display_only_ai_summary(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "following.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="黑镜",
            current_episode=1,
            latest_episode=2,
            total_episodes=6,
        )
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            overview="科技寓言",
            next_episode=FollowingEpisode(
                episode_number=3,
                title="第三集",
                air_date="2026-05-30",
            ),
        ),
    )
    ai = AISummarizesFollowingDetail()
    controller = FollowingController(
        repo,
        metadata_search_service=FakeSearchService(),
        ai_enrichment_service=ai,
        now=lambda: 100,
    )

    view = controller.load_detail(record_id, refresh_if_empty=False)

    assert ai.inputs[0].title == "黑镜"
    assert view.snapshot.ai_summary is not None
    assert view.snapshot.ai_summary.summary == "AI 摘要"
    assert view.record.latest_episode == 2


def test_following_controller_ai_summary_uses_pre_metadata_bundle_snapshot(tmp_path: Path) -> None:
    class MetadataEnhancingFollowingController(FollowingController):
        def _ensure_metadata_bundle(self, record, snapshot, *, force=False, tmdb_detail_record=None):
            del record, force, tmdb_detail_record
            return FollowingDetailSnapshot(
                following_id=snapshot.following_id,
                overview="增强简介",
                metadata_fields=[{"label": "增强字段", "value": "增强值"}],
                metadata_bundle=FollowingMetadataBundle(
                    merged_snapshot=FollowingMetadataSourceSnapshot(
                        source_key="merged",
                        provider="merged",
                        provider_label="合并",
                        overview="增强简介",
                        metadata_fields=[{"label": "增强字段", "value": "增强值"}],
                    )
                ),
            )

    repo = FollowingRepository(tmp_path / "following.db")
    record_id = repo.upsert(FollowingRecord(id=0, title="黑镜"))
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            overview="原始简介",
            metadata_fields=[{"label": "原始字段", "value": "原始值"}],
        ),
    )
    ai = AISummarizesFollowingDetail()
    controller = MetadataEnhancingFollowingController(
        repo,
        metadata_search_service=FakeSearchService(),
        ai_enrichment_service=ai,
        now=lambda: 100,
    )

    view = controller.load_detail(record_id, refresh_if_empty=False)

    assert ai.inputs[0].overview == "原始简介"
    assert ai.inputs[0].metadata_fields == [{"label": "原始字段", "value": "原始值"}]
    assert view.snapshot.overview == "增强简介"
    assert view.snapshot.ai_summary is not None


def test_following_controller_can_skip_ai_summary_on_detail_load(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "following.db")
    record_id = repo.upsert(FollowingRecord(id=0, title="黑镜"))
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(following_id=record_id, overview="科技寓言"),
    )
    ai = AISummarizesFollowingDetail()
    controller = FollowingController(
        repo,
        metadata_search_service=FakeSearchService(),
        ai_enrichment_service=ai,
        now=lambda: 100,
    )

    view = controller.load_detail(
        record_id,
        refresh_if_empty=False,
        include_ai_summary=False,
    )

    assert ai.inputs == []
    assert view.snapshot.ai_summary is None


def test_following_controller_loads_related_recommendations_from_tmdb_record(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="黑袍纠察队",
            provider="tmdb",
            provider_id="tv:76479",
            external_ids={"tmdb": "76479"},
        )
    )

    class Service:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def related(self, **kwargs):
            self.calls.append(kwargs)
            return DiscoveryResult(
                items=[
                    DiscoveryItem(
                        provider="tmdb",
                        provider_id="tv:100",
                        tmdb_id="100",
                        media_type="tv",
                        title="Gen V",
                    )
                ],
                total=1,
                source_label="关联推荐",
            )

    service = Service()
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), discovery_service=service)

    result = controller.load_related_recommendations(record_id)

    assert service.calls == [
        {
            "media_type": "tv",
            "tmdb_id": "76479",
            "excluded_provider_ids": {"tv:76479"},
        }
    ]
    assert result.items[0].title == "Gen V"


def test_following_controller_loads_related_recommendations_from_tmdb_source_snapshot(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="星际穿越",
            media_kind="电影",
            provider="player",
            provider_id="player:source:vod-1",
        )
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            metadata_bundle=FollowingMetadataBundle(
                merged_snapshot=FollowingMetadataSourceSnapshot(
                    source_key="merged",
                    provider="merged",
                    provider_label="合并",
                ),
                source_snapshots={
                    "tmdb": FollowingMetadataSourceSnapshot(
                        source_key="tmdb",
                        provider="tmdb",
                        provider_label="TMDB",
                        provider_id="movie:157336",
                    )
                },
                available_source_keys=["merged", "tmdb"],
                default_source_key="merged",
            ),
        ),
    )

    class Service:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def related(self, **kwargs):
            self.calls.append(kwargs)
            return DiscoveryResult(items=[], total=0, source_label="关联推荐")

    service = Service()
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), discovery_service=service)

    result = controller.load_related_recommendations(record_id)

    assert result.source_label == "关联推荐"
    assert service.calls == [
        {
            "media_type": "movie",
            "tmdb_id": "157336",
            "excluded_provider_ids": {"movie:157336"},
        }
    ]


def test_following_controller_prefers_douban_related_for_domestic_anime(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="凡人修仙传",
            media_kind="anime",
            provider="tmdb",
            provider_id="tv:30983",
            external_ids={"tmdb": "30983", "douban": "30267287"},
        )
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            metadata_fields=[
                {"label": "类型", "value": "动画 / 奇幻"},
                {"label": "地区", "value": "中国大陆"},
            ],
        ),
    )

    class Service:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def related(self, **kwargs):
            self.calls.append(kwargs)
            return DiscoveryResult(items=[], total=0, source_label="关联推荐")

    service = Service()
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), discovery_service=service)

    controller.load_related_recommendations(record_id)

    assert service.calls == [
        {
            "media_type": "tv",
            "tmdb_id": "30983",
            "douban_id": "30267287",
            "prefer_douban": True,
            "excluded_provider_ids": {"tv:30983"},
        }
    ]


def test_following_controller_keeps_tmdb_related_for_non_domestic_movie(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="星际穿越",
            media_kind="movie",
            provider="tmdb",
            provider_id="movie:157336",
            external_ids={"tmdb": "157336", "douban": "1889243"},
        )
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            metadata_fields=[
                {"label": "类型", "value": "剧情 / 科幻"},
                {"label": "地区", "value": "美国 / 英国"},
            ],
        ),
    )

    class Service:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def related(self, **kwargs):
            self.calls.append(kwargs)
            return DiscoveryResult(items=[], total=0, source_label="关联推荐")

    service = Service()
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), discovery_service=service)

    controller.load_related_recommendations(record_id)

    assert service.calls == [
        {
            "media_type": "movie",
            "tmdb_id": "157336",
            "excluded_provider_ids": {"movie:157336"},
        }
    ]


def test_following_controller_loads_douban_related_when_domestic_record_has_no_tmdb_id(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="国宝里的中国故事",
            media_kind="documentary",
            provider="official_douban",
            provider_id="35746415",
            external_ids={"douban": "35746415"},
        )
    )
    repo.save_detail_snapshot(
        record_id,
        FollowingDetailSnapshot(
            following_id=record_id,
            metadata_fields=[
                {"label": "类型", "value": "纪录片"},
                {"label": "地区", "value": "中国大陆"},
            ],
        ),
    )

    class Service:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def related(self, **kwargs):
            self.calls.append(kwargs)
            return DiscoveryResult(items=[], total=0, source_label="关联推荐")

    service = Service()
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), discovery_service=service)

    controller.load_related_recommendations(record_id)

    assert service.calls == [
        {
            "media_type": "tv",
            "tmdb_id": "",
            "douban_id": "35746415",
            "prefer_douban": True,
            "excluded_provider_ids": set(),
        }
    ]
def test_following_controller_updates_recent_playback_binding_when_progress_reaches_current(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), now=lambda: 500)
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="凡人修仙传",
            provider="player",
            provider_id="browse::vod-1",
            source_bindings=[
                FollowingSourceBinding(source_kind="browse", source_key="", vod_id="vod-1")
            ],
            current_season_number=1,
            current_episode=20,
            latest_episode=20,
        )
    )

    controller.record_playback_source(
        record_id,
        source_kind="telegram",
        source_key="",
        vod_id="tg-vod-1",
        current_season_number=1,
        current_episode=20,
        playlist_latest_episode=24,
    )

    loaded = repo.get(record_id)
    assert loaded is not None
    assert [(b.source_kind, b.source_key, b.vod_id) for b in loaded.source_bindings[:2]] == [
        ("telegram", "", "tg-vod-1"),
        ("browse", "", "vod-1"),
    ]
    assert loaded.latest_episode == 24
    assert loaded.has_update is True
    assert loaded.new_episode_count == 4


def test_following_controller_keeps_binding_when_switched_source_is_behind_current_progress(tmp_path: Path) -> None:
    repo = FollowingRepository(tmp_path / "app.db")
    controller = FollowingController(repo, metadata_search_service=FakeSearchService(), now=lambda: 500)
    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="凡人修仙传",
            provider="player",
            provider_id="browse::vod-1",
            source_bindings=[
                FollowingSourceBinding(source_kind="browse", source_key="", vod_id="vod-1")
            ],
            current_season_number=1,
            current_episode=20,
            latest_episode=20,
        )
    )

    controller.record_playback_source(
        record_id,
        source_kind="telegram",
        source_key="",
        vod_id="tg-vod-1",
        current_season_number=1,
        current_episode=1,
        playlist_latest_episode=24,
    )

    loaded = repo.get(record_id)
    assert loaded is not None
    assert [(b.source_kind, b.source_key, b.vod_id) for b in loaded.source_bindings] == [
        ("browse", "", "vod-1")
    ]
    assert loaded.latest_episode == 24
    assert loaded.has_update is True


def test_check_all_due_and_check_one_both_trigger_backend_sync(tmp_path) -> None:
    class StubBackendSync:
        def __init__(self) -> None:
            self.calls = 0

        def sync_blocking(self):
            self.calls += 1
            return []

    repo = FollowingRepository(tmp_path / "app.db")
    backend = StubBackendSync()
    controller = FollowingController(
        repo,
        metadata_search_service=FakeSearchService(),
        update_service=FakeUpdateService(),
        backend_sync_service=backend,
        now=lambda: 100,
    )

    controller.check_all_due()
    assert backend.calls == 1

    record_id = repo.upsert(
        FollowingRecord(
            id=0,
            title="凡人修仙传",
            media_kind="anime",
            provider="tmdb",
            provider_id="tv:456",
            season_number=1,
            current_episode=3,
            latest_episode=3,
            created_at=1,
            updated_at=1,
        )
    )
    controller.check_one(record_id)
    assert backend.calls == 2

    no_service = FollowingController(
        repo,
        metadata_search_service=FakeSearchService(),
        now=lambda: 100,
    )
    no_service.check_all_due()  # 无后端服务时不炸
    no_service.check_one(record_id)
